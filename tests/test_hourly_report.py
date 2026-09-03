"""
Tests for bot/hourly_report.py — the prose formatter for the hourly SMC scan.

Pure formatting: every test builds a SymbolSnapshot by hand, no MT5 and no
network. Focus is the "Expectation" text, since that's the part with actual
logic (picking the right structural gap or failing gate to explain).

Run directly (`python tests/test_hourly_report.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.hourly_report import SymbolSnapshot, build_report, build_symbol_report


def _base(**kw) -> SymbolSnapshot:
    defaults = dict(
        symbol="BTCUSDc", price=78800.0, bar_time="2026-08-30 17:15:00",
        ltf_label="15m", htf_label="4h",
        ltf_trend="neutral", htf_trend="neutral",
        ltf_last_event=None, htf_last_event=None,
        sweep=None, ote_band=None, ote_direction=None,
        signal_type="none", signal_reason="No setup: ranging",
    )
    defaults.update(kw)
    return SymbolSnapshot(**defaults)


# --- no-signal expectation defers to the strategy's own diagnosis -----------
# An earlier version RE-DERIVED an explanation from a fixed priority order
# (HTF neutral -> LTF disagreement -> no sweep -> no zone), independent of
# what SMCStrategy actually computed. On a real bar where HTF read neutral but
# the true blocker was a missing supply/demand zone, it printed "HTF neutral
# blocks everything downstream" -- plausible, and wrong, because it never
# consulted signal_reason (SMCStrategy._diagnose's own text). Repeating that
# text verbatim is the fix: it cannot diverge from what the strategy computed,
# because it IS what the strategy computed.


def test_no_signal_expectation_is_exactly_the_strategys_own_reason():
    reason = "No setup: price outside the dealing range, no supply/demand zones formed"
    r = build_symbol_report(_base(htf_trend="neutral", ltf_trend="bullish", signal_reason=reason))
    assert reason in r.split("Expectation:")[1]


def test_no_signal_expectation_does_not_blame_neutral_htf_when_thats_not_the_reason():
    """Regression for the exact bug: HTF neutral must not be named as the
    blocker when the real diagnosis names something else entirely."""
    reason = "No setup: price outside the dealing range, no supply/demand zones formed"
    r = build_symbol_report(_base(htf_trend="neutral", ltf_trend="bullish", signal_reason=reason))
    expectation = r.split("Expectation:")[1]
    assert "blocks everything downstream" not in expectation
    assert "HTF" not in expectation or "neutral" not in expectation.lower()


def test_no_signal_expectation_tracks_whatever_the_strategy_actually_says():
    for reason in (
        "No setup: ranging (no clear trend)",
        "No setup: choppy LTF structure",
        "No setup: HTF bullish vs LTF bearish conflict",
    ):
        r = build_symbol_report(_base(signal_reason=reason))
        assert reason in r


# --- signal-present expectations point at the first failing gate ------------


def _signal(gates, **kw):
    kw.setdefault("signal_type", "long")
    kw.setdefault("confidence", 0.85)
    kw.setdefault("entry", 78800.0)
    kw.setdefault("stop", 78400.0)
    kw.setdefault("take_profit", 79600.0)
    kw.setdefault("gate_checks", gates)
    return _base(**kw)


def test_first_failing_gate_drives_the_expectation():
    gates = [
        ("SMC confluence", True, "85%"),
        ("Top-down alignment", True, "HTF bullish"),
        ("Liquidity sweep", False, "none (need sell_side)"),
        ("Fibonacci OTE (final)", False, "out of pocket"),
    ]
    r = build_symbol_report(_signal(gates))
    assert "Blocked on Liquidity sweep" in r
    assert "Fibonacci" not in r.split("Expectation:")[1], \
        "should name the FIRST failing gate, not a later one"


def test_all_gates_passed_but_below_auto_fire_says_queue():
    gates = [("SMC confluence", True, "85%")]
    r = build_symbol_report(_signal(gates, final_pct=72.0, auto_fire_pct=80.0, approved=True))
    assert "queue" in r.lower()
    assert "8.0 points" in r or "8.0" in r


def test_all_gates_passed_and_above_auto_fire_says_fires():
    gates = [("SMC confluence", True, "85%")]
    r = build_symbol_report(_signal(gates, final_pct=88.0, auto_fire_pct=80.0, approved=True))
    assert "FIRES unattended" in r


def test_unknown_gate_name_still_produces_readable_text():
    """A gate name not in the lookup table must not crash the formatter."""
    gates = [("Some New Gate", False, "detail here")]
    r = build_symbol_report(_signal(gates))
    assert "Some New Gate" in r


# --- structural display lines ------------------------------------------------


def test_ote_band_shows_distance_when_price_is_outside_it():
    snap = _base(price=78000.0, ote_band=(78900.0, 79000.0), ote_direction="bullish")
    r = build_symbol_report(snap)
    assert "below the pocket" in r


def test_ote_band_flags_when_price_is_inside_it():
    snap = _base(price=78950.0, ote_band=(78900.0, 79000.0), ote_direction="bullish")
    r = build_symbol_report(snap)
    assert "INSIDE the pocket" in r


def test_report_includes_every_symbol():
    a = _base(symbol="BTCUSDc")
    b = _base(symbol="XAUUSDc", price=4600.0)
    out = build_report([a, b], generated_at="2026-08-30 17:00")
    assert "BTCUSDc" in out and "XAUUSDc" in out
    assert "2026-08-30 17:00" in out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- candlestick-only cross-timeframe section --------------------------------
# Independent of the SMC confluence stack -- no zones/sweeps/fib. Reports
# whether the LTF and HTF candlestick geometry actually agree.


def test_candlestick_section_omitted_when_data_absent():
    """Fields default to None; the section must not print with gaps."""
    r = build_symbol_report(_base())
    assert "Candlesticks only" not in r


def test_candlestick_section_shown_when_data_present():
    r = build_symbol_report(_base(
        ltf_candle_kind="bearish_pin", ltf_candle_strength=0.6,
        ltf_candle_bias="bullish", ltf_candle_bias_strength=0.09,
        htf_candle_kind="neutral", htf_candle_strength=0.0,
        htf_candle_bias="neutral", htf_candle_bias_strength=0.0,
    ))
    assert "Candlesticks only" in r
    assert "bearish_pin" in r and "neutral" in r


def test_correspond_no_when_biases_differ():
    r = build_symbol_report(_base(
        ltf_candle_kind="bullish_close", ltf_candle_strength=0.5,
        ltf_candle_bias="bullish", ltf_candle_bias_strength=0.4,
        htf_candle_kind="bearish_pin", htf_candle_strength=0.7,
        htf_candle_bias="bearish", htf_candle_bias_strength=0.2,
    ))
    assert "Correspond: NO" in r


def test_correspond_yes_when_biases_agree_and_are_directional():
    r = build_symbol_report(_base(
        ltf_candle_kind="bullish_pin", ltf_candle_strength=0.6,
        ltf_candle_bias="bullish", ltf_candle_bias_strength=0.3,
        htf_candle_kind="bullish_close", htf_candle_strength=0.5,
        htf_candle_bias="bullish", htf_candle_bias_strength=0.5,
    ))
    assert "Correspond: YES — BULLISH on both" in r


def test_correspond_no_when_both_neutral():
    """Two neutrals are technically equal, but neutral is not a direction --
    'correspond' must mean two timeframes agreeing on a DIRECTION."""
    r = build_symbol_report(_base(
        ltf_candle_kind="neutral", ltf_candle_strength=0.0,
        ltf_candle_bias="neutral", ltf_candle_bias_strength=0.0,
        htf_candle_kind="neutral", htf_candle_strength=0.0,
        htf_candle_bias="neutral", htf_candle_bias_strength=0.0,
    ))
    assert "Correspond: NO" in r
