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
from .breaker import active_breaker, detect_breakers
from .candles import rejection_bias
from .mitigation import detect_mitigation_blocks, nearest_unmitigated
from .volume_profile import build_volume_profile


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
    # Which detector modules actually contributed to this setup, as dotted
    # module paths ("bot.smc.order_blocks", ...). Optional and defaulted so
    # every existing keyword construction of Signal is unaffected.
    #
    # These are the SAME identifiers the ingested corpus already carries in
    # its concepts' `maps_to` field, which is what lets bot/knowledge.py line
    # a live setup up against the corpus without inventing a second taxonomy.
    # Populated from the reasons list rather than parsed back out of the
    # joined `reason` string, which would break the moment wording changed.
    detectors: tuple = ()


# Maps a reason fragment emitted by _check_long/_check_short (and by
# _apply_extended) to the module that produced it. Matched as a
# case-insensitive substring so "Bullish order block" and "Bearish order
# block" share one entry.
_REASON_MODULES = (
    ("order block", "bot.smc.order_blocks"),
    ("fvg", "bot.smc.fvg"),
    ("liquidity swept", "bot.smart_money"),
    ("demand zone", "bot.smc.supply_demand"),
    ("supply zone", "bot.smc.supply_demand"),
    ("discount zone", "bot.smc.structure"),
    ("premium zone", "bot.smc.structure"),
    ("structure", "bot.smc.structure"),
    ("bos", "bot.smc.structure"),
    ("choch", "bot.smc.structure"),
    ("htf", "bot.unified_screen"),
    ("breaker", "bot.smc.breaker"),
    ("mitigation", "bot.smc.mitigation"),
    ("value area", "bot.smc.volume_profile"),
    ("rejection", "bot.smc.candles"),
)


def _modules_for(reasons) -> tuple:
    """Detector modules implied by a reasons list, de-duplicated, order kept."""
    out: list[str] = []
    for r in reasons:
        low = r.lower()
        for fragment, module in _REASON_MODULES:
            if fragment in low and module not in out:
                out.append(module)
    return tuple(out)


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
        extended_detectors: bool = False,
        extended_max_adjust: float = 0.10,
        htf_neutral_credit: float = 0.0,
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
        # Extended detectors (breaker / mitigation / candles / volume profile),
        # config.yaml's smc.extended_detectors. OFF by default: with the flag
        # off analyze() returns bit-identical Signals to before these existed,
        # which tests/test_smc_strategy.py pins.
        #
        # They refine confidence AFTER the confluence gate below has already
        # decided there is a setup — they are deliberately NOT extra points in
        # _check_long/_check_short's score. Adding to that score would let a
        # new detector push a sub-threshold setup over the 0.55 entry bar and
        # manufacture trades that never existed. Refining afterwards can only
        # sharpen or dampen a setup that already qualified.
        #
        # Bounded by extended_max_adjust either way. The bound matters in the
        # UP direction specifically: confidence feeds screening.py's
        # min_confidence/sniper gates and unified_screen.py's final_pct, and
        # final_pct is what runner.py compares against auto_fire_pct to decide
        # whether a trade fires unattended. Unbounded upward drift here would
        # quietly raise the hands-off firing rate.
        self.extended_detectors = extended_detectors
        self.extended_max_adjust = extended_max_adjust
        # Confluence credit for a NEUTRAL HTF read, at explicit user request.
        # Default 0.0 preserves today's behaviour exactly: today, HTF neutral
        # earns nothing toward the +0.25 htf_trend bonus below -- the same as
        # an outright OPPOSING HTF earns nothing. That parity is real: neither
        # bullish nor bearish confirmed means the HTF layer offers no opinion,
        # which is worth less than a confirmed match but is not the same as
        # active disagreement.
        #
        # Clamped to [0, 0.25): capped below the full bullish/bearish bonus so
        # "no opinion" can never outscore a genuinely confirmed HTF trend --
        # that would make silence more valuable than confirmation, which is
        # backwards. This is a scoring ADDITION, not a gate being loosened:
        # TradeScreener's Top-down alignment check already passes on neutral
        # HTF (it only rejects a DIRECTLY OPPOSING one), so there was no
        # existing restriction on neutral to relax here.
        self.htf_neutral_credit = max(0.0, min(0.2499, htf_neutral_credit))

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
            return self._apply_extended(long_signal, price, df, swings, events)

        short_signal = self._check_short(
            price, trend, htf_trend, events, zone, order_blocks, fvgs, sweep, sd_zones, df
        )
        if short_signal.type != SignalType.NONE:
            return self._apply_extended(short_signal, price, df, swings, events)

        # No confluence — explain WHY, so a "no setup" is diagnostic, not opaque.
        return self._no_signal(self._diagnose(price, trend, htf_trend, events, zone, sweep, sd_zones))

    def _apply_extended(self, signal, price, df, swings, events):
        """Bounded confidence refinement from the extended detectors.

        Four independent reads, each scored in [-1, 1] FOR THE SIGNAL'S OWN
        DIRECTION (positive = agrees with the setup), averaged, then scaled by
        extended_max_adjust. Averaging rather than summing is what keeps one
        loud detector from dominating; the scale is what keeps the whole group
        from moving confidence more than the caller allowed.

        Returns the signal unchanged when the flag is off.
        """
        if not self.extended_detectors:
            return signal

        bullish = signal.type == SignalType.LONG
        contributions: list[float] = []
        reasons: list[str] = []

        # 1. Candle-level rejection over the last few bars.
        bias, strength = rejection_bias(df, bars=3)
        if bias == "bullish":
            contributions.append(strength if bullish else -strength)
            reasons.append("bullish rejection" if bullish else "bullish rejection against")
        elif bias == "bearish":
            contributions.append(-strength if bullish else strength)
            reasons.append("bearish rejection against" if bullish else "bearish rejection")
        else:
            contributions.append(0.0)

        # 2. Breaker block price is currently sitting in (flipped polarity).
        breakers = detect_breakers(df, self.order_block_lookback)
        with_side = active_breaker(price, breakers, "bullish" if bullish else "bearish")
        against_side = active_breaker(price, breakers, "bearish" if bullish else "bullish")
        if with_side is not None:
            contributions.append(1.0)
            reasons.append("breaker support" if bullish else "breaker resistance")
        elif against_side is not None:
            contributions.append(-1.0)
            reasons.append("opposing breaker")
        else:
            contributions.append(0.0)

        # 3. Unmitigated mitigation block at price.
        blocks = detect_mitigation_blocks(df, swings, events)
        with_block = nearest_unmitigated(price, blocks, "bullish" if bullish else "bearish")
        against_block = nearest_unmitigated(price, blocks, "bearish" if bullish else "bullish")
        if with_block is not None:
            contributions.append(1.0)
            reasons.append("mitigation block")
        elif against_block is not None:
            contributions.append(-1.0)
            reasons.append("opposing mitigation block")
        else:
            contributions.append(0.0)

        # 4. Position against the volume-profile value area. Entering at the
        # far edge of balance is the reaction level; entering through the
        # opposite edge is chasing a move that already happened.
        profile = build_volume_profile(df)
        if profile is None:
            contributions.append(0.0)
        elif bullish and price <= profile.value_area_low:
            contributions.append(1.0)
            reasons.append("below value area")
        elif bullish and price >= profile.value_area_high:
            contributions.append(-1.0)
            reasons.append("extended above value area")
        elif not bullish and price >= profile.value_area_high:
            contributions.append(1.0)
            reasons.append("above value area")
        elif not bullish and price <= profile.value_area_low:
            contributions.append(-1.0)
            reasons.append("extended below value area")
        else:
            contributions.append(0.0)

        net = sum(contributions) / len(contributions)
        adjust = max(-self.extended_max_adjust, min(self.extended_max_adjust, net * self.extended_max_adjust))
        if adjust == 0.0 and not reasons:
            return signal

        signal.confidence = max(0.0, min(1.0, signal.confidence + adjust))
        if reasons:
            signal.reason = f"{signal.reason} | ext {adjust:+.3f}: " + ", ".join(reasons)
            extra = tuple(m for m in _modules_for(reasons) if m not in signal.detectors)
            signal.detectors = tuple(signal.detectors) + extra
        return signal

    def _check_long(self, price, trend, htf_trend, events, zone, obs, fvgs, sweep, sd_zones, df):
        score = 0.0
        reasons: list[str] = []

        if htf_trend == Trend.BULLISH:
            score += 0.25
            reasons.append("HTF bullish")
        elif htf_trend == Trend.NEUTRAL and self.htf_neutral_credit > 0:
            score += self.htf_neutral_credit
            reasons.append(f"HTF neutral (+{self.htf_neutral_credit:.2f} partial credit)")

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
            detectors=_modules_for(reasons),
        )

    def _check_short(self, price, trend, htf_trend, events, zone, obs, fvgs, sweep, sd_zones, df):
        score = 0.0
        reasons: list[str] = []

        if htf_trend == Trend.BEARISH:
            score += 0.25
            reasons.append("HTF bearish")
        elif htf_trend == Trend.NEUTRAL and self.htf_neutral_credit > 0:
            score += self.htf_neutral_credit
            reasons.append(f"HTF neutral (+{self.htf_neutral_credit:.2f} partial credit)")

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
            detectors=_modules_for(reasons),
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
