"""
Tests for bot/trade_grades.py — grading detectors by realised outcomes.

The point of this module is to replace the corpus's popularity weighting with
performance, so the tests focus on the two things that would make a grade
misleading: extrapolating from a thin sample, and recording an unknown
outcome as a real one.

Run directly (`python tests/test_trade_grades.py`) or under pytest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.trade_grades import MIN_SAMPLE, GradeBook


def _book(tmp_path) -> GradeBook:
    return GradeBook.load(tmp_path / "grades.json")


def _round_trip(book, trade_id, detectors, pnl, symbol="BTCUSDc"):
    book.record_entry(trade_id, symbol, "long", detectors, 100.0, 90.0, 120.0)
    book.record_exit(trade_id, pnl)


# --- recording --------------------------------------------------------------


def test_entry_then_exit_produces_a_closed_record(tmp_path):
    b = _book(tmp_path)
    _round_trip(b, "t1", ["bot.smc.order_blocks"], 5.0)
    assert b.summary()["closed"] == 1
    assert b.summary()["wins"] == 1
    assert b.summary()["net_pnl"] == 5.0


def test_open_trade_is_counted_but_not_graded(tmp_path):
    b = _book(tmp_path)
    b.record_entry("t1", "BTCUSDc", "long", ["bot.smc.fvg"], 100.0, 90.0, 120.0)
    s = b.summary()
    assert s["recorded"] == 1 and s["open"] == 1 and s["closed"] == 0
    assert b.grades() == {}, "an open trade must not contribute to any grade"


def test_exit_without_a_matching_entry_is_refused(tmp_path):
    """A close with no recorded entry — a restart mid-trade, or a hand-closed
    position — is ungradeable. Inventing an entry would poison the sample."""
    assert _book(tmp_path).record_exit("never-seen", 5.0) is False


def test_a_trade_credits_every_detector_that_fired(tmp_path):
    b = _book(tmp_path)
    _round_trip(b, "t1", ["bot.smc.structure", "bot.smc.fvg", "bot.smart_money"], -3.0)
    g = b.grades()
    assert set(g) == {"bot.smc.structure", "bot.smc.fvg", "bot.smart_money"}
    assert all(x.losses == 1 and x.net_pnl == -3.0 for x in g.values())


# --- the thin-sample guard --------------------------------------------------


def test_no_grade_is_reported_below_the_minimum_sample(tmp_path):
    """A 100% win rate off one trade is noise. Reporting it as a grade would
    look like evidence, which is worse than reporting nothing."""
    b = _book(tmp_path)
    for i in range(MIN_SAMPLE - 1):
        _round_trip(b, f"t{i}", ["bot.smc.fvg"], 5.0)
    g = b.grades()["bot.smc.fvg"]
    assert g.trades == MIN_SAMPLE - 1
    assert g.win_rate == 1.0, "the raw stat is still visible"
    assert not g.graded
    assert b.grade_for("bot.smc.fvg") is None, "must not report a grade yet"


def test_grade_appears_once_the_sample_is_reached(tmp_path):
    b = _book(tmp_path)
    for i in range(MIN_SAMPLE):
        _round_trip(b, f"t{i}", ["bot.smc.fvg"], 5.0)
    grade = b.grade_for("bot.smc.fvg")
    assert grade is not None and grade.graded and grade.trades == MIN_SAMPLE


def test_win_rate_and_avg_pnl_are_computed_over_closed_trades(tmp_path):
    b = _book(tmp_path)
    for i in range(6):
        _round_trip(b, f"w{i}", ["bot.smc.fvg"], 10.0)
    for i in range(4):
        _round_trip(b, f"l{i}", ["bot.smc.fvg"], -5.0)
    g = b.grades()["bot.smc.fvg"]
    assert g.trades == 10 and g.wins == 6 and g.losses == 4
    assert g.win_rate == 0.6
    assert abs(g.net_pnl - 40.0) < 1e-9
    assert abs(g.avg_pnl - 4.0) < 1e-9


def test_flat_outcome_counts_as_a_trade_but_not_a_win_or_loss(tmp_path):
    b = _book(tmp_path)
    _round_trip(b, "t1", ["bot.smc.fvg"], 0.0)
    g = b.grades()["bot.smc.fvg"]
    assert g.trades == 1 and g.wins == 0 and g.losses == 0
    assert g.win_rate is None, "no decided trades yet"


# --- persistence ------------------------------------------------------------


def test_records_survive_a_reload(tmp_path):
    path = tmp_path / "grades.json"
    b = GradeBook.load(path)
    _round_trip(b, "t1", ["bot.smc.fvg"], 5.0)
    assert GradeBook.load(path).summary()["closed"] == 1


def test_corrupt_or_missing_store_loads_empty_rather_than_raising(tmp_path):
    missing = tmp_path / "nope.json"
    assert GradeBook.load(missing).records == []
    bad = tmp_path / "bad.json"
    bad.write_text("{{{ not json")
    assert GradeBook.load(bad).records == []


def test_summary_reports_zero_state_cleanly(tmp_path):
    s = _book(tmp_path).summary()
    assert s["recorded"] == 0 and s["closed"] == 0
    assert s["win_rate"] is None
    assert s["detectors_graded"] == 0
    assert s["min_sample"] == MIN_SAMPLE


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- the store must never be written by anything but the live loop ----------


def test_a_broker_without_a_grades_path_records_nothing(tmp_path, monkeypatch):
    """Regression: _grade_entry originally loaded a module-default path
    relative to the cwd, so every paper-mode broker test wrote fixture trades
    into the live grading store. The dataset the grades are computed from
    filled up with EURUSD trades that never happened."""
    import os
    from bot.mt5.broker import MT5Broker

    class FakeInfo:
        tick_size = 0.0001
        tick_value = 1.0
        volume_min = 0.01
        volume_step = 0.01
        volume_max = 100.0

    class FakeClient:
        def symbol_info(self, symbol):
            return FakeInfo()

    monkeypatch.chdir(tmp_path)
    broker = MT5Broker(FakeClient(), symbol="EURUSD", mode="paper")  # no grades_path
    broker.open_position("long", 1.1, 0.1, 1.099, 1.102, "test", "EURUSD")
    broker.check_exit(1.102)

    assert not (tmp_path / "trade_grades.json").exists(), \
        "a broker with no grades_path must not write a grading store"
    assert not list(tmp_path.glob("*.json")), "no store written anywhere"
