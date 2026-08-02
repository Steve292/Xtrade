"""
Tests for bot/regime.py — the macro regime detection engine. No network,
pure scoring logic against plain floats.

Run directly (`python tests/test_regime.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.regime import RegimeInputs, score_regime

ALL_RISK_ON = RegimeInputs(
    yield_10y_4h_change_pct=0.0,
    dxy_24h_change_pct=0.1,
    vix_level=15.0,
    yield_curve_10y_3m=0.1,
    btc_24h_change_pct=3.0,
    etf_7d_net_flow_usd=1_000_000.0,
    stablecoin_ssr=2.0,
    exchange_reserve_7d_change_pct=-1.0,
)

ALL_RISK_OFF = RegimeInputs(
    yield_10y_4h_change_pct=0.5,
    dxy_24h_change_pct=1.0,
    vix_level=35.0,
    yield_curve_10y_3m=-0.5,
    btc_24h_change_pct=-5.0,
    etf_7d_net_flow_usd=-1_000_000.0,
    stablecoin_ssr=4.0,
    exchange_reserve_7d_change_pct=1.0,
)


def test_all_conditions_met_scores_100_and_growth_regime():
    result = score_regime(ALL_RISK_ON)
    assert result.score == 100.0
    assert result.regime == "RISK_ON_GROWTH"
    assert result.missing == []


def test_all_conditions_failed_scores_0_and_risk_off():
    result = score_regime(ALL_RISK_OFF)
    assert result.score == 0.0
    assert result.regime == "RISK_OFF"


def test_no_data_at_all_is_neutral_not_risk_off():
    result = score_regime(RegimeInputs())
    assert result.regime == "NEUTRAL"
    assert set(result.missing) == {
        "yield_10y_4h_change_pct",
        "dxy_24h_change_pct",
        "vix_level",
        "yield_curve_10y_3m",
        "btc_24h_change_pct",
        "etf_7d_net_flow_usd",
        "stablecoin_ssr",
        "exchange_reserve_7d_change_pct",
    }


def test_missing_factors_redistribute_weight_instead_of_scoring_zero():
    # Only VIX (15) and BTC (15) have data, both pass -> should still hit
    # 100, not 30/100=30, because the other 70 points of weight that have no
    # data get excluded from the denominator rather than counted as failed.
    inputs = RegimeInputs(vix_level=15.0, btc_24h_change_pct=3.0)
    result = score_regime(inputs)
    assert result.score == 100.0
    assert result.regime == "RISK_ON_GROWTH"
    assert len(result.missing) == 6


def test_partial_pass_scores_proportionally_to_available_weight():
    # Only VIX (weight 15, passes) and DXY (weight 15, fails) have data.
    inputs = RegimeInputs(vix_level=15.0, dxy_24h_change_pct=5.0)
    result = score_regime(inputs)
    assert result.score == 50.0  # 15 earned / 30 available * 100


def test_label_boundaries_match_the_blueprint_table():
    cases = [
        (100.0, "RISK_ON_GROWTH"),
        (70.0, "RISK_ON_GROWTH"),
        (69.9, "RISK_ON_INFLATION"),
        (60.0, "RISK_ON_INFLATION"),
        (59.9, "NEUTRAL"),
        (40.0, "NEUTRAL"),
        (39.9, "STAGFLATION"),
        (25.0, "STAGFLATION"),
        (24.9, "RISK_OFF"),
        (0.0, "RISK_OFF"),
    ]
    for score, expected in cases:
        from bot.regime import _label_for

        assert _label_for(score) == expected, f"{score} -> expected {expected}, got {_label_for(score)}"


def test_factors_dict_only_reports_available_factors():
    inputs = RegimeInputs(vix_level=15.0, btc_24h_change_pct=-5.0)
    result = score_regime(inputs)
    assert result.factors == {"vix_level": True, "btc_24h_change_pct": False}


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
