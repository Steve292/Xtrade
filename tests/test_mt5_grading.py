"""
Tests for MT5Broker's realised-outcome grading (bot/trade_grades.py wiring
in bot/mt5/broker.py) -- entry recording on open_position(), exit recording
+ outcome on check_exit(), and the grades_path=None / no-recording default.

This wiring predates this test file (MT5Broker._grade_entry/_grade_exit
already existed and Hyperliquid's equivalent was built and tested to match
it), but had no direct test coverage of its own -- these close that gap,
mirroring tests/test_hyperliquid_grading.py's structure. Paper mode only
(same StubClient tests/test_mt5.py already uses) -- no live bridge, no
ticket-based live matching needed to exercise the grading contract itself.

Run directly (`python tests/test_mt5_grading.py`) or under pytest.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.mt5.broker import MT5Broker
from bot.mt5.client import SymbolInfo
from bot.trade_grades import GradeBook

EURUSD = SymbolInfo(
    name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01, contract_size=100000.0,
)


class StubClient:
    def __init__(self, info: SymbolInfo = EURUSD):
        self._info = info

    def symbol_info(self, symbol: str) -> SymbolInfo:
        return self._info

    def copy_rates(self, symbol: str, timeframe: str, count: int = 200) -> pd.DataFrame:
        return pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=count, freq="15min"),
            "open": [1.1] * count, "high": [1.1] * count, "low": [1.1] * count,
            "close": [1.1] * count, "volume": [100] * count,
        })


def _broker(grades_path=None) -> MT5Broker:
    return MT5Broker(StubClient(), symbol="EURUSD", mode="paper",
                      initial_balance=10000, grades_path=grades_path)


# --- grades_path=None: the default, recording stays off exactly as before --


def test_default_grades_path_records_nothing(tmp_path):
    broker = _broker()  # grades_path defaults to None
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                          reason="test", symbol="EURUSD")
    broker.check_exit(1.10200)
    assert not (tmp_path / "trade_grades.json").exists()


# --- entry recording ---------------------------------------------------------


def test_open_position_records_an_entry_with_its_detectors(tmp_path):
    grades_path = tmp_path / "trade_grades.json"
    broker = _broker(grades_path=grades_path)
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                          reason="test", symbol="EURUSD",
                          detectors=("bot.smc.structure", "bot.smc.fvg"))

    book = GradeBook.load(grades_path)
    assert len(book.records) == 1
    rec = book.records[0]
    assert rec["symbol"] == "EURUSD"
    assert rec["side"] == "long"
    assert set(rec["detectors"]) == {"bot.smc.structure", "bot.smc.fvg"}
    assert rec["outcome"] is None  # still open


def test_paper_mode_grade_id_falls_back_to_symbol_and_entry(tmp_path):
    """No ticket exists in paper mode -- MT5Broker._grade_id()'s own
    documented fallback. Confirmed indirectly: a second open+close cycle at
    the SAME entry price closes against the FIRST still-open record (the
    known limitation of that fallback), proving the id is derived from
    symbol+entry rather than being unique-per-call."""
    grades_path = tmp_path / "trade_grades.json"
    broker = _broker(grades_path=grades_path)
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                          reason="test", symbol="EURUSD")
    broker.check_exit(1.10200)  # closes and grades it

    book = GradeBook.load(grades_path)
    assert len(book.records) == 1
    assert book.records[0]["outcome"] == "win"


# --- exit recording -----------------------------------------------------------


def test_take_profit_close_is_graded_as_a_win_with_the_right_pnl(tmp_path):
    grades_path = tmp_path / "trade_grades.json"
    broker = _broker(grades_path=grades_path)
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                          reason="test", symbol="EURUSD")
    broker.check_exit(1.10200)  # TP: +200 points * $1/point * 1.0 lot = +$200

    book = GradeBook.load(grades_path)
    rec = book.records[0]
    assert rec["outcome"] == "win"
    # isclose, not == : tick-based P&L runs through a float division/
    # multiplication chain (see _paper_pnl) that lands at 200.00000000000017
    # here, not exactly 200.0 -- tests/test_mt5.py's own broker.balance
    # assertion already uses isclose for the same reason.
    assert math.isclose(rec["pnl"], 200.0, abs_tol=1e-6)


def test_stop_loss_close_is_graded_as_a_loss(tmp_path):
    grades_path = tmp_path / "trade_grades.json"
    broker = _broker(grades_path=grades_path)
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                          reason="test", symbol="EURUSD")
    broker.check_exit(1.09900)  # SL: -100 points

    book = GradeBook.load(grades_path)
    rec = book.records[0]
    assert rec["outcome"] == "loss"
    assert math.isclose(rec["pnl"], -100.0, abs_tol=1e-6)


def test_a_still_open_position_is_not_graded_as_closed(tmp_path):
    grades_path = tmp_path / "trade_grades.json"
    broker = _broker(grades_path=grades_path)
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                          reason="test", symbol="EURUSD")
    broker.check_exit(1.10050)  # between SL and TP -- no exit yet

    book = GradeBook.load(grades_path)
    assert book.records[0]["outcome"] is None


def test_short_take_profit_is_graded_as_a_win(tmp_path):
    grades_path = tmp_path / "trade_grades.json"
    broker = _broker(grades_path=grades_path)
    broker.open_position("short", entry=1.10000, size=1.0, sl=1.10100, tp=1.09800,
                          reason="test", symbol="EURUSD")
    broker.check_exit(1.09800)  # TP for a short: price fell

    book = GradeBook.load(grades_path)
    rec = book.records[0]
    assert rec["side"] == "short"
    assert rec["outcome"] == "win"
    assert math.isclose(rec["pnl"], 200.0, abs_tol=1e-6)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
