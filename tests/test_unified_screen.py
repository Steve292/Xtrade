"""
Tests for bot/unified_screen.py — the combined structure + smart-money gate.
No network: takes plain Signal/ScreenResult objects and plain smart-money
values.

Run directly (`python tests/test_unified_screen.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.screening import Check, ScreenResult
from bot.smc.strategy import Signal, SignalType
from bot.unified_screen import evaluate_unified


def _signal(side: SignalType, confidence: float = 0.8) -> Signal:
    return Signal(type=side, entry=100.0, stop_loss=95.0, take_profit=110.0, reason="", confidence=confidence)


def _screen(approved: bool, direction: str = "long") -> ScreenResult:
    checks = [Check("x", approved, "")]
    return ScreenResult(approved=approved, direction=direction, checks=checks)


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


# --- structure gate ----------------------------------------------------


def test_structure_not_cleared_blocks_regardless_of_smart_money():
    result = evaluate_unified(
        _signal(SignalType.LONG), _screen(False),
        smart_money_direction="BULLISH", smart_money_bullish_count=5, smart_money_bearish_count=0,
    )
    assert not result.approved
    assert not result.structure_ok
    assert "structure" in result.reason


# --- smart money contradiction ---------------------------------------------


def test_smart_money_bearish_blocks_a_long_signal():
    result = evaluate_unified(
        _signal(SignalType.LONG), _screen(True),
        smart_money_direction="BEARISH", smart_money_bullish_count=0, smart_money_bearish_count=3,
    )
    assert not result.approved
    assert result.structure_ok
    assert not result.smart_money_ok
    assert "contradicts" in result.reason


def test_smart_money_bullish_blocks_a_short_signal():
    result = evaluate_unified(
        _signal(SignalType.SHORT), _screen(True, direction="short"),
        smart_money_direction="BULLISH", smart_money_bullish_count=4, smart_money_bearish_count=0,
    )
    assert not result.approved
    assert not result.smart_money_ok


def test_neutral_smart_money_does_not_block():
    # NEUTRAL is common (most passes read neutral/split) -- must not block.
    result = evaluate_unified(
        _signal(SignalType.LONG), _screen(True),
        smart_money_direction="NEUTRAL", smart_money_bullish_count=2, smart_money_bearish_count=2,
    )
    assert result.approved
    assert result.smart_money_ok


def test_agreeing_smart_money_approves():
    result = evaluate_unified(
        _signal(SignalType.LONG), _screen(True),
        smart_money_direction="BULLISH", smart_money_bullish_count=4, smart_money_bearish_count=1,
    )
    assert result.approved
    assert result.smart_money_ok
    assert result.smart_money_agreement_count == 4


def test_none_signal_type_never_approves():
    result = evaluate_unified(
        _signal(SignalType.NONE), _screen(True),
        smart_money_direction="NEUTRAL", smart_money_bullish_count=0, smart_money_bearish_count=0,
    )
    assert not result.approved
    assert not result.smart_money_ok


# --- final_pct blend ------------------------------------------------------


def test_final_pct_blends_confidence_and_agreement():
    # confidence 80%, 4/9 modules agree -> (80 + 44.44...) / 2
    result = evaluate_unified(
        _signal(SignalType.LONG, confidence=0.8), _screen(True),
        smart_money_direction="BULLISH", smart_money_bullish_count=4, smart_money_bearish_count=0,
    )
    expected = (80.0 + (4 / 9) * 100) / 2
    assert _close(result.final_pct, expected)


def test_final_pct_with_zero_agreement():
    result = evaluate_unified(
        _signal(SignalType.LONG, confidence=0.6), _screen(True),
        smart_money_direction="NEUTRAL", smart_money_bullish_count=0, smart_money_bearish_count=0,
    )
    assert _close(result.final_pct, 30.0)  # (60 + 0) / 2


def test_final_pct_with_full_agreement():
    result = evaluate_unified(
        _signal(SignalType.SHORT, confidence=1.0), _screen(True, direction="short"),
        smart_money_direction="BEARISH", smart_money_bullish_count=0, smart_money_bearish_count=9,
    )
    assert _close(result.final_pct, 100.0)  # (100 + 100) / 2


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
