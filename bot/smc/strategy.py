from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from .fvg import detect_fvg, price_in_fvg
from .liquidity import detect_liquidity_pools, recent_sweep
from .order_blocks import detect_order_blocks, price_in_order_block
from .structure import (
    Trend,
    detect_structure_breaks,
    detect_trend,
    find_swing_points,
    is_in_discount,
    is_in_premium,
    premium_discount_zone,
)
from .supply_demand import detect_supply_demand_zones, nearest_zone


class SignalType(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass
class Signal:
    type: SignalType
    entry: float
    stop_loss: float
    take_profit: float
    reason: str
    confidence: float  # 0.0 - 1.0


class SMCStrategy:
    """
    Smart Money Concepts confluence strategy.

    Long setup:
      1. HTF bullish trend or recent bullish CHoCH
      2. Liquidity sweep of sell-side (equal lows)
      3. Price in discount zone
      4. Entry at bullish order block or FVG

    Short setup: mirror of long.
    """

    def __init__(
        self,
        swing_lookback: int = 5,
        order_block_lookback: int = 20,
        fvg_min_size_pct: float = 0.001,
        liquidity_tolerance_pct: float = 0.0005,
        reward_risk_ratio: float = 2.0,
        stop_loss_pct: float | None = None,
    ):
        self.swing_lookback = swing_lookback
        self.order_block_lookback = order_block_lookback
        self.fvg_min_size_pct = fvg_min_size_pct
        self.liquidity_tolerance_pct = liquidity_tolerance_pct
        self.reward_risk_ratio = reward_risk_ratio
        # At explicit user request: a fixed stop distance from entry,
        # replacing the order-block/FVG-boundary invalidation stop below.
        # None (default) keeps the original structural-stop behavior —
        # only live callers that opt in (config.yaml's stop_loss_pct) get
        # the fixed-percentage stop; backtests/scan.py/market_snapshot.py's
        # informational second-opinion strategy are untouched.
        self.stop_loss_pct = stop_loss_pct

    def analyze(self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> Signal:
        if len(df) < 50:
            return self._no_signal("Insufficient data")

        price = float(df.iloc[-1]["close"])

        # Market structure
        swings = find_swing_points(df, self.swing_lookback)
        trend = detect_trend(swings)
        events = detect_structure_breaks(df, swings)
        zone = premium_discount_zone(df, swings)

        # HTF bias
        htf_trend = trend
        if htf_df is not None and len(htf_df) >= 20:
            htf_swings = find_swing_points(htf_df, self.swing_lookback)
            htf_trend = detect_trend(htf_swings)

        # SMC components
        order_blocks = detect_order_blocks(df, self.order_block_lookback)
        fvgs = detect_fvg(df, self.fvg_min_size_pct)
        pools = detect_liquidity_pools(df, self.liquidity_tolerance_pct)
        sweep = recent_sweep(pools, df, bars=5)
        sd_zones = detect_supply_demand_zones(df, swings)

        long_signal = self._check_long(
            price, trend, htf_trend, events, zone, order_blocks, fvgs, sweep, sd_zones, df
        )
        if long_signal.type != SignalType.NONE:
            return long_signal

        short_signal = self._check_short(
            price, trend, htf_trend, events, zone, order_blocks, fvgs, sweep, sd_zones, df
        )
        if short_signal.type != SignalType.NONE:
            return short_signal

        # No confluence — explain WHY, so a "no setup" is diagnostic, not opaque.
        return self._no_signal(self._diagnose(price, trend, htf_trend, events, zone, sweep, sd_zones))

    def _check_long(self, price, trend, htf_trend, events, zone, obs, fvgs, sweep, sd_zones, df):
        score = 0.0
        reasons: list[str] = []

        if htf_trend == Trend.BULLISH:
            score += 0.25
            reasons.append("HTF bullish")

        if trend == Trend.BULLISH:
            score += 0.15
            reasons.append("LTF bullish structure")

        recent_bullish = [e for e in events[-3:] if e.direction == "bullish"]
        if recent_bullish:
            score += 0.15
            reasons.append(recent_bullish[-1].kind.upper())

        if sweep and sweep.kind == "sell_side":
            score += 0.2
            reasons.append("Sell-side liquidity swept")

        if is_in_discount(price, zone):
            score += 0.15
            reasons.append("Discount zone")

        if nearest_zone(price, sd_zones, "demand") is not None:
            score += 0.15
            reasons.append("Demand zone")

        entry_zone = None
        for ob in reversed(obs):
            if ob.direction == "bullish" and price_in_order_block(price, ob):
                entry_zone = ob
                score += 0.15
                reasons.append("Bullish order block")
                break

        if entry_zone is None:
            for fvg in reversed(fvgs):
                if fvg.direction == "bullish" and price_in_fvg(price, fvg):
                    entry_zone = fvg
                    score += 0.1
                    reasons.append("Bullish FVG")
                    break

        if score < 0.55 or entry_zone is None:
            return self._no_signal("Long confluence insufficient")

        if self.stop_loss_pct is not None:
            stop = price * (1 - self.stop_loss_pct)
        elif hasattr(entry_zone, "bottom"):
            stop = entry_zone.bottom * 0.999
        else:
            stop = price * 0.985

        risk = price - stop
        tp = price + risk * self.reward_risk_ratio

        return Signal(
            type=SignalType.LONG,
            entry=price,
            stop_loss=stop,
            take_profit=tp,
            reason=" + ".join(reasons),
            confidence=min(score, 1.0),
        )

    def _check_short(self, price, trend, htf_trend, events, zone, obs, fvgs, sweep, sd_zones, df):
        score = 0.0
        reasons: list[str] = []

        if htf_trend == Trend.BEARISH:
            score += 0.25
            reasons.append("HTF bearish")

        if trend == Trend.BEARISH:
            score += 0.15
            reasons.append("LTF bearish structure")

        recent_bearish = [e for e in events[-3:] if e.direction == "bearish"]
        if recent_bearish:
            score += 0.15
            reasons.append(recent_bearish[-1].kind.upper())

        if sweep and sweep.kind == "buy_side":
            score += 0.2
            reasons.append("Buy-side liquidity swept")

        if is_in_premium(price, zone):
            score += 0.15
            reasons.append("Premium zone")

        if nearest_zone(price, sd_zones, "supply") is not None:
            score += 0.15
            reasons.append("Supply zone")

        entry_zone = None
        for ob in reversed(obs):
            if ob.direction == "bearish" and price_in_order_block(price, ob):
                entry_zone = ob
                score += 0.15
                reasons.append("Bearish order block")
                break

        if entry_zone is None:
            for fvg in reversed(fvgs):
                if fvg.direction == "bearish" and price_in_fvg(price, fvg):
                    entry_zone = fvg
                    score += 0.1
                    reasons.append("Bearish FVG")
                    break

        if score < 0.55 or entry_zone is None:
            return self._no_signal("Short confluence insufficient")

        if self.stop_loss_pct is not None:
            stop = price * (1 + self.stop_loss_pct)
        elif hasattr(entry_zone, "top"):
            stop = entry_zone.top * 1.001
        else:
            stop = price * 1.015

        risk = stop - price
        tp = price - risk * self.reward_risk_ratio

        return Signal(
            type=SignalType.SHORT,
            entry=price,
            stop_loss=stop,
            take_profit=tp,
            reason=" + ".join(reasons),
            confidence=min(score, 1.0),
        )

    def _diagnose(self, price, trend, htf_trend, events, zone, sweep, sd_zones) -> str:
        """Explain WHY there's no setup — the specific confluences that are
        missing right now, so a 'no setup' verdict is actionable rather than
        opaque. Reads the same market state _check_long/_check_short scored."""
        clues: list[str] = []

        # Trend / structure state
        if trend == Trend.NEUTRAL and htf_trend == Trend.NEUTRAL:
            clues.append("ranging (no clear trend)")
        elif trend == Trend.NEUTRAL:
            clues.append("choppy LTF structure")
        elif htf_trend != Trend.NEUTRAL and trend != Trend.NEUTRAL and htf_trend != trend:
            clues.append(f"HTF {htf_trend.value} vs LTF {trend.value} conflict")

        if not events:
            clues.append("no recent BOS/CHoCH")

        # Liquidity
        if sweep is None:
            clues.append("no liquidity sweep")

        # Premium/discount location
        low, eq, high = zone
        rng = high - low
        if low <= price <= eq:
            loc = "discount"
        elif eq <= price <= high:
            loc = "premium"
        else:
            loc = "outside range"
        if not (low <= price <= high):
            clues.append("price outside the dealing range")
        elif rng > 0 and abs(price - eq) / rng < 0.08:
            clues.append("price at equilibrium (mid-range)")

        # Supply / demand
        has_demand = any(z.kind == "demand" for z in sd_zones)
        has_supply = any(z.kind == "supply" for z in sd_zones)
        at_demand = nearest_zone(price, sd_zones, "demand") is not None
        at_supply = nearest_zone(price, sd_zones, "supply") is not None
        if not at_demand and not at_supply:
            if has_demand or has_supply:
                clues.append("price not at a supply/demand zone")
            else:
                clues.append("no supply/demand zones formed")

        if not clues:
            clues.append(f"confluence too weak in {loc}")
        return "No setup: " + ", ".join(clues)

    def _no_signal(self, reason: str) -> Signal:
        return Signal(
            type=SignalType.NONE,
            entry=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reason=reason,
            confidence=0.0,
        )
