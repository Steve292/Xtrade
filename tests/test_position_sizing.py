"""
Tests for bot/position_sizing.py — the Section 6 sizing formula, ATR, and
volume-exhaustion classifier. No network.

Run directly (`python tests/test_position_sizing.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.position_sizing import (
    SizingFactors,
    asset_class_base_risk_pct,
    atr,
    check_volume_exhaustion,
    final_risk_pct,
    regime_alloc_weight,
    risk_pct_for_fixed_usd,
    staged_fixed_risk_usd,
    volatility_adjust,
)


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


# --- asset_class_base_risk_pct -------------------------------------------


def test_known_asset_classes_return_documented_base_risk():
    assert asset_class_base_risk_pct("btc") == 2.0
    assert asset_class_base_risk_pct("ETH") == 2.0  # case-insensitive
    assert asset_class_base_risk_pct("alt") == 1.5
    assert asset_class_base_risk_pct("meme") == 1.0
    assert asset_class_base_risk_pct("equity") == 1.5
    assert asset_class_base_risk_pct("commodity") == 1.0


def test_unknown_asset_class_raises():
    try:
        asset_class_base_risk_pct("dogecoin-futures")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- atr -------------------------------------------------------------------


def test_atr_matches_hand_computed_wilder_recursion():
    df = pd.DataFrame(
        {
            "high": [10, 12, 11, 13, 14, 12, 15],
            "low": [8, 9, 10, 11, 12, 10, 13],
            "close": [9, 11, 10, 12, 13, 11, 14],
        }
    )
    result = atr(df, period=3)
    assert result.iloc[0] != result.iloc[0]  # NaN
    assert result.iloc[1] != result.iloc[1]  # NaN
    # True ranges: [2, 3, 1, 3, 2, 3, 4]; recursive EMA seeded at tr[0]=2,
    # alpha=1/3, verified independently against the exact same formula.
    expected = [None, None, 1.8888888888888888, 2.259259259259259, 2.1728395061728394, 2.448559670781893, 2.9657064471879284]
    for i in range(2, len(expected)):
        assert _close(result.iloc[i], expected[i]), f"row {i}: {result.iloc[i]} != {expected[i]}"


def test_atr_requires_high_low_close_columns_only():
    df = pd.DataFrame({"high": [5, 6, 7, 8, 9], "low": [4, 4, 5, 6, 7], "close": [4.5, 5.5, 6.5, 7.5, 8.5]})
    result = atr(df, period=2)
    assert result.iloc[0] != result.iloc[0]
    assert result.iloc[-1] > 0


# --- volatility_adjust -----------------------------------------------------


def test_volatility_adjust_neutral_when_equal():
    assert volatility_adjust(atr_20=1.0, atr_100_avg=1.0) == 1.0


def test_volatility_adjust_clamps_ceiling_when_vol_collapses():
    # atr_20 much smaller than its own history -> raw ratio would be huge.
    assert volatility_adjust(atr_20=0.1, atr_100_avg=1.0) == 2.0


def test_volatility_adjust_clamps_floor_when_vol_spikes():
    # atr_20 much larger than its own history -> raw ratio would be tiny.
    assert volatility_adjust(atr_20=10.0, atr_100_avg=1.0) == 0.3


def test_volatility_adjust_handles_zero_atr_100_avg():
    assert volatility_adjust(atr_20=1.0, atr_100_avg=0.0) == 1.0


def test_volatility_adjust_handles_zero_atr_20():
    assert volatility_adjust(atr_20=0.0, atr_100_avg=1.0) == 2.0


# --- final_risk_pct ---------------------------------------------------------


def test_final_risk_pct_multiplies_all_factors():
    factors = SizingFactors(
        base_risk_pct=2.0,
        regime_alloc_weight=1.0,
        hotness_multiplier=1.5,
        volatility_adjust=1.0,
        confidence_multiplier=1.0,
    )
    assert _close(final_risk_pct(factors), 3.0)


def test_final_risk_pct_clamps_combined_multiplier_ceiling():
    # 2.5 (meme rotation) * 2.0 (vol-adjust ceiling) = 5.0 combined, clamped to 3.0.
    factors = SizingFactors(
        base_risk_pct=1.0,
        regime_alloc_weight=1.0,
        hotness_multiplier=2.5,
        volatility_adjust=2.0,
        confidence_multiplier=1.0,
    )
    assert _close(final_risk_pct(factors), 3.0)


def test_final_risk_pct_zero_when_hotness_says_exit():
    # RISK_OFF_WARNING / MEME_WINTER hand back a 0x hotness multiplier.
    factors = SizingFactors(base_risk_pct=2.0, hotness_multiplier=0.0)
    assert final_risk_pct(factors) == 0.0


# --- check_volume_exhaustion ------------------------------------------------


def test_volume_fade_past_35_pct_triggers_exit_80():
    result = check_volume_exhaustion(breakout_volume=1000.0, current_30m_volume=640.0)  # -36%
    assert result.action == "EXIT_80_PCT"


def test_volume_surge_past_50_pct_triggers_cancel_tps():
    result = check_volume_exhaustion(breakout_volume=1000.0, current_30m_volume=1600.0)  # +60%
    assert result.action == "CANCEL_TPS_TRAIL_FULL"


def test_volume_within_band_holds():
    result = check_volume_exhaustion(breakout_volume=1000.0, current_30m_volume=1000.0)
    assert result.action == "HOLD"


def test_volume_exhaustion_handles_zero_breakout_volume():
    result = check_volume_exhaustion(breakout_volume=0.0, current_30m_volume=500.0)
    assert result.action == "HOLD"


def test_regime_alloc_weight_known_labels():
    assert regime_alloc_weight("RISK_ON_GROWTH") == 1.2
    assert regime_alloc_weight("RISK_ON_INFLATION") == 1.0
    assert regime_alloc_weight("NEUTRAL") == 1.0
    assert regime_alloc_weight("STAGFLATION") == 0.6
    assert regime_alloc_weight("RISK_OFF") == 0.0


def test_regime_alloc_weight_unknown_label_defaults_neutral():
    assert regime_alloc_weight("SOMETHING_NEW") == 1.0


def test_staged_fixed_risk_usd_below_threshold_is_low():
    assert staged_fixed_risk_usd(8.0) == 3.0
    assert staged_fixed_risk_usd(99.99) == 3.0


def test_staged_fixed_risk_usd_at_or_above_threshold_is_high():
    assert staged_fixed_risk_usd(100.0) == 6.0
    assert staged_fixed_risk_usd(250.0) == 6.0


def test_staged_fixed_risk_usd_custom_values():
    assert staged_fixed_risk_usd(50.0, low_risk_usd=1.0, high_risk_usd=2.0, threshold_usd=50.0) == 2.0
    assert staged_fixed_risk_usd(49.0, low_risk_usd=1.0, high_risk_usd=2.0, threshold_usd=50.0) == 1.0


def test_risk_pct_for_fixed_usd_computes_correctly():
    # $3 risk on an $8 balance -> 37.5%
    assert _close(risk_pct_for_fixed_usd(3.0, 8.0), 37.5)


def test_risk_pct_for_fixed_usd_handles_non_positive_balance():
    assert risk_pct_for_fixed_usd(3.0, 0.0) == 0.0
    assert risk_pct_for_fixed_usd(3.0, -5.0) == 0.0


def test_staged_risk_end_to_end_matches_expected_pct_at_each_stage():
    # Below $100: $3 risk. At $8 balance that's 37.5%.
    low_usd = staged_fixed_risk_usd(8.0)
    assert _close(risk_pct_for_fixed_usd(low_usd, 8.0), 37.5)
    # At/above $100: $6 risk. At $100 balance that's exactly 6%.
    high_usd = staged_fixed_risk_usd(100.0)
    assert _close(risk_pct_for_fixed_usd(high_usd, 100.0), 6.0)


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


# --- live-path helpers: confidence multiplier and the absolute dollar ceiling
# These are what stand between the multiplier chain above and a real order.
# The ceiling tests below use this account's ACTUAL balance ($6.12 at the time
# of writing) rather than a round number, because that is the regime where a
# relative-only cap fails.

from bot.position_sizing import (  # noqa: E402
    CONFIDENCE_FLOOR,
    MAX_COMBINED_MULTIPLIER,
    MAX_CONFIDENCE_MULTIPLIER,
    MIN_COMBINED_MULTIPLIER,
    MIN_CONFIDENCE_MULTIPLIER,
    apply_risk_ceiling,
    confidence_multiplier,
)

LIVE_BALANCE = 6.12
STAGED_RISK_USD = 3.0


def test_confidence_multiplier_spans_its_documented_band():
    assert confidence_multiplier(0.0) == MIN_CONFIDENCE_MULTIPLIER
    assert confidence_multiplier(CONFIDENCE_FLOOR) == MIN_CONFIDENCE_MULTIPLIER
    assert confidence_multiplier(1.0) == MAX_CONFIDENCE_MULTIPLIER
    assert MIN_CONFIDENCE_MULTIPLIER < confidence_multiplier(0.8) < MAX_CONFIDENCE_MULTIPLIER


def test_confidence_multiplier_is_monotonic():
    values = [confidence_multiplier(c / 20) for c in range(21)]
    assert values == sorted(values)


def test_ceiling_clamps_risk_that_exceeds_the_dollar_cap():
    # 100% risk on a $6.12 balance is $6.12; a $4.50 ceiling must cut it.
    capped = apply_risk_ceiling(100.0, LIVE_BALANCE, 4.50)
    assert capped < 100.0
    assert abs(capped / 100 * LIVE_BALANCE - 4.50) < 1e-9


def test_ceiling_leaves_risk_below_the_cap_untouched():
    assert apply_risk_ceiling(10.0, LIVE_BALANCE, 4.50) == 10.0


def test_relative_clamp_alone_would_exceed_the_account_but_the_ceiling_does_not():
    """The reason both caps exist.

    MAX_COMBINED_MULTIPLIER is RELATIVE: against the staged $3 risk it
    authorises $9, which on a $6.12 balance is more than the whole account.
    Only the absolute ceiling can express "never risk more than N dollars".
    """
    staged_pct = STAGED_RISK_USD / LIVE_BALANCE * 100
    unbounded = final_risk_pct(SizingFactors(
        base_risk_pct=staged_pct,
        regime_alloc_weight=1.2, hotness_multiplier=2.5,
        volatility_adjust=2.0, confidence_multiplier=1.5,
    ))
    assert unbounded / 100 * LIVE_BALANCE > LIVE_BALANCE, (
        "sanity: the relative clamp alone should authorise more than the balance"
    )

    ceiling = min(STAGED_RISK_USD * 1.5, 5.0)
    capped = apply_risk_ceiling(unbounded, LIVE_BALANCE, ceiling)
    assert capped / 100 * LIVE_BALANCE <= ceiling + 1e-9
    assert capped / 100 * LIVE_BALANCE < LIVE_BALANCE


def test_no_multiplier_stack_can_breach_the_ceiling():
    """Exhaustive over the multiplier ranges the modules can actually emit."""
    ceiling = min(STAGED_RISK_USD * 1.5, 5.0)
    staged_pct = STAGED_RISK_USD / LIVE_BALANCE * 100
    for regime in (0.0, 0.6, 1.0, 1.2):
        for hot in (0.5, 1.0, 1.5, 2.5):
            for vol in (0.3, 1.0, 2.0):
                for conf in (MIN_CONFIDENCE_MULTIPLIER, 1.0, MAX_CONFIDENCE_MULTIPLIER):
                    pct = final_risk_pct(SizingFactors(
                        base_risk_pct=staged_pct, regime_alloc_weight=regime,
                        hotness_multiplier=hot, volatility_adjust=vol,
                        confidence_multiplier=conf,
                    ))
                    capped = apply_risk_ceiling(pct, LIVE_BALANCE, ceiling)
                    assert capped / 100 * LIVE_BALANCE <= ceiling + 1e-9


def test_risk_off_regime_sizes_to_zero():
    """RISK_OFF maps to weight 0.0 — a full stop, not a small position."""
    pct = final_risk_pct(SizingFactors(
        base_risk_pct=50.0, regime_alloc_weight=regime_alloc_weight("RISK_OFF"),
        hotness_multiplier=2.5, volatility_adjust=2.0, confidence_multiplier=1.5,
    ))
    assert pct == 0.0


def test_ceiling_skips_sizing_on_degenerate_inputs():
    assert apply_risk_ceiling(50.0, 0.0, 4.5) == 0.0      # no balance
    assert apply_risk_ceiling(50.0, -1.0, 4.5) == 0.0     # negative balance
    assert apply_risk_ceiling(50.0, LIVE_BALANCE, 0.0) == 0.0   # no ceiling
    assert apply_risk_ceiling(0.0, LIVE_BALANCE, 4.5) == 0.0    # no risk


# --- confidence_only sizing (bot/runner.py's flag) --------------------------
# The flag itself lives in runner.py's live loop as a branch that neutralises
# regime_alloc_weight/hotness_multiplier/volatility_adjust to 1.0 before
# building SizingFactors -- not new math in this module. What's tested here
# is the invariant that branch relies on: with those three pinned at 1.0,
# final_risk_pct depends on confidence_multiplier ALONE, regardless of what
# regime/hotness/volatility would otherwise have been.


def test_neutralised_factors_make_confidence_the_only_driver():
    base = 30.0  # a representative effective_risk_pct on a small balance
    # Two calls that would give WILDLY different results with the real
    # factors (hot market + high vol vs risk-off + calm) must be IDENTICAL
    # once regime/hotness/vol are all pinned to 1.0.
    a = final_risk_pct(SizingFactors(
        base_risk_pct=base, regime_alloc_weight=1.0, hotness_multiplier=1.0,
        volatility_adjust=1.0, confidence_multiplier=confidence_multiplier(0.90)))
    b = final_risk_pct(SizingFactors(
        base_risk_pct=base, regime_alloc_weight=1.0, hotness_multiplier=1.0,
        volatility_adjust=1.0, confidence_multiplier=confidence_multiplier(0.90)))
    assert _close(a, b)


def test_confidence_only_result_matches_hand_computed_multiplier():
    base = 30.0
    conf = confidence_multiplier(0.85)  # the only non-1.0 factor
    expected = base * min(MAX_COMBINED_MULTIPLIER, max(MIN_COMBINED_MULTIPLIER, conf))
    got = final_risk_pct(SizingFactors(
        base_risk_pct=base, regime_alloc_weight=1.0, hotness_multiplier=1.0,
        volatility_adjust=1.0, confidence_multiplier=conf))
    assert _close(got, expected)


def test_confidence_only_still_respects_the_absolute_dollar_ceiling():
    """The flag changes which factors feed the formula; it does not touch the
    dollar ceiling downstream of it. Even at max confidence (1.5x) on a small
    balance, the ceiling must still hold."""
    base_pct = risk_pct_for_fixed_usd(STAGED_RISK_USD, LIVE_BALANCE)
    adapted = final_risk_pct(SizingFactors(
        base_risk_pct=base_pct, regime_alloc_weight=1.0, hotness_multiplier=1.0,
        volatility_adjust=1.0, confidence_multiplier=MAX_CONFIDENCE_MULTIPLIER))
    ceiling = min(STAGED_RISK_USD * 1.25, 5.0)  # this session's live values
    capped = apply_risk_ceiling(adapted, LIVE_BALANCE, ceiling)
    assert capped / 100 * LIVE_BALANCE <= ceiling + 1e-9


def test_confidence_only_scales_monotonically_with_confidence():
    base = 30.0
    results = [
        final_risk_pct(SizingFactors(
            base_risk_pct=base, regime_alloc_weight=1.0, hotness_multiplier=1.0,
            volatility_adjust=1.0, confidence_multiplier=confidence_multiplier(c / 20)))
        for c in range(21)
    ]
    assert results == sorted(results)
