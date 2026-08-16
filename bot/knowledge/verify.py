"""Test what the corpus claims against actual price data.

This is the half that was missing. Everything else in bot/knowledge/ measures
how WIDELY something is taught. Nothing measured whether it WORKS. Those are
different questions, and conflating them is how a system ends up with a profit
factor of 1.022 built entirely from consensus advice.

So: take a rule candidate or an optional screening gate, run the existing
backtester twice -- once as things stand, once with the change -- and report the
delta. "143 of 157 educators emphasise mitigation" becomes "requiring
mitigation took profit factor from 1.02 to 1.31 across 214 trades", or "took it
to 0.94", or most often "left 11 trades, which proves nothing".

THREE RULES THIS MODULE REFUSES TO BEND, because each is a standard way to
fool yourself with a backtest:

1. A minimum trade count. Below it the verdict is INCONCLUSIVE, never
   "improved". A gate that filters 200 trades down to 8 winners has not been
   validated; it has been overfitted, and the difference is invisible if you
   only read the profit factor.

2. Filters cut BOTH ways. A stricter gate removes losers and winners alike.
   Fewer trades at a higher win rate is not automatically better -- it can mean
   less total return with more idle capital, so total return and trade count
   are always reported next to the win rate.

3. It reports, it does not apply. Same boundary as the rest of this package:
   a measured improvement still prints an edit for a human to make.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

# The floor below which a backtest comparison is not evidence. Chosen to match
# the guidance the corpus research itself surfaced: profit factor needs ~100
# trades to be a signal at all and ~400 to be reliable. 30 is generous already.
MIN_TRADES_FOR_A_VERDICT = 30


@dataclass
class Metrics:
    trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    total_return_pct: float

    @classmethod
    def of(cls, result) -> "Metrics":
        return cls(
            trades=len(result.trades),
            win_rate=result.win_rate,
            profit_factor=result.profit_factor,
            max_drawdown_pct=result.max_drawdown_pct,
            total_return_pct=result.total_return_pct,
        )

    def line(self) -> str:
        return (f"{self.trades:>4} trades  PF {self.profit_factor:>5.3f}  "
                f"win {self.win_rate:>5.1f}%  ret {self.total_return_pct:>7.2f}%  "
                f"maxDD {self.max_drawdown_pct:>5.1f}%")


@dataclass
class Comparison:
    label: str
    baseline: Metrics
    variant: Metrics
    verdict: str          # improved | degraded | neutral | inconclusive
    note: str = ""

    @property
    def pf_delta(self) -> float:
        return self.variant.profit_factor - self.baseline.profit_factor

    def report(self) -> str:
        return "\n".join([
            f"  {self.label}",
            f"    baseline  {self.baseline.line()}",
            f"    variant   {self.variant.line()}",
            f"    verdict   {self.verdict.upper()}  (PF {self.pf_delta:+.3f})"
            + (f"  -- {self.note}" if self.note else ""),
        ])


def _judge(baseline: Metrics, variant: Metrics,
           min_trades: int = MIN_TRADES_FOR_A_VERDICT) -> tuple:
    if variant.trades < min_trades or baseline.trades < min_trades:
        return ("inconclusive",
                f"needs >={min_trades} trades on both sides "
                f"(got {baseline.trades} vs {variant.trades})")
    delta = variant.profit_factor - baseline.profit_factor
    # A gate that removes most of the sample is suspect even when the surviving
    # profit factor looks better -- that is the classic overfit signature.
    if variant.trades < baseline.trades * 0.25:
        return ("inconclusive",
                f"variant kept only {variant.trades}/{baseline.trades} trades; "
                "too selective to trust")
    if delta > 0.05:
        return ("improved", "")
    if delta < -0.05:
        return ("degraded", "")
    return ("neutral", "difference is within noise")


def compare_screen_gate(df: pd.DataFrame,
                        htf_df: Optional[pd.DataFrame],
                        gate: str,
                        strategy_kwargs: Optional[dict] = None,
                        screen_kwargs: Optional[dict] = None,
                        initial_balance: float = 10000.0,
                        risk_pct: float = 1.0,
                        min_trades: int = MIN_TRADES_FOR_A_VERDICT) -> Comparison:
    """Backtest with `gate` off, then on. `gate` is a ScreenConfig flag name."""
    from bot.backtest.engine import BacktestEngine
    from bot.screening import ScreenConfig, TradeScreener
    from bot.smc.strategy import SMCStrategy

    strategy_kwargs = dict(strategy_kwargs or {})
    screen_kwargs = dict(screen_kwargs or {})
    if gate not in ScreenConfig.__dataclass_fields__:
        raise ValueError(f"{gate!r} is not a ScreenConfig field")

    def _run(flags: dict) -> Metrics:
        engine = BacktestEngine(SMCStrategy(**strategy_kwargs),
                                initial_balance=initial_balance, risk_pct=risk_pct)
        cfg = ScreenConfig(**{**screen_kwargs, **flags})
        return Metrics.of(engine.run(df, htf_df, screener=TradeScreener(cfg)))

    # Baseline is the SEVEN-gate screen, not "no screen at all". The question is
    # what this gate adds to the system as it actually runs, and comparing
    # against an unscreened strategy would credit the gate with the other seven
    # gates' work.
    baseline = _run({gate: False})
    variant = _run({gate: True})
    verdict, note = _judge(baseline, variant, min_trades)
    return Comparison(f"gate: {gate}", baseline, variant, verdict, note)


def compare_param(df: pd.DataFrame,
                  htf_df: Optional[pd.DataFrame],
                  param: str,
                  proposed: float,
                  strategy_kwargs: Optional[dict] = None,
                  screen_kwargs: Optional[dict] = None,
                  initial_balance: float = 10000.0,
                  risk_pct: float = 1.0,
                  min_trades: int = MIN_TRADES_FOR_A_VERDICT) -> Comparison:
    """Backtest the current value of `param` against a proposed one."""
    from bot.backtest.engine import BacktestEngine
    from bot.screening import ScreenConfig, TradeScreener
    from bot.smc.strategy import SMCStrategy

    strategy_kwargs = dict(strategy_kwargs or {})
    screen_kwargs = dict(screen_kwargs or {})
    in_screen = param in ScreenConfig.__dataclass_fields__

    def _run(value_override: Optional[dict]) -> Metrics:
        sk = dict(strategy_kwargs)
        ck = dict(screen_kwargs)
        if value_override:
            (ck if in_screen else sk).update(value_override)
        engine = BacktestEngine(SMCStrategy(**sk),
                                initial_balance=initial_balance, risk_pct=risk_pct)
        return Metrics.of(engine.run(df, htf_df,
                                     screener=TradeScreener(ScreenConfig(**ck))))

    baseline = _run(None)
    variant = _run({param: proposed})
    verdict, note = _judge(baseline, variant, min_trades)
    current = (ScreenConfig().__dict__.get(param) if in_screen
               else strategy_kwargs.get(param, "default"))
    return Comparison(f"param: {param} {current} -> {proposed:g}",
                      baseline, variant, verdict, note)


def format_summary(comparisons: list) -> str:
    """A ranked digest. Inconclusive results are shown, not hidden.

    Dropping them would leave a page of apparent wins and no sense of how many
    questions the data could not answer -- which is itself the most important
    finding when a corpus is large and a price history is short.
    """
    if not comparisons:
        return "  nothing verified"
    order = {"improved": 0, "degraded": 1, "neutral": 2, "inconclusive": 3}
    rows = sorted(comparisons, key=lambda c: (order.get(c.verdict, 9), -c.pf_delta))
    out = [c.report() for c in rows]
    counts: dict = {}
    for c in comparisons:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    out.append("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    out.append("  Measured on YOUR price history. Nothing here changed config.yaml.")
    return "\n".join(out)
