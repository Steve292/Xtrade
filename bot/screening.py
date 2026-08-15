"""
Trade screener — the approval gate.

A signal from the SMC strategy is only allowed to trade if it clears EVERY one
of seven checks:

  1. SMC confluence     — the strategy actually found a scored setup
  2. Top-down alignment — the higher timeframe bias does not oppose the trade
  3. Liquidity sweep     — a stop-hunt in the trade direction is confirmed
  4. Risk management     — reward:risk clears the minimum and the stop is valid
  5. Sniper entry        — high confluence AND the stop within max_stop_pct
                            (raised to accommodate a fixed 20% stop-loss —
                            see bot/smc/strategy.py's stop_loss_pct — no
                            longer "tight" by design, at explicit user
                            request)
  6. Supply/Demand       — entry sits at a demand (long) or supply (short) zone
  7. Fibonacci (OTE)     — FINAL gate: entry sits in the 0.618-0.786 golden pocket

Fibonacci runs last by design: a trade is only approved once everything else
holds AND price is at the optimal entry in the pocket.

The screener is venue-agnostic: it judges a signal + market context and returns
an approve/reject with a per-check breakdown, so the same gate protects any
execution venue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from bot.smc.fibonacci import ote_band, recent_leg
from bot.smc.liquidity import detect_liquidity_pools, recent_sweep
from bot.smc.strategy import Signal, SignalType
from bot.smc.structure import Trend, detect_trend, find_swing_points
from bot.smc.supply_demand import detect_supply_demand_zones, nearest_zone


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
    max_stop_pct: float = 0.25  # raised from 2% to fit the fixed 20% stop-loss + headroom
    ote_low: float = 0.618
    ote_high: float = 0.786
    swing_lookback: int = 5
    liquidity_tolerance_pct: float = 0.0005  # equal-high/low tolerance for pools
    sweep_bars: int = 20  # a liquidity sweep must be this recent to confirm

    # Optional gates 8-10. OFF by default: enabling one adds a check that can
    # REJECT trades the current seven-gate screen would have approved, which is
    # a real behaviour change on an armed account. Turn them on deliberately,
    # one at a time, and walk-forward the result before going live.
    require_mitigation: bool = False          # entry must sit in a respected zone
    require_breaker: bool = False             # entry must sit in a retested breaker
    require_candle_confirmation: bool = False # a candle must confirm the direction
    candle_lookback: int = 3
    candle_min_strength: float = 0.0
    require_wyckoff: bool = False             # range bias must agree with the trade
    require_value_area_edge: bool = False     # entry must sit at a value-area edge
    volume_profile_bins: int = 50

    # Gates 13-15, added after comparing gate coverage against what the
    # 329-document corpus actually emphasises. Each closes a hole where a
    # heavily-taught concept had no gate at all:
    #   premium/discount  4 channels, 164 videos, 866 mentions -- scored inside
    #                     SMCStrategy but never an independent veto
    #   take profit       4 channels, 263 videos, 2512 mentions -- the SECOND
    #                     most-discussed concept in the corpus, and gate 4 only
    #                     checked the RR ratio, never whether the target sits
    #                     at a level price might actually reach
    #   consensus         the weighted vote across all ten concepts
    require_premium_discount: bool = False
    require_target_at_level: bool = False
    target_tolerance_pct: float = 0.004
    # Minimum bot.smc.consensus score (-1..+1). None disables. This is the
    # "approve only what the ingested evidence supports" gate: weights come
    # from cross-channel breadth, so a setup carried by four-educator concepts
    # scores above one carried by single-source vocabulary.
    min_consensus_score: float | None = None

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

        # 3. Liquidity sweep — a stop-hunt in the trade direction must be confirmed
        #    (long needs sell-side liquidity swept; short needs buy-side swept)
        pools = detect_liquidity_pools(df, cfg.liquidity_tolerance_pct)
        sweep = recent_sweep(pools, df, bars=cfg.sweep_bars)
        want_side = "sell_side" if direction == "long" else "buy_side"
        swept = sweep is not None and sweep.kind == want_side
        checks.append(Check(
            "Liquidity sweep", swept,
            f"{sweep.kind if sweep else 'none'} (need {want_side})",
        ))

        # 4. Risk management — reward:risk and a valid stop
        risk = abs(signal.entry - signal.stop_loss)
        reward = abs(signal.take_profit - signal.entry)
        rr = (reward / risk) if risk > 0 else 0.0
        checks.append(Check(
            "Risk/reward", rr >= cfg.min_rr, f"1:{rr:.2f} (min 1:{cfg.min_rr:g})",
        ))

        # 5. Sniper entry — high confluence AND a tight invalidation
        stop_pct = (risk / signal.entry) if signal.entry else 1.0
        sniper = (
            signal.confidence >= cfg.sniper_confidence
            and stop_pct <= cfg.max_stop_pct
        )
        checks.append(Check(
            "Sniper entry", sniper,
            f"conf {signal.confidence:.0%}, stop {stop_pct:.2%} (max {cfg.max_stop_pct:.0%})",
        ))

        swings = find_swing_points(df, cfg.swing_lookback)

        # 6. Supply & Demand — the entry must sit at a genuine institutional
        #    zone: a demand zone (long) or supply zone (short) that a prior
        #    impulse originated from. Structural confirmation that price is at
        #    a level smart money is likely defending, not mid-air.
        sd_zones = detect_supply_demand_zones(df, swings)
        want_zone = "demand" if direction == "long" else "supply"
        at_zone = nearest_zone(signal.entry, sd_zones, want_zone) is not None
        n_zones = sum(1 for z in sd_zones if z.kind == want_zone)
        checks.append(Check(
            "Supply/Demand", at_zone,
            f"{'in ' + want_zone + ' zone' if at_zone else 'not at a ' + want_zone + ' zone'} "
            f"({n_zones} {want_zone} zone{'s' if n_zones != 1 else ''} active)",
        ))

        # 7. Fibonacci OTE — the FINAL gate: entry must be in the golden pocket
        #    of the recent leg. Runs last so a trade is only approved once every
        #    other condition holds AND price is at the optimal entry.
        leg = recent_leg(swings, direction)
        if leg is None:
            checks.append(Check("Fibonacci OTE (final)", False, "no clean leg"))
        else:
            lo, hi = ote_band(leg[0], leg[1], cfg.ote_low, cfg.ote_high)
            in_zone = lo <= signal.entry <= hi
            checks.append(Check(
                "Fibonacci OTE (final)", in_zone,
                f"entry {signal.entry:.4g} vs pocket {lo:.4g}-{hi:.4g}",
            ))

        # --- OPTIONAL gates 8-10, all OFF by default -------------------------
        #
        # These append NOTHING unless explicitly enabled, so `all(c.passed)` is
        # bit-for-bit unchanged for anyone who does not turn them on. Same
        # convention as bot/capital_guard.py's newer breakers, and the same
        # reason: both live accounts are armed against real money, so a new
        # gate must be opt-in rather than something that silently starts
        # rejecting or approving trades on the next restart.
        #
        # Each corresponds to a concept the ingested educator corpus leaned on
        # heavily while this codebase had no representation of it at all:
        # mitigation in 143 of 157 videos, breakers in 78, candle-close
        # confirmation in 49.

        if cfg.require_mitigation:
            from bot.smc.mitigation import active_mitigation
            m = active_mitigation(df, signal.entry, direction,
                                  lookback=cfg.swing_lookback * 4)
            checks.append(Check(
                "Mitigation", m is not None,
                f"zone {m.bottom:.4g}-{m.top:.4g} respected "
                f"(reaction {m.reaction_pct:.2%})" if m
                else "entry is not in a respected mitigation zone",
            ))

        if cfg.require_breaker:
            from bot.smc.breaker import active_breaker
            b = active_breaker(df, signal.entry, direction,
                               lookback=cfg.swing_lookback * 4)
            checks.append(Check(
                "Breaker", b is not None,
                f"flipped {b.origin_direction} zone {b.bottom:.4g}-{b.top:.4g}, "
                f"retested" if b
                else "entry is not in a retested breaker zone",
            ))

        if cfg.require_candle_confirmation:
            from bot.smc.candles import confirms
            p = confirms(df, direction, lookback=cfg.candle_lookback,
                         min_strength=cfg.candle_min_strength)
            checks.append(Check(
                "Candle confirmation", p is not None,
                f"{p.name} ({p.direction}, strength {p.strength:.2f})" if p
                else "no confirming candle pattern on the last "
                     f"{cfg.candle_lookback} bars",
            ))

        if cfg.require_wyckoff:
            from bot.smc.wyckoff import confirms as wyckoff_confirms
            w = wyckoff_confirms(df, direction)
            checks.append(Check(
                "Wyckoff", w is not None,
                f"{w.bias} range {w.trading_range.low:.4g}-{w.trading_range.high:.4g}, "
                f"{len(w.events)} event(s)" if w and w.trading_range
                else "no range, or bias does not agree with the trade",
            ))

        if cfg.require_value_area_edge:
            from bot.smc.volume_profile import at_value_area_edge
            edge = at_value_area_edge(df, signal.entry,
                                      bins=cfg.volume_profile_bins)
            # Long at the LOW edge, short at the HIGH edge. Entering long at the
            # top of the value area is buying the expensive end of fair value,
            # which is the opposite of what the range-deviation trades in the
            # source material do.
            want = "low" if direction == "long" else "high"
            checks.append(Check(
                "Value area edge", edge == want,
                f"entry at value-area {edge}" if edge
                else "entry is not at a value-area edge",
            ))

        if cfg.require_premium_discount:
            from bot.smc.structure import (is_in_discount, is_in_premium,
                                           premium_discount_zone)
            zone = premium_discount_zone(df, swings)
            if zone is None:
                checks.append(Check("Premium/Discount", False, "no clean range"))
            else:
                ok = (is_in_discount(signal.entry, zone) if direction == "long"
                      else is_in_premium(signal.entry, zone))
                side = "discount" if direction == "long" else "premium"
                checks.append(Check(
                    "Premium/Discount", ok,
                    f"entry {'in' if ok else 'NOT in'} {side} "
                    f"(eq {zone[1]:.4g})",
                ))

        if cfg.require_target_at_level:
            # Gate 4 checks the RR RATIO. It never checks that the target is a
            # place price might actually go. A 1:5 target floating in open air
            # is arithmetically excellent and practically unreachable -- which
            # is the same failure mode as the 20% fixed stop, one step later in
            # the trade.
            # NO local imports here. detect_liquidity_pools and
            # detect_supply_demand_zones are already imported at module scope,
            # and re-importing them inside this branch makes the names LOCAL to
            # screen() for its whole body -- which broke gate 3 with an
            # UnboundLocalError even when this gate was switched off. A local
            # import in one branch silently rebinds a module-level name
            # everywhere in the function.
            tol = signal.take_profit * cfg.target_tolerance_pct
            pools = detect_liquidity_pools(df, cfg.liquidity_tolerance_pct)
            zones = detect_supply_demand_zones(df, swings)
            near_pool = any(abs(p.level - signal.take_profit) <= tol for p in pools)
            near_zone = any(
                (z.bottom - tol) <= signal.take_profit <= (z.top + tol)
                for z in zones)
            hit = near_pool or near_zone
            checks.append(Check(
                "Target at a level", hit,
                ("target sits at " + ("liquidity" if near_pool else "a zone"))
                if hit else "target is in open air, not at any liquidity or zone",
            ))

        if cfg.min_consensus_score is not None:
            from bot.smc.consensus import evaluate as consensus_evaluate
            cr = consensus_evaluate(df, htf_df, direction, signal.entry)
            ok = (cr.verdict != "no_opinion"
                  and cr.score >= cfg.min_consensus_score)
            checks.append(Check(
                "Concept consensus", ok,
                f"{cr.verdict} score {cr.score:+.2f} "
                f"({cr.agreed}a/{cr.dissented}d/{cr.abstained}x, "
                f"min {cfg.min_consensus_score:+.2f})",
            ))

        approved = all(c.passed for c in checks)
        return ScreenResult(approved, direction, checks)
