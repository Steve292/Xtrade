"""Abstention veto: refuse trades the SMC layer sees no setup for.

Why this exists
---------------
Replaying all 3,547 closed positions from the live Exness account
(161363546, 2025-11-25 .. 2026-08-14) through SMCStrategy.analyze() +
TradeScreener.screen() produced one result that survived out-of-sample
testing and several that did not.

    cohort                          trades      net        PF
    everything actually taken         3547   +10,020     1.022
    SMC saw a setup                    600   +43,879     1.538
    SMC saw NOTHING                   2947   -33,859     0.912

Split in half chronologically, the rule still holds on data it was not
chosen on -- including the second half, where taking everything LOST money
and the filtered set did not:

    half     take-everything PF     signal-only PF
    first          1.110                1.804
    second         0.943                1.172

Contrast "metals only", which looked better in aggregate (+12,785) and
FAILED out of sample (PF 1.243 then 0.906): it was chosen because metals had
won, which is hindsight. This module deliberately implements only the rule
that held.

What it does NOT claim
----------------------
The bot's DIRECTION was not predictive on the same data -- the cohort it
approved in the traded direction under-performed the baseline win rate by
9.9pp (z = -2.41). So this is a veto and nothing more: it uses the strategy's
abstention, never its opinion about which way to go. `allows()` can only ever
remove a trade; it cannot propose one.

Boundary
--------
Advisory, like bot/capital_guard.py and bot/entry_rules.py: it reports a
verdict and never places, modifies, or closes anything. Nothing here writes
config. It is not imported by the live execute path -- wiring it in is a
deliberate act, not a side effect of importing it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from bot.smc.strategy import SMCStrategy, SignalType

__all__ = ["Verdict", "ReviewStats", "allows", "review", "summarize"]


@dataclass
class Verdict:
    """The outcome of reviewing one proposed trade.

    `signal` carries the Signal that analyze() produced so a caller that acts
    on an ALLOW does not have to run the strategy a second time. It is None on
    every veto, which is what makes the veto unable to originate a trade: there
    is nothing to act on unless the strategy already said there was.
    """

    allowed: bool
    reason: str
    signal_type: str = "none"
    confidence: float = 0.0
    signal: object | None = None

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{'ALLOW' if self.allowed else 'VETO '}  {self.reason}"


def review(
    strategy: SMCStrategy,
    df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame] = None,
) -> Verdict:
    """Ask the SMC layer whether there is a setup here at all.

    Direction is reported but deliberately not judged -- see the module
    docstring for why the directional signal is excluded.
    """
    if df is None or len(df) == 0:
        return Verdict(False, "no market data")
    try:
        signal = strategy.analyze(df, htf_df)
    except Exception as exc:  # a detector fault must not silently allow a trade
        return Verdict(False, f"strategy error: {type(exc).__name__}: {exc}")

    if signal.type == SignalType.NONE:
        return Verdict(False, f"no SMC setup ({signal.reason})")
    return Verdict(
        True,
        f"SMC setup present ({signal.type.value}, {signal.confidence:.0%})",
        signal_type=signal.type.value,
        confidence=float(signal.confidence),
        signal=signal,
    )


def allows(
    strategy: SMCStrategy,
    df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame] = None,
) -> bool:
    """Convenience boolean form of :func:`review`."""
    return review(strategy, df, htf_df).allowed


@dataclass
class ReviewStats:
    """Aggregate outcome of applying the veto across a set of trades."""

    kept: int = 0
    vetoed: int = 0
    kept_pnl: float = 0.0
    vetoed_pnl: float = 0.0
    kept_wins: int = 0
    vetoed_wins: int = 0
    reasons: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.kept + self.vetoed

    @property
    def kept_win_rate(self) -> float:
        return (100.0 * self.kept_wins / self.kept) if self.kept else 0.0

    @property
    def vetoed_win_rate(self) -> float:
        return (100.0 * self.vetoed_wins / self.vetoed) if self.vetoed else 0.0

    @property
    def pnl_avoided(self) -> float:
        """P&L that the veto removed. Negative means it removed losses."""
        return self.vetoed_pnl

    def add(self, verdict: Verdict, pnl: float) -> None:
        if verdict.allowed:
            self.kept += 1
            self.kept_pnl += pnl
            self.kept_wins += 1 if pnl > 0 else 0
        else:
            self.vetoed += 1
            self.vetoed_pnl += pnl
            self.vetoed_wins += 1 if pnl > 0 else 0
            key = verdict.reason.split("(")[0].strip()
            self.reasons[key] = self.reasons.get(key, 0) + 1


def summarize(stats: ReviewStats) -> str:
    """Human-readable report. Reports, decides nothing."""
    if stats.total == 0:
        return "no trades reviewed"
    lines = [
        f"reviewed      : {stats.total}",
        f"kept          : {stats.kept} ({100*stats.kept/stats.total:.1f}%)  "
        f"P&L {stats.kept_pnl:+,.2f}  win {stats.kept_win_rate:.1f}%",
        f"vetoed        : {stats.vetoed} ({100*stats.vetoed/stats.total:.1f}%)  "
        f"P&L {stats.vetoed_pnl:+,.2f}  win {stats.vetoed_win_rate:.1f}%",
        f"net effect    : {-stats.vetoed_pnl:+,.2f} "
        f"({'losses avoided' if stats.vetoed_pnl < 0 else 'profit forgone'})",
    ]
    if stats.reasons:
        lines.append("veto reasons  :")
        for reason, n in sorted(stats.reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {n:>6}  {reason}")
    return "\n".join(lines)
