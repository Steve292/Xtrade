"""Tests for bot/knowledge/extract.py and unit safety in candidates.py.

No network, no model. Run directly (`python tests/test_knowledge_extract.py`)
or under pytest.

test_percent_never_lands_raw_in_a_fraction_parameter is the important one. It
pins the worst defect this pipeline has produced: "risk 1% per trade" yielded
1.0, max_stop_pct accepts 0.001-1.0, so 1.0 passed every range check and every
validation and surfaced as the HIGHEST-SCORING candidate in the review file --
"set max_stop_pct to 1", i.e. a 100% stop loss, presented to a human as the
best-evidenced recommendation available. Nothing about it looked wrong; the
number was real, the quote was real, the citation was real. Only the unit was
wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.knowledge import candidates as C
from bot.knowledge import extract
from bot.knowledge.store import Segment


def test_extract_numbers_reports_the_unit_family():
    got = extract.extract_numbers("risk 1% per trade", "position_size")
    assert got == [("1%", 1.0, "percent")], got
    rr = extract.extract_numbers("never below 1:3 risk reward", "risk_reward")
    assert rr and rr[0][2] == "ratio"
    bars = extract.extract_numbers("look back 20 candles", "sweep")
    assert bars and bars[0][2] == "count"


def test_percent_converts_to_fraction():
    t = C.PARAM_TARGETS["max_stop_pct"]
    assert t.unit == "fraction"
    assert C.convert_to_param_unit(1.0, "percent", t) == 0.01
    assert C.convert_to_param_unit(40.0, "percent", t) == 0.4


def test_conversion_refuses_rather_than_guesses():
    # A count of bars is not a fraction; a ratio is not a percentage. These
    # must return None so the candidate drops its param, not be coerced.
    frac = C.PARAM_TARGETS["max_stop_pct"]
    ratio = C.PARAM_TARGETS["min_rr"]
    count = C.PARAM_TARGETS["sweep_bars"]
    assert C.convert_to_param_unit(20.0, "count", frac) is None
    assert C.convert_to_param_unit(3.0, "ratio", frac) is None
    assert C.convert_to_param_unit(1.0, "percent", ratio) is None
    assert C.convert_to_param_unit(0.5, "fraction", count) is None


def test_percent_never_lands_raw_in_a_fraction_parameter():
    # The real regression: 1% must never become max_stop_pct=1.0 (a 100% stop).
    t = C.PARAM_TARGETS["max_stop_pct"]
    for raw, val, fam in extract.extract_numbers("keep the stop under 1%", "stop_loss"):
        converted = C.convert_to_param_unit(val, fam, t)
        if converted is not None:
            assert converted <= 0.5, (
                f"'{raw}' became {converted} for max_stop_pct — a stop of "
                f"{converted*100:.0f}%")


def test_ratio_passes_through_unscaled():
    t = C.PARAM_TARGETS["min_rr"]
    got = extract.extract_numbers("minimum 1:4 risk reward", "risk_reward")
    vals = [C.convert_to_param_unit(v, f, t) for _r, v, f in got]
    assert 4.0 in vals, vals


def test_years_and_money_are_still_rejected():
    assert extract.extract_numbers("back in 2024 we saw", "risk_reward") == []
    assert extract.extract_numbers("price hit $65,000 there", "fib") == []


def test_fib_only_accepts_real_retracement_levels():
    got = extract.extract_numbers("enter at the 0.705 not the 0.618", "fib")
    vals = sorted(v for _r, v, _f in got)
    assert vals == [0.618, 0.705], vals
    assert extract.extract_numbers("some 0.431 number", "fib") == []


def test_concepts_carry_segment_index_and_timestamp():
    segs = [Segment(0.0, 5.0, "nothing here"),
            Segment(5.0, 10.0, "wait for the liquidity sweep")]
    hits = extract.extract_concepts(segs)
    sweep = [h for h in hits if h.key == "sweep"]
    assert sweep and sweep[0].segment_index == 1 and sweep[0].start == 5.0


def test_every_param_target_declares_a_known_unit():
    for name, t in C.PARAM_TARGETS.items():
        assert t.unit in ("fraction", "ratio", "count"), f"{name}: {t.unit}"


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
