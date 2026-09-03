"""
Tests for bot/entry_rules.py — Section 5's asset-specific entry rules.
No network. A genuine "everything simultaneously aligns" BUY case needs a
fresh MACD crossover + non-overbought RSI + a golden cross all lining up at
once — a property of real market data, not something worth faking with a
toy series — so these tests verify the verdict/check-propagation LOGIC
precisely instead (verdict priority, None vs True/False, insufficient-data
handling), plus that a clearly bad (falling) series reaches NO_SETUP and
that missing external data never gets silently treated as pass/fail.

Run directly (`python tests/test_entry_rules.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from bot.entry_rules import (
    ALT_SL_PCT,
    MAJOR_SL_PCT,
    MAJOR_TRAIL_ATR_MULT,
    MEME_SL_PCT,
    RuleCheck,
    _check,
    _verdict_from_checks,
    evaluate_altcoin,
    evaluate_major,
    evaluate_meme,
)


def _trend_df(n: int, slope: float, start: float = 100.0, wiggle: float = 0.5) -> pd.DataFrame:
    t = np.arange(n)
    close = start + t * slope + np.sin(t / 5.0) * wiggle
    return pd.DataFrame(
        {"close": close, "open": close, "high": close + 1, "low": close - 1, "volume": [1000.0] * n}
    )


# --- _verdict_from_checks --------------------------------------------------


def test_verdict_all_true_is_buy():
    checks = [RuleCheck("a", True), RuleCheck("b", True)]
    assert _verdict_from_checks(checks) == "BUY"


def test_verdict_any_false_is_no_setup_even_with_a_true():
    checks = [RuleCheck("a", True), RuleCheck("b", False)]
    assert _verdict_from_checks(checks) == "NO_SETUP"


def test_verdict_any_none_without_false_is_incomplete():
    checks = [RuleCheck("a", True), RuleCheck("b", None)]
    assert _verdict_from_checks(checks) == "INCOMPLETE"


def test_verdict_false_takes_priority_over_none():
    checks = [RuleCheck("a", False), RuleCheck("b", None)]
    assert _verdict_from_checks(checks) == "NO_SETUP"


# --- _check ----------------------------------------------------------------


def test_check_returns_none_when_value_missing():
    result = _check("x", None, lambda v: v > 0)
    assert result.passed is None


def test_check_applies_predicate_when_value_present():
    assert _check("x", 5, lambda v: v > 0).passed is True
    assert _check("x", -5, lambda v: v > 0).passed is False


# --- evaluate_major ---------------------------------------------------------


def test_major_falling_series_is_no_setup():
    df = _trend_df(260, slope=-0.8, start=300)
    result = evaluate_major(df, df, funding_rate=None, cvd_1h_signal=None, ssr=None)
    assert result.verdict == "NO_SETUP"
    by_name = {c.name: c.passed for c in result.checks}
    assert by_name["golden_cross_50_over_200ema"] is False


def test_major_reports_none_for_unavailable_external_checks():
    df = _trend_df(260, slope=0.8)
    result = evaluate_major(df, df, funding_rate=None, cvd_1h_signal=None, ssr=None)
    by_name = {c.name: c.passed for c in result.checks}
    assert by_name["funding_rate_below_0.01pct"] is None
    assert by_name["cvd_1h_positive"] is None
    assert by_name["etf_7d_flow_positive"] is None  # no free source, always None unless supplied
    assert by_name["ssr_below_4"] is None


def test_major_reports_true_when_external_checks_are_favorable():
    df = _trend_df(260, slope=0.8)
    result = evaluate_major(df, df, funding_rate=0.00005, cvd_1h_signal="BUY", ssr=2.0, etf_7d_flow_usd=1.0)
    by_name = {c.name: c.passed for c in result.checks}
    assert by_name["funding_rate_below_0.01pct"] is True
    assert by_name["cvd_1h_positive"] is True
    assert by_name["etf_7d_flow_positive"] is True
    assert by_name["ssr_below_4"] is True


def test_major_insufficient_history_reports_none_not_false():
    df = _trend_df(260, slope=-0.8, start=300)
    short_df = df.tail(10).reset_index(drop=True)  # far short of a 200-bar EMA
    result = evaluate_major(short_df, short_df, funding_rate=0.00005, cvd_1h_signal="BUY", ssr=2.0, etf_7d_flow_usd=1.0)
    by_name = {c.name: c.passed for c in result.checks}
    assert by_name["price_above_rising_200ema"] is None
    assert by_name["golden_cross_50_over_200ema"] is None
    assert by_name["rsi14_between_50_and_80"] is None


def test_major_has_eight_checks_and_documented_risk_plan():
    df = _trend_df(260, slope=0.8)
    result = evaluate_major(df, df, funding_rate=None, cvd_1h_signal=None, ssr=None)
    assert len(result.checks) == 8
    assert result.stop_loss_pct == MAJOR_SL_PCT
    assert result.trail_atr_mult == MAJOR_TRAIL_ATR_MULT
    assert result.tp_plan == [(1.5, 0.5), (3.0, 0.3)]


# --- evaluate_meme -----------------------------------------------------------


def test_meme_falling_series_is_no_setup():
    df = _trend_df(60, slope=-0.8, start=300)
    result = evaluate_meme(df, meme_score=70.0, btc_24h_change_pct=2.0, volume_30d_avg=500.0)
    assert result.verdict == "NO_SETUP"


def test_meme_reports_none_for_whale_and_social_when_unsupplied():
    df = _trend_df(60, slope=0.8)
    result = evaluate_meme(df, meme_score=70.0, btc_24h_change_pct=2.0, volume_30d_avg=500.0)
    by_name = {c.name: c.passed for c in result.checks}
    assert by_name["whale_netflow_24h_above_500k"] is None
    assert by_name["social_mentions_up_20pct_organic"] is None


def test_meme_score_and_btc_change_checks_reflect_input():
    df = _trend_df(60, slope=0.8)
    passing = evaluate_meme(df, meme_score=70.0, btc_24h_change_pct=2.0, volume_30d_avg=500.0)
    failing = evaluate_meme(df, meme_score=40.0, btc_24h_change_pct=-1.0, volume_30d_avg=500.0)
    p = {c.name: c.passed for c in passing.checks}
    f = {c.name: c.passed for c in failing.checks}
    assert p["meme_score_above_60"] is True and f["meme_score_above_60"] is False
    assert p["btc_24h_change_above_1.5pct"] is True and f["btc_24h_change_above_1.5pct"] is False


def test_meme_has_six_checks_and_documented_risk_plan():
    df = _trend_df(60, slope=0.8)
    result = evaluate_meme(df, meme_score=None, btc_24h_change_pct=None, volume_30d_avg=None)
    assert len(result.checks) == 6
    assert result.stop_loss_pct == MEME_SL_PCT
    assert result.tp_plan == [(1.5, 0.5), (3.0, 0.3)]


# --- evaluate_altcoin --------------------------------------------------------


def test_altcoin_falling_series_is_no_setup():
    df = _trend_df(60, slope=-0.8, start=300)
    result = evaluate_altcoin(df, df, regime_score=70.0, btc_d_trend="falling")
    assert result.verdict == "NO_SETUP"


def test_altcoin_regime_check_needs_both_score_and_dominance_trend():
    df = _trend_df(60, slope=0.8)
    good = evaluate_altcoin(df, df, regime_score=70.0, btc_d_trend="falling")
    bad_trend = evaluate_altcoin(df, df, regime_score=70.0, btc_d_trend="rising")
    missing = evaluate_altcoin(df, df, regime_score=None, btc_d_trend="falling")
    g = {c.name: c.passed for c in good.checks}
    b = {c.name: c.passed for c in bad_trend.checks}
    m = {c.name: c.passed for c in missing.checks}
    assert g["btc_bullish_regime_and_dominance_not_rising"] is True
    assert b["btc_bullish_regime_and_dominance_not_rising"] is False
    assert m["btc_bullish_regime_and_dominance_not_rising"] is None


def test_altcoin_reports_none_for_exchange_outflow_when_unsupplied():
    df = _trend_df(60, slope=0.8)
    result = evaluate_altcoin(df, df, regime_score=70.0, btc_d_trend="falling")
    by_name = {c.name: c.passed for c in result.checks}
    assert by_name["exchange_outflow_accumulation"] is None


def test_altcoin_has_four_checks_and_documented_risk_plan():
    df = _trend_df(60, slope=0.8)
    result = evaluate_altcoin(df, df, regime_score=None, btc_d_trend=None)
    assert len(result.checks) == 4
    assert result.stop_loss_pct == ALT_SL_PCT
    assert result.tp_plan == [(1.5, 0.5), (3.0, 0.5)]
    assert result.trail_atr_mult is None  # blueprint doesn't specify one for alts


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
