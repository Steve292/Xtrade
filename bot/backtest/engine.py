from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.risk import calc_position_size
from bot.smc.strategy import SMCStrategy, SignalType


@dataclass
class Trade:
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    size: float
    pnl: float
    outcome: str
    reason: str


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[pd.Timestamp] = field(default_factory=list)
    initial_balance: float = 10000.0
    final_balance: float = 10000.0

    @property
    def total_return_pct(self) -> float:
        return (self.final_balance - self.initial_balance) / self.initial_balance * 100

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades) * 100

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown_pct(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        returns = pd.Series(self.equity_curve).pct_change().dropna()
        if returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std() * np.sqrt(252 * 24 * 4))  # 15m bars


def resample_htf(df: pd.DataFrame, htf: str) -> pd.DataFrame:
    """Build higher-timeframe OHLCV from lower-timeframe data."""
    tmp = df.set_index("timestamp")
    rule = {"1h": "1h", "4h": "4h", "1d": "1D"}.get(htf, "1h")
    resampled = tmp.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return resampled.reset_index()


class BacktestEngine:
    """Walk-forward backtester for the SMC strategy."""

    def __init__(
        self,
        strategy: SMCStrategy,
        initial_balance: float = 10000.0,
        risk_pct: float = 1.0,
        warmup_bars: int = 50,
    ):
        self.strategy = strategy
        self.initial_balance = initial_balance
        self.risk_pct = risk_pct
        self.warmup_bars = warmup_bars

    def run(
        self,
        df: pd.DataFrame,
        htf_df: pd.DataFrame | None = None,
        htf: str = "1h",
    ) -> BacktestResult:
        balance = self.initial_balance
        position: dict | None = None
        trades: list[Trade] = []
        equity: list[float] = []
        timestamps: list[pd.Timestamp] = []

        if htf_df is None:
            htf_df = resample_htf(df, htf)

        for i in range(self.warmup_bars, len(df)):
            bar = df.iloc[i]
            ts = bar["timestamp"]
            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            # Manage open position — check SL/TP against bar range
            if position is not None:
                hit_sl = hit_tp = False
                exit_price = close

                if position["side"] == "long":
                    if low <= position["sl"]:
                        hit_sl, exit_price = True, position["sl"]
                    elif high >= position["tp"]:
                        hit_tp, exit_price = True, position["tp"]
                else:
                    if high >= position["sl"]:
                        hit_sl, exit_price = True, position["sl"]
                    elif low <= position["tp"]:
                        hit_tp, exit_price = True, position["tp"]

                if hit_sl or hit_tp:
                    pnl = self._pnl(position, exit_price)
                    balance += pnl
                    trades.append(
                        Trade(
                            side=position["side"],
                            entry_time=position["entry_time"],
                            exit_time=ts,
                            entry=position["entry"],
                            exit=exit_price,
                            size=position["size"],
                            pnl=pnl,
                            outcome="TP" if hit_tp else "SL",
                            reason=position["reason"],
                        )
                    )
                    position = None

            # Look for new entry
            if position is None:
                window = df.iloc[: i + 1]
                htf_window = htf_df[htf_df["timestamp"] <= ts]
                if len(htf_window) < 20:
                    htf_window = htf_df.iloc[: max(20, len(htf_df))]

                signal = self.strategy.analyze(window, htf_window)
                if signal.type != SignalType.NONE:
                    size = calc_position_size(
                        balance, signal.entry, signal.stop_loss, self.risk_pct
                    )
                    if size > 0:
                        position = {
                            "side": signal.type.value,
                            "entry": signal.entry,
                            "size": size,
                            "sl": signal.stop_loss,
                            "tp": signal.take_profit,
                            "reason": signal.reason,
                            "entry_time": ts,
                        }

            equity.append(balance)
            timestamps.append(ts)

        return BacktestResult(
            trades=trades,
            equity_curve=equity,
            timestamps=timestamps,
            initial_balance=self.initial_balance,
            final_balance=balance,
        )

    def _pnl(self, position: dict, exit_price: float) -> float:
        if position["side"] == "long":
            return (exit_price - position["entry"]) * position["size"]
        return (position["entry"] - exit_price) * position["size"]


def format_report(result: BacktestResult) -> str:
    lines = [
        "=" * 55,
        "  SMC Backtest Results",
        "=" * 55,
        f"  Initial Balance:  ${result.initial_balance:,.2f}",
        f"  Final Balance:    ${result.final_balance:,.2f}",
        f"  Total Return:     {result.total_return_pct:+.2f}%",
        f"  Total Trades:     {len(result.trades)}",
        f"  Win Rate:         {result.win_rate:.1f}%",
        f"  Profit Factor:    {result.profit_factor:.2f}",
        f"  Max Drawdown:     {result.max_drawdown_pct:.2f}%",
        f"  Sharpe Ratio:     {result.sharpe_ratio:.2f}",
        "=" * 55,
    ]
    if result.trades:
        lines.append("\n  Recent Trades:")
        for t in result.trades[-5:]:
            lines.append(
                f"    {t.side.upper()} {t.outcome} | "
                f"${t.pnl:+,.2f} | {t.entry_time} → {t.exit_time}"
            )
    return "\n".join(lines)
