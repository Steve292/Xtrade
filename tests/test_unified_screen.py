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


# --- knowledge confluence (advisory only) -----------------------------------
# bot/knowledge.py layers the ingested corpus on as a third input. Two
# properties below are load-bearing and must not regress: it cannot change
# `approved`, and it cannot move final_pct further than the caller allowed.


class _Knowledge:
    """Stand-in for bot.knowledge.KnowledgeResult — evaluate_unified only ever
    reads these three attributes, so the tests don't need the real corpus."""

    def __init__(self, pct, available=True, reason="test"):
        self.knowledge_pct = pct
        self.available = available
        self.reason = reason


def test_omitting_knowledge_reproduces_the_previous_final_pct():
    """The flag-off guarantee: callers that pass no knowledge result must get
    byte-for-byte what this function returned before the layer existed."""
    args = (_signal(SignalType.LONG, 0.8), _screen(True))
    kwargs = dict(smart_money_direction="BULLISH", smart_money_bullish_count=4,
                  smart_money_bearish_count=0)
    baseline = evaluate_unified(*args, **kwargs)
    assert _close(baseline.final_pct, (0.8 * 100 + (4 / 9) * 100) / 2)
    assert baseline.knowledge_adjust == 0.0
    assert baseline.knowledge_pct == 0.0


def test_knowledge_cannot_approve_a_trade_the_gates_rejected():
    for direction, bull, bear in (("BULLISH", 5, 0), ("BEARISH", 0, 5)):
        rejected = evaluate_unified(
            _signal(SignalType.LONG), _screen(False),
            smart_money_direction=direction, smart_money_bullish_count=bull,
            smart_money_bearish_count=bear,
            knowledge_result=_Knowledge(100.0),
        )
        assert not rejected.approved, "knowledge must never flip approval"


def test_knowledge_cannot_veto_a_trade_the_gates_approved():
    approved = evaluate_unified(
        _signal(SignalType.LONG), _screen(True),
        smart_money_direction="BULLISH", smart_money_bullish_count=4,
        smart_money_bearish_count=0,
        knowledge_result=_Knowledge(0.0),
    )
    assert approved.approved, "knowledge is advisory — it does not veto either"


def test_adjustment_never_exceeds_the_configured_cap():
    base = evaluate_unified(
        _signal(SignalType.LONG, 0.8), _screen(True),
        smart_money_direction="BULLISH", smart_money_bullish_count=4,
        smart_money_bearish_count=0,
    ).final_pct

    for pct in (0.0, 25.0, 50.0, 75.0, 100.0):
        for cap in (0.0, 1.0, 5.0, 20.0):
            result = evaluate_unified(
                _signal(SignalType.LONG, 0.8), _screen(True),
                smart_money_direction="BULLISH", smart_money_bullish_count=4,
                smart_money_bearish_count=0,
                knowledge_result=_Knowledge(pct), knowledge_max_adjust_pct=cap,
            )
            assert abs(result.knowledge_adjust) <= cap + 1e-9
            assert abs(result.final_pct - base) <= cap + 1e-9


def test_adjustment_is_signed_around_the_neutral_midpoint():
    def adjust(pct):
        return evaluate_unified(
            _signal(SignalType.LONG, 0.8), _screen(True),
            smart_money_direction="BULLISH", smart_money_bullish_count=4,
            smart_money_bearish_count=0,
            knowledge_result=_Knowledge(pct), knowledge_max_adjust_pct=5.0,
        ).knowledge_adjust

    assert adjust(100.0) > 0
    assert _close(adjust(50.0), 0.0)
    assert adjust(0.0) < 0


def test_unavailable_knowledge_applies_no_adjustment():
    """A missing corpus must read as no opinion. Treating it as a 0% score
    would quietly dampen every setup and look exactly like the strategy
    degrading."""
    result = evaluate_unified(
        _signal(SignalType.LONG, 0.8), _screen(True),
        smart_money_direction="BULLISH", smart_money_bullish_count=4,
        smart_money_bearish_count=0,
        knowledge_result=_Knowledge(0.0, available=False),
    )
    assert result.knowledge_adjust == 0.0


def test_final_pct_stays_within_zero_to_hundred():
    for conf in (0.0, 0.5, 1.0):
        for pct in (0.0, 100.0):
            result = evaluate_unified(
                _signal(SignalType.LONG, conf), _screen(True),
                smart_money_direction="BULLISH", smart_money_bullish_count=9,
                smart_money_bearish_count=0,
                knowledge_result=_Knowledge(pct), knowledge_max_adjust_pct=50.0,
            )
            assert 0.0 <= result.final_pct <= 100.0


# --- direction-fair scoring -------------------------------------------------
# Only 6 of the 9 smart-money modules can vote BUY and only 4 can vote SELL,
# so dividing agreement by 9 scores a short against a ceiling it can never
# reach. These pin the fix and the flag-off parity.


def test_normalisation_is_off_by_default():
    """Off by default: it changes final_pct, and final_pct decides firing."""
    base = evaluate_unified(
        _signal(SignalType.SHORT), _screen(True, "short"),
        smart_money_direction="BEARISH", smart_money_bullish_count=0,
        smart_money_bearish_count=4,
    )
    explicit = evaluate_unified(
        _signal(SignalType.SHORT), _screen(True, "short"),
        smart_money_direction="BEARISH", smart_money_bullish_count=0,
        smart_money_bearish_count=4, normalise_by_direction=False,
    )
    assert _close(base.final_pct, explicit.final_pct)
    assert _close(base.final_pct, (0.8 * 100 + (4 / 9) * 100) / 2)


def test_unnormalised_short_cannot_reach_the_long_ceiling():
    """The bug, stated as a test: a fully-agreed short scores below a
    fully-agreed long, purely because of how many modules can vote each way."""
    from bot.unified_screen import MAX_BEARISH_MODULES, MAX_BULLISH_MODULES

    long_max = evaluate_unified(
        _signal(SignalType.LONG, 1.0), _screen(True),
        smart_money_direction="BULLISH", smart_money_bullish_count=MAX_BULLISH_MODULES,
        smart_money_bearish_count=0,
    )
    short_max = evaluate_unified(
        _signal(SignalType.SHORT, 1.0), _screen(True, "short"),
        smart_money_direction="BEARISH", smart_money_bullish_count=0,
        smart_money_bearish_count=MAX_BEARISH_MODULES,
    )
    assert short_max.final_pct < long_max.final_pct
    assert short_max.final_pct < 75.0, "a maximal short is below a 75% fire line"


def test_normalised_both_directions_reach_the_same_ceiling():
    from bot.unified_screen import MAX_BEARISH_MODULES, MAX_BULLISH_MODULES

    long_max = evaluate_unified(
        _signal(SignalType.LONG, 1.0), _screen(True),
        smart_money_direction="BULLISH", smart_money_bullish_count=MAX_BULLISH_MODULES,
        smart_money_bearish_count=0, normalise_by_direction=True,
    )
    short_max = evaluate_unified(
        _signal(SignalType.SHORT, 1.0), _screen(True, "short"),
        smart_money_direction="BEARISH", smart_money_bullish_count=0,
        smart_money_bearish_count=MAX_BEARISH_MODULES, normalise_by_direction=True,
    )
    assert _close(long_max.final_pct, short_max.final_pct)
    assert _close(long_max.final_pct, 100.0)


def test_normalised_agreement_never_exceeds_one_hundred():
    """A module set richer than the derived maximum must clamp, not overflow."""
    r = evaluate_unified(
        _signal(SignalType.SHORT, 1.0), _screen(True, "short"),
        smart_money_direction="BEARISH", smart_money_bullish_count=0,
        smart_money_bearish_count=99, normalise_by_direction=True,
    )
    assert r.final_pct <= 100.0


def test_normalisation_does_not_change_approval():
    """It rescales a score. Approval still depends only on the two gates."""
    for norm in (False, True):
        r = evaluate_unified(
            _signal(SignalType.SHORT), _screen(False, "short"),
            smart_money_direction="BEARISH", smart_money_bullish_count=0,
            smart_money_bearish_count=4, normalise_by_direction=norm,
        )
        assert not r.approved
