"""
Tests for SMCStrategy's stop_loss_pct override — the fixed stop-loss
distance from entry that REPLACES the order-block/FVG-boundary invalidation
stop, at explicit user request (paired with bot/screening.py's raised
max_stop_pct so the Sniper-entry gate can still pass it). No network.

Calls the private _check_long/_check_short directly with hand-built SMC
component objects (OrderBlock, LiquidityPool, SupplyDemandZone, etc.) rather
than driving the full raw-OHLCV detection pipeline (find_swing_points,
detect_order_blocks, ...) — those are already covered by their own test
files; this isolates the one thing that actually changed: how `stop` is
computed once a confluence + entry_zone already exist.

Run directly (`python tests/test_smc_strategy.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.smc.liquidity import LiquidityPool
from bot.smc.order_blocks import OrderBlock
from bot.smc.strategy import SignalType, SMCStrategy
from bot.smc.structure import StructureEvent, Trend, detect_trend, find_swing_points
from bot.smc.supply_demand import SupplyDemandZone

# A fully-confluent long: HTF+LTF bullish, a recent bullish BOS, a swept
# sell-side pool, price in the discount half of the range, inside both a
# demand zone and a bullish order block (entry_zone) spanning [90, 100].
LONG_ARGS = dict(
    price=95.0,
    trend=Trend.BULLISH,
    htf_trend=Trend.BULLISH,
    events=[StructureEvent(index=0, kind="bos", direction="bullish", level=99.0)],
    zone=(90.0, 100.0, 110.0),  # (range_low, equilibrium, range_high)
    obs=[OrderBlock(index=0, direction="bullish", top=100.0, bottom=90.0)],
    fvgs=[],
    sweep=LiquidityPool(index=0, kind="sell_side", level=94.0, swept=True),
    sd_zones=[SupplyDemandZone(index=0, kind="demand", top=100.0, bottom=90.0, strength=0.02)],
    df=None,  # unused by _check_long/_check_short
)

# Mirror image: a fully-confluent short, entry_zone spanning [100, 110].
SHORT_ARGS = dict(
    price=105.0,
    trend=Trend.BEARISH,
    htf_trend=Trend.BEARISH,
    events=[StructureEvent(index=0, kind="bos", direction="bearish", level=101.0)],
    zone=(90.0, 100.0, 110.0),
    obs=[OrderBlock(index=0, direction="bearish", top=110.0, bottom=100.0)],
    fvgs=[],
    sweep=LiquidityPool(index=0, kind="buy_side", level=106.0, swept=True),
    sd_zones=[SupplyDemandZone(index=0, kind="supply", top=110.0, bottom=100.0, strength=0.02)],
    df=None,
)


def test_long_confluence_is_high_enough_to_confirm_the_fixture_is_valid():
    # Sanity check on the fixture itself, independent of stop_loss_pct.
    signal = SMCStrategy()._check_long(**LONG_ARGS)
    assert signal.type == SignalType.LONG
    assert signal.confidence >= 0.55


def test_short_confluence_is_high_enough_to_confirm_the_fixture_is_valid():
    signal = SMCStrategy()._check_short(**SHORT_ARGS)
    assert signal.type == SignalType.SHORT
    assert signal.confidence >= 0.55


def test_stop_loss_pct_none_keeps_structural_stop_for_long():
    # Regression guard: omitting stop_loss_pct must reproduce the original
    # order-block-boundary stop (entry_zone.bottom * 0.999), unchanged.
    signal = SMCStrategy()._check_long(**LONG_ARGS)
    assert abs(signal.stop_loss - 90.0 * 0.999) < 1e-9


def test_stop_loss_pct_none_keeps_structural_stop_for_short():
    signal = SMCStrategy()._check_short(**SHORT_ARGS)
    assert abs(signal.stop_loss - 110.0 * 1.001) < 1e-9


def test_stop_loss_pct_overrides_long_stop_to_fixed_pct():
    signal = SMCStrategy(stop_loss_pct=0.20)._check_long(**LONG_ARGS)
    assert signal.type == SignalType.LONG
    assert abs(signal.stop_loss - 95.0 * 0.80) < 1e-9


def test_stop_loss_pct_overrides_short_stop_to_fixed_pct():
    signal = SMCStrategy(stop_loss_pct=0.20)._check_short(**SHORT_ARGS)
    assert signal.type == SignalType.SHORT
    assert abs(signal.stop_loss - 105.0 * 1.20) < 1e-9


def test_stop_loss_pct_take_profit_still_scales_by_reward_risk_ratio():
    strat = SMCStrategy(stop_loss_pct=0.20, reward_risk_ratio=2.0)
    signal = strat._check_long(**LONG_ARGS)
    risk = signal.entry - signal.stop_loss
    assert abs(signal.take_profit - (signal.entry + risk * 2.0)) < 1e-9


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)


# --- extended detectors -----------------------------------------------------
# bot/smc/{breaker,mitigation,candles,volume_profile}.py refine confidence
# AFTER the confluence gate has already decided a setup exists. The two
# properties below are the safety contract for that.

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def _walk(seed: int, n: int = 220):
    rng = np.random.default_rng(seed)
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        c = price + rng.normal(0, 1.2)
        h = max(o, c) + abs(rng.normal(0, 0.7))
        l = min(o, c) - abs(rng.normal(0, 0.7))
        rows.append([o, h, l, c, abs(rng.normal(500, 150))])
        price = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_extended_detectors_are_off_by_default():
    assert SMCStrategy().extended_detectors is False


def test_extended_detectors_never_change_entry_stop_or_direction():
    """They refine CONFIDENCE only. If they could move the entry or the stop
    they would be changing the trade, not scoring it."""
    for seed in range(40):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        on = SMCStrategy(extended_detectors=True).analyze(df, htf)
        assert off.type == on.type
        assert off.entry == on.entry
        assert off.stop_loss == on.stop_loss
        assert off.take_profit == on.take_profit


def test_extended_detectors_cannot_manufacture_a_signal():
    """A setup the confluence gate rejected must stay rejected. The refinement
    happens after the gate precisely so a new detector can never push a
    sub-threshold setup over the entry bar."""
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        on = SMCStrategy(extended_detectors=True).analyze(df, htf)
        if off.type is SignalType.NONE:
            assert on.type is SignalType.NONE


def test_confidence_adjustment_respects_its_bound():
    for cap in (0.0, 0.05, 0.10, 0.25):
        for seed in range(30):
            df, htf = _walk(seed), _walk(seed + 9000, 120)
            off = SMCStrategy().analyze(df, htf)
            on = SMCStrategy(extended_detectors=True, extended_max_adjust=cap).analyze(df, htf)
            if off.type is SignalType.NONE:
                continue
            assert abs(on.confidence - off.confidence) <= cap + 1e-9


def test_confidence_stays_within_zero_to_one():
    for seed in range(40):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        on = SMCStrategy(extended_detectors=True, extended_max_adjust=0.5).analyze(df, htf)
        assert 0.0 <= on.confidence <= 1.0


def test_adjustment_moves_in_both_directions_across_a_sample():
    """A refinement that only ever raises confidence is a bias, not a signal —
    and raising it is the direction that increases unattended auto-fire."""
    ups = downs = 0
    for seed in range(120):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        if off.type is SignalType.NONE:
            continue
        on = SMCStrategy(extended_detectors=True).analyze(df, htf)
        delta = on.confidence - off.confidence
        if delta > 0:
            ups += 1
        elif delta < 0:
            downs += 1
    assert ups > 0 and downs > 0, f"adjustment was one-directional (up={ups}, down={downs})"


# --- detector reporting -----------------------------------------------------


def test_signals_report_the_detectors_that_produced_them():
    """Signal.detectors is the join key bot/knowledge.py scores against — it
    must carry the same dotted module names the corpus's `maps_to` uses."""
    found = False
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        sig = SMCStrategy().analyze(df, htf)
        if sig.type is SignalType.NONE:
            continue
        found = True
        assert sig.detectors, "a real setup reported no detectors"
        assert all(d.startswith("bot.") for d in sig.detectors)
        assert len(set(sig.detectors)) == len(sig.detectors), "duplicate detectors"
    assert found, "sample produced no signals — detector reporting went untested"


def test_no_signal_reports_no_detectors():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert SMCStrategy().analyze(empty).detectors == ()


# --- htf_neutral_credit: partial confluence credit for a NEUTRAL HTF read ---
# Not a gate loosened (TradeScreener's Top-down alignment already passes on
# neutral) -- a scoring addition. Neutral currently earns the SAME as an
# outright OPPOSING HTF: zero. Default 0.0 preserves that; a positive value
# gives neutral partial credit, capped below the full +0.25 confirmed-trend
# bonus so silence can never outscore confirmation.


def test_htf_neutral_credit_defaults_to_zero():
    assert SMCStrategy().htf_neutral_credit == 0.0


def test_htf_neutral_credit_is_clamped_below_the_confirmed_trend_bonus():
    """0.25 is the full bullish/bearish bonus. Neutral must never reach it --
    that would make 'no opinion' as valuable as a confirmed HTF trend."""
    assert SMCStrategy(htf_neutral_credit=0.25).htf_neutral_credit < 0.25
    assert SMCStrategy(htf_neutral_credit=1.0).htf_neutral_credit < 0.25
    assert SMCStrategy(htf_neutral_credit=-5.0).htf_neutral_credit == 0.0


def test_zero_credit_is_bit_identical_to_the_original_behaviour():
    """Flag-off parity: htf_neutral_credit=0.0 (the default) must reproduce
    exactly what the strategy did before this existed."""
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        explicit = SMCStrategy(htf_neutral_credit=0.0).analyze(df, htf)
        assert off.confidence == explicit.confidence
        assert off.type == explicit.type


def test_positive_credit_never_lowers_confidence():
    """Partial credit is strictly additive on a neutral HTF read -- it must
    never reduce a score relative to the zero-credit baseline."""
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        on = SMCStrategy(htf_neutral_credit=0.10).analyze(df, htf)
        if off.type is SignalType.NONE or on.type is SignalType.NONE:
            continue
        if off.type == on.type:
            assert on.confidence >= off.confidence - 1e-9


def test_credit_only_applies_when_htf_actually_reads_neutral():
    """A confirmed HTF trend must still score its full 0.25 -- the credit
    must not be added ON TOP of a real bullish/bearish match."""
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        hs = find_swing_points(htf, 5)
        if detect_trend(hs) == Trend.NEUTRAL:
            continue  # only care about bars where HTF is confirmed one way
        off = SMCStrategy().analyze(df, htf)
        on = SMCStrategy(htf_neutral_credit=0.20).analyze(df, htf)
        if off.type is SignalType.NONE or on.type is SignalType.NONE:
            continue
        if off.type == on.type:
            assert abs(on.confidence - off.confidence) < 1e-9, \
                "credit leaked into a bar with a CONFIRMED (non-neutral) HTF trend"


# --- min_confidence: the confluence-score floor that decides whether a ------
# Signal is emitted at all (SignalType.NONE vs LONG/SHORT). Used to be
# hardcoded 0.55 in _check_long/_check_short, independent of config.yaml's
# screening.min_confidence -- which TradeScreener's "SMC confluence" gate
# checks AGAIN, downstream, on whatever confidence this floor let through.
# Those two numbers happened to match (both 0.55) but nothing enforced it:
# lowering screening.min_confidence in config, expecting weaker setups to
# reach the screen for inspection, silently did nothing below 0.55 -- this
# floor had already refused to generate a signal. Now one config value
# drives both.


def test_min_confidence_defaults_to_the_prior_hardcoded_value():
    assert SMCStrategy().min_confidence == 0.55


def test_default_min_confidence_is_bit_identical_to_the_original_behaviour():
    """Flag-off parity, same shape as the htf_neutral_credit equivalent
    above: the new parameter existing at all must not change anything when
    left at its default."""
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        explicit = SMCStrategy(min_confidence=0.55).analyze(df, htf)
        assert off.confidence == explicit.confidence
        assert off.type == explicit.type


def test_a_fully_confluent_setup_clears_a_high_min_confidence():
    """LONG_ARGS/SHORT_ARGS above are hand-built to score ~1.0 (every
    confluence factor present) -- raising the floor short of that must still
    let it through."""
    strategy = SMCStrategy(min_confidence=0.9)
    assert strategy._check_long(**LONG_ARGS).type == SignalType.LONG
    assert strategy._check_short(**SHORT_ARGS).type == SignalType.SHORT


def test_min_confidence_above_the_max_possible_score_blocks_every_signal():
    """The gate (`score < self.min_confidence`) compares the RAW, unclamped
    score -- not signal.confidence, which is separately clamped to 1.0 only
    on the Signal that's actually returned. LONG_ARGS/SHORT_ARGS stack every
    confluence bonus at once (HTF + LTF + BOS + sweep + zone + entry_zone),
    which sums past 1.0 (1.20 here) -- so 1.01 does NOT block them, as an
    earlier version of this test wrongly assumed. 5.0 is comfortably above
    anything any realistic combination of bonuses could sum to."""
    strategy = SMCStrategy(min_confidence=5.0)
    assert strategy._check_long(**LONG_ARGS).type == SignalType.NONE
    assert strategy._check_short(**SHORT_ARGS).type == SignalType.NONE


def test_lowering_min_confidence_never_turns_a_signal_into_no_signal():
    """A lower floor is strictly more permissive in the one sense that's
    actually guaranteed: a bar that already qualified (long OR short) can
    never regress to NO signal at a lower floor. It does NOT guarantee the
    SAME type -- see the direction-flip test right below for why not."""
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        strict = SMCStrategy(min_confidence=0.55).analyze(df, htf)
        loose = SMCStrategy(min_confidence=0.20).analyze(df, htf)
        if strict.type is not SignalType.NONE:
            assert loose.type is not SignalType.NONE, (
                "a LOWER confluence floor turned an already-qualifying setup "
                "into NO signal -- a more permissive floor must never be "
                "more restrictive than a stricter one"
            )


def test_lowering_min_confidence_can_flip_long_to_short_by_priority_order():
    """Real, structural, worth having a named test for: analyze() checks
    LONG first and only evaluates SHORT if long_signal.type is NONE (see
    analyze()'s own body). On a bar where long scores just under the floor
    and short scores over it, raising min_confidence's floor does not just
    admit 'more, weaker' setups symmetrically -- lowering it can let LONG
    clear the bar first and pre-empt a SHORT that would otherwise have won.
    This is NOT a bug introduced by making min_confidence configurable; the
    priority order predates it. It was simply unobservable before, when
    0.55 was the only value that ever existed to compare against. Recorded
    here so a future change to analyze()'s dispatch order has to consciously
    break this test rather than silently changing live trading behaviour."""
    flipped = 0
    for seed in range(300):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        strict = SMCStrategy(min_confidence=0.55).analyze(df, htf)
        loose = SMCStrategy(min_confidence=0.20).analyze(df, htf)
        if (strict.type is SignalType.SHORT and loose.type is SignalType.LONG):
            flipped += 1
    assert flipped > 0, (
        "expected at least one seed in this sample to demonstrate the "
        "long-pre-empts-short priority flip -- if this now fails, either "
        "analyze()'s dispatch order changed (intentionally or not) or the "
        "sample is too small; widen the seed range before assuming it's fixed"
    )
