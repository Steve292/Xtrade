"""
Hyperliquid auto-trader.

Pulls candles for a coin, runs the SMC strategy top-down (LTF + HTF), then puts
the signal through the full TradeScreener (SMC + Fibonacci + top-down + risk +
sniper). ONLY an approved signal is sized and — in live mode — sent to the venue
as a real testnet long/short.

Safety: dry-run by default. It screens and prints the full breakdown but sends
no order unless `dry_run=False`, which also requires a funded (key-bearing)
client.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.screening import ScreenResult, TradeScreener
from bot.smc.strategy import Signal, SignalType, SMCStrategy


@dataclass
class TradePlan:
    coin: str
    side: str  # "long" | "short"
    usd: float
    leverage: int
    entry: float
    stop_loss: float
    take_profit: float
    risk_pct: float


class HyperliquidTrader:
    def __init__(
        self,
        client,
        strategy: SMCStrategy,
        screener: TradeScreener,
        risk_pct: float = 1.0,
        leverage: int = 3,
        max_notional_pct: float = 100.0,  # cap position notional at N% of buying power
    ):
        self.client = client
        self.strategy = strategy
        self.screener = screener
        self.risk_pct = risk_pct
        self.leverage = leverage
        self.max_notional_pct = max_notional_pct

    def _plan(self, coin: str, signal: Signal, account_value: float) -> TradePlan | None:
        risk_amount = account_value * (self.risk_pct / 100)
        stop_frac = abs(signal.entry - signal.stop_loss) / signal.entry
        if stop_frac <= 0:
            return None
        notional = risk_amount / stop_frac
        # Never exceed buying power (account_value * leverage); honour the $10 floor.
        buying_power = account_value * self.leverage * (self.max_notional_pct / 100)
        notional = min(notional, buying_power)
        if notional < 10:
            return None  # can't place a compliant order within the risk budget
        return TradePlan(
            coin=coin,
            side=signal.type.value,
            usd=round(notional, 2),
            leverage=self.leverage,
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            risk_pct=self.risk_pct,
        )

    def evaluate(self, coin: str, ltf: str, htf: str, account_value: float):
        """Return (signal, ScreenResult, TradePlan|None) without trading."""
        df = self.client.candles(coin, ltf, lookback_hours=72)
        htf_df = self.client.candles(coin, htf, lookback_hours=240)
        signal = self.strategy.analyze(df, htf_df)
        result = self.screener.screen(signal, df, htf_df)
        plan = self._plan(coin, signal, account_value) if result.approved else None
        return signal, result, plan

    def execute(self, plan: TradePlan):
        """Send the approved trade to the venue (real testnet order)."""
        if plan.side == "long":
            return self.client.long(plan.coin, plan.usd, leverage=plan.leverage)
        return self.client.short(plan.coin, plan.usd, leverage=plan.leverage)

    def run_once(self, coin: str, ltf: str, htf: str, account_value: float, dry_run: bool = True):
        signal, result, plan = self.evaluate(coin, ltf, htf, account_value)

        if signal.type == SignalType.NONE:
            print(f"[{coin}] no SMC setup — {signal.reason}")
            return None

        print(f"[{coin}] {signal.type.value.upper()} candidate ({signal.confidence:.0%}) "
              f"— {signal.reason}")
        print(f"  entry {signal.entry:.4g}  SL {signal.stop_loss:.4g}  TP {signal.take_profit:.4g}")
        print("  Screening:")
        print(result.table())

        if not result.approved:
            print("  -> not traded (failed screening)\n")
            return result

        print(f"  Plan: {plan.side.upper()} ${plan.usd} of {coin} at {plan.leverage}x "
              f"(risk {plan.risk_pct}% of ${account_value:,.2f})")
        if dry_run:
            print("  -> DRY RUN — no order sent (use --live on a funded wallet to fire)\n")
        else:
            print("  -> firing testnet order...")
            print("  ", self.execute(plan), "\n")
        return result
