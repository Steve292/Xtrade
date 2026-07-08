"""
Trade screener — the approval gate.

A signal from the SMC strategy is only allowed to trade if it clears EVERY one
of five checks:

  1. SMC confluence     — the strategy actually found a scored setup
  2. Top-down alignment — the higher timeframe bias does not oppose the trade
  3. Fibonacci (OTE)     — entry sits in the 0.618-0.786 golden pocket of the leg
  4. Risk management     — reward:risk clears the minimum and the stop is valid
  5. Sniper entry        — high confluence AND a tight invalidation AND in the OTE

The screener is venue-agnostic: it judges a signal + market context and returns
an approve/reject with a per-check breakdown, so the same gate protects any
execution venue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from bot.smc.fibonacci import ote_band, recent_leg
from bot.smc.strategy import Signal, SignalType
from bot.smc.structure import Trend, detect_trend, find_swing_points


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class ScreenResult:
    approved: bool
    direction: str
    checks: list[Check] = field(default_factory=list)

    def table(self) -> str:
        lines = []
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"    [{mark}] {c.name:<22} {c.detail}")
        verdict = "APPROVED" if self.approved else "REJECTED"
        lines.append(f"    => {verdict}")
        return "\n".join(lines)


@dataclass
class ScreenConfig:
    min_confidence: float = 0.55
    min_rr: float = 2.0
    sniper_confidence: float = 0.65
    max_stop_pct: float = 0.02  # tight invalidation for a sniper entry (2%)
    ote_low: float = 0.618
    ote_high: float = 0.786
    swing_lookback: int = 5

    @classmethod
    def from_dict(cls, d: dict) -> "ScreenConfig":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class TradeScreener:
    def __init__(self, config: ScreenConfig | None = None):
        self.cfg = config or ScreenConfig()

    def screen(self, signal: Signal, df: pd.DataFrame, htf_df: pd.DataFrame) -> ScreenResult:
        cfg = self.cfg
        if signal.type == SignalType.NONE:
            return ScreenResult(False, "none", [Check("SMC confluence", False, "no signal")])

        direction = signal.type.value  # "long" | "short"
        checks: list[Check] = []

        # 1. SMC confluence
        checks.append(Check(
            "SMC confluence",
            signal.confidence >= cfg.min_confidence,
            f"{signal.confidence:.0%} (min {cfg.min_confidence:.0%})",
        ))

        # 2. Top-down HTF alignment — HTF bias must not oppose the trade
        htf_trend = detect_trend(find_swing_points(htf_df, cfg.swing_lookback))
        opposing = (
            (direction == "long" and htf_trend == Trend.BEARISH)
            or (direction == "short" and htf_trend == Trend.BULLISH)
        )
        checks.append(Check(
            "Top-down alignment", not opposing, f"HTF {htf_trend.value}",
        ))

        # 3. Fibonacci OTE — entry inside the golden pocket of the recent leg
        swings = find_swing_points(df, cfg.swing_lookback)
        leg = recent_leg(swings, direction)
        if leg is None:
            checks.append(Check("Fibonacci OTE", False, "no clean leg"))
            in_zone = False
        else:
            lo, hi = ote_band(leg[0], leg[1], cfg.ote_low, cfg.ote_high)
            in_zone = lo <= signal.entry <= hi
            checks.append(Check(
                "Fibonacci OTE", in_zone,
                f"entry {signal.entry:.4g} vs pocket {lo:.4g}-{hi:.4g}",
            ))

        # 4. Risk management — reward:risk and a valid stop
        risk = abs(signal.entry - signal.stop_loss)
        reward = abs(signal.take_profit - signal.entry)
        rr = (reward / risk) if risk > 0 else 0.0
        checks.append(Check(
            "Risk/reward", rr >= cfg.min_rr, f"1:{rr:.2f} (min 1:{cfg.min_rr:g})",
        ))

        # 5. Sniper entry — high confluence AND tight invalidation AND in OTE
        stop_pct = (risk / signal.entry) if signal.entry else 1.0
        sniper = (
            signal.confidence >= cfg.sniper_confidence
            and stop_pct <= cfg.max_stop_pct
            and in_zone
        )
        checks.append(Check(
            "Sniper entry", sniper,
            f"conf {signal.confidence:.0%}, stop {stop_pct:.2%} (max {cfg.max_stop_pct:.0%})",
        ))

        approved = all(c.passed for c in checks)
        return ScreenResult(approved, direction, checks)
