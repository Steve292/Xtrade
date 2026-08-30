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


# --- no-signal expectations point at the right structural gap ---------------


def test_neutral_htf_blames_the_htf_bias():
    r = build_symbol_report(_base(htf_trend="neutral", ltf_trend="neutral"))
    assert "HTF bias is neutral" in r or "bias is neutral" in r


def test_htf_ltf_disagreement_is_named():
    r = build_symbol_report(_base(htf_trend="bullish", ltf_trend="bearish"))
    assert "hasn't aligned" in r
    assert "bullish" in r  # names which direction the LTF needs to match


def test_missing_sweep_is_named_when_structure_agrees():
    r = build_symbol_report(_base(htf_trend="bullish", ltf_trend="bullish", sweep=None))
    assert "liquidity has been swept" in r


def test_zone_gap_is_the_last_resort_explanation():
    r = build_symbol_report(_base(
        htf_trend="bullish", ltf_trend="bullish",
        sweep="sell_side @ 78,900 (3 bars ago)"))
    assert "demand/supply zone" in r


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
