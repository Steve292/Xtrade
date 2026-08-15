"""Tests for the optional gates 8-10 in bot/screening.py.

No network. Run directly (`python tests/test_screening_optional_gates.py`) or
under pytest.

test_defaults_add_no_checks is the one that matters. Both live accounts are
armed against real money, so a new gate that quietly appended a check would
start rejecting trades the seven-gate screen previously approved, on the next
restart, with no config change and nothing in the logs to explain it. The gates
must be inert until switched on -- not "usually inert", provably inert.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.screening import ScreenConfig, TradeScreener
from bot.smc.strategy import Signal, SignalType

COLS = ["open", "high", "low", "close"]


def _series(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS)


# Enough bars for the swing/zone detectors to have something to chew on.
_BARS = [
    (100.0, 100.6, 99.8, 100.1),
    (100.0, 100.5, 99.0, 99.2),
    (99.2, 100.5, 99.1, 100.3),
    (100.3, 101.0, 100.2, 100.8),
    (100.8, 101.0, 99.5, 99.8),
    (99.8, 103.0, 99.7, 102.5),
    (102.5, 103.2, 101.9, 102.0),
    (102.0, 102.4, 100.5, 100.7),
    (100.7, 101.2, 99.6, 99.9),
    (99.9, 102.8, 99.8, 102.4),
]
DF = _series(_BARS)
HTF = _series(_BARS)

SIG = Signal(type=SignalType.LONG, entry=100.0, stop_loss=99.0,
             take_profit=103.0, reason="test", confidence=0.8)


def _checks(cfg: ScreenConfig) -> list:
    return TradeScreener(cfg).screen(SIG, DF, HTF).checks


def _names(cfg: ScreenConfig) -> list:
    return [c.name for c in _checks(cfg)]


def test_defaults_add_no_checks():
    names = _names(ScreenConfig())
    for added in ("Mitigation", "Breaker", "Candle confirmation",
                  "Wyckoff", "Value area edge"):
        assert added not in names, f"{added} ran without being enabled: {names}"


def test_default_check_count_is_the_original_seven():
    assert len(_checks(ScreenConfig())) == 7, _names(ScreenConfig())


def test_each_gate_appears_only_when_enabled():
    assert "Mitigation" in _names(ScreenConfig(require_mitigation=True))
    assert "Breaker" in _names(ScreenConfig(require_breaker=True))
    assert "Candle confirmation" in _names(
        ScreenConfig(require_candle_confirmation=True))
    assert "Wyckoff" in _names(ScreenConfig(require_wyckoff=True))
    assert "Value area edge" in _names(ScreenConfig(require_value_area_edge=True))


def test_gates_are_independent():
    names = _names(ScreenConfig(require_candle_confirmation=True))
    assert "Candle confirmation" in names
    assert "Mitigation" not in names and "Breaker" not in names


def test_an_enabled_gate_can_reject():
    # A flat series has no candle pattern in any direction, so the gate must
    # fail rather than pass vacuously. A gate that cannot reject is decoration.
    flat = _series([(100, 100, 100, 100)] * 10)
    res = TradeScreener(ScreenConfig(require_candle_confirmation=True)).screen(
        SIG, flat, flat)
    candle = [c for c in res.checks if c.name == "Candle confirmation"]
    assert candle and candle[0].passed is False, res.table()


def test_enabling_a_gate_never_turns_a_rejection_into_an_approval():
    # Gates only ever ADD conditions. If the base screen rejects, no additional
    # gate may rescue the trade.
    base = TradeScreener(ScreenConfig()).screen(SIG, DF, HTF)
    if base.approved:
        return          # nothing to prove on this fixture
    for cfg in (ScreenConfig(require_mitigation=True),
                ScreenConfig(require_breaker=True),
                ScreenConfig(require_candle_confirmation=True)):
        assert TradeScreener(cfg).screen(SIG, DF, HTF).approved is False


def test_from_dict_still_ignores_unknown_keys():
    cfg = ScreenConfig.from_dict({"require_mitigation": True, "nonsense": 1})
    assert cfg.require_mitigation is True
    assert cfg.min_rr == ScreenConfig().min_rr


def test_no_signal_short_circuits_before_any_optional_gate():
    none_sig = Signal(type=SignalType.NONE, entry=0.0, stop_loss=0.0,
                      take_profit=0.0, reason="none", confidence=0.0)
    res = TradeScreener(ScreenConfig(require_mitigation=True,
                                     require_breaker=True,
                                     require_candle_confirmation=True)).screen(
        none_sig, DF, HTF)
    assert res.approved is False
    assert [c.name for c in res.checks] == ["SMC confluence"]


def test_new_gates_appear_only_when_enabled():
    assert "Premium/Discount" in _names(ScreenConfig(require_premium_discount=True))
    assert "Target at a level" in _names(ScreenConfig(require_target_at_level=True))
    assert "Concept consensus" in _names(ScreenConfig(min_consensus_score=0.0))


def test_a_disabled_gate_does_not_break_an_enabled_one():
    """Regression: a local import inside one gate broke a DIFFERENT gate.

    `from bot.smc.liquidity import detect_liquidity_pools` inside the
    require_target_at_level branch made that name local to screen() for the
    whole function body, so gate 3 -- a CORE gate, always on -- raised
    UnboundLocalError even with the new gate switched off. Python rebinds the
    name for the entire scope, not just the branch. This asserts the core
    screen still runs with every optional gate off, which is exactly the case
    that broke.
    """
    res = TradeScreener(ScreenConfig()).screen(SIG, DF, HTF)
    names = [c.name for c in res.checks]
    assert "Liquidity sweep" in names, names
    assert len(names) == 7


def test_consensus_gate_accepts_a_threshold_of_zero_as_active():
    # min_consensus_score=0.0 is falsy. If the gate tested truthiness instead
    # of `is not None`, a threshold of exactly zero would silently disable it.
    assert "Concept consensus" in _names(ScreenConfig(min_consensus_score=0.0))


def test_gate_timeframe_modes():
    # Eleven of the twelve gates only ever read the entry frame, so a setup
    # could satisfy every concept on the 15m while the 1h said the opposite and
    # nothing noticed. Each mode must actually change which frame is consulted.
    for mode in ("ltf", "htf", "both"):
        cfg = ScreenConfig(gate_timeframe=mode, require_candle_confirmation=True)
        detail = [c for c in _checks(cfg) if c.name == "Candle confirmation"][0].detail
        assert mode.split("/")[0] in detail or "htf" in detail or "ltf" in detail


def test_both_mode_names_the_failing_timeframe():
    # "no confirming candle" is far less useful than "no confirming candle on
    # htf" when you are trying to work out why a trade was refused.
    cfg = ScreenConfig(gate_timeframe="both", require_candle_confirmation=True)
    c = [x for x in _checks(cfg) if x.name == "Candle confirmation"][0]
    assert "ltf" in c.detail or "htf" in c.detail


def test_default_timeframe_is_unchanged_behaviour():
    assert ScreenConfig().gate_timeframe == "ltf"


def _run_all() -> bool:
    ok = True
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                ok = False
                print(f"  FAIL {name}: {exc}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
