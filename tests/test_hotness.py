"""
Tests for bot/hotness.py — the 4-factor hotness matrix + meme season score.
No network, pure logic against plain values.

Run directly (`python tests/test_hotness.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.hotness import DominanceTrend, MemeScoreInputs, _climate_action, detect_hotness, meme_season_score


# --- detect_hotness ----------------------------------------------------


def test_meme_rotation_active_all_three_conditions():
    trend = DominanceTrend(btc_d="falling", meme_share="rising", others_d="rising", stable_c="flat")
    result = detect_hotness(trend)
    assert result.signal == "MEME_ROTATION_ACTIVE"
    assert result.multiplier == 2.5
    assert result.confidence == 1.0


def test_alt_season_starting_when_others_d_not_rising():
    trend = DominanceTrend(btc_d="falling", meme_share="rising", others_d="falling", stable_c="flat")
    result = detect_hotness(trend)
    assert result.signal == "ALT_SEASON_STARTING"
    assert result.multiplier == 2.0


def test_meme_rotation_takes_priority_over_alt_season_ordering():
    # All 3 rotation conditions true -> must report the stronger signal, not
    # the weaker one whose 2 conditions are a subset of these same inputs.
    trend = DominanceTrend(btc_d="falling", meme_share="rising", others_d="rising")
    assert detect_hotness(trend).signal == "MEME_ROTATION_ACTIVE"


def test_risk_off_warning():
    trend = DominanceTrend(btc_d="rising", stable_c="rising")
    result = detect_hotness(trend)
    assert result.signal == "RISK_OFF_WARNING"
    assert result.multiplier == 0.0


def test_neutral_when_nothing_matches():
    trend = DominanceTrend(btc_d="flat", meme_share="flat", others_d="flat", stable_c="flat")
    result = detect_hotness(trend)
    assert result.signal == "NEUTRAL"
    assert result.multiplier == 1.0


def test_confidence_reflects_missing_trend_data():
    trend = DominanceTrend(btc_d="falling", meme_share="rising")  # others_d/stable_c still None
    result = detect_hotness(trend)
    assert result.confidence == 0.5


def test_all_none_is_neutral_with_zero_confidence():
    result = detect_hotness(DominanceTrend())
    assert result.signal == "NEUTRAL"
    assert result.confidence == 0.0


# --- meme_season_score ---------------------------------------------------


def test_fully_bullish_inputs_scores_100_peak_mania():
    inputs = MemeScoreInputs(
        meme_dominance_change_24h_pct=10.0,
        meme_top10_avg_return_7d_pct=50.0,
        btc_dominance_pct=35.0,
        others_dominance_change_24h_pct=5.0,
        stablecoin_dominance_pct=5.0,
    )
    result = meme_season_score(inputs)
    assert result.score == 100.0
    assert result.zone == "PEAK_MANIA"
    assert result.size_multiplier == 0.0
    assert result.missing == []


def test_fully_bearish_inputs_scores_0_meme_winter():
    inputs = MemeScoreInputs(
        meme_dominance_change_24h_pct=-10.0,
        meme_top10_avg_return_7d_pct=-50.0,
        btc_dominance_pct=65.0,
        others_dominance_change_24h_pct=-5.0,
        stablecoin_dominance_pct=20.0,
    )
    result = meme_season_score(inputs)
    assert result.score == 0.0
    assert result.zone == "MEME_WINTER"
    assert result.size_multiplier == 0.0


def test_climate_action_zone_boundaries():
    cases = [
        (100.0, "PEAK_MANIA", 0.0),
        (80.0, "PEAK_MANIA", 0.0),
        (79.9, "ACTIVE_SEASON", 1.8),
        (60.0, "ACTIVE_SEASON", 1.8),
        (59.9, "WARMING_UP", 1.3),
        (40.0, "WARMING_UP", 1.3),
        (39.9, "COOLING", 0.5),
        (20.0, "COOLING", 0.5),
        (19.9, "MEME_WINTER", 0.0),
        (0.0, "MEME_WINTER", 0.0),
    ]
    for score, expected_zone, expected_mult in cases:
        zone, _action, mult = _climate_action(score)
        assert zone == expected_zone and mult == expected_mult, f"{score} -> {zone}/{mult}"


def test_no_data_at_all_defaults_to_neutral_warming_up():
    result = meme_season_score(MemeScoreInputs())
    assert result.score == 50.0
    assert result.zone == "WARMING_UP"
    # All 6 components are missing: meme_dominance_change_24h_pct feeds both
    # meme_d_trend and meme_c_total_accel, so its absence counts twice.
    assert len(result.missing) == 6


def test_only_meme_dominance_change_populates_two_components():
    # meme_dominance_change_24h_pct feeds BOTH meme_d_trend (25) and
    # meme_c_total_accel (10) -- the only field shared by two components.
    inputs = MemeScoreInputs(meme_dominance_change_24h_pct=10.0)
    result = meme_season_score(inputs)
    assert result.score == 100.0  # both populated components score 100
    assert set(result.missing) == {
        "meme_c_momentum",
        "btc_d_inverse",
        "others_d_trend",
        "stable_c_inverse",
    }


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
