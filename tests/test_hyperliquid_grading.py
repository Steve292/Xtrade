"""
Tests for HyperliquidTrader's realised-outcome grading (bot/trade_grades.py
wiring in bot/hyperliquid/trader.py) -- entry recording on execute(), P&L
lookup + exit recording on check_exits(), and the grades_path=None /
no-recording default. Mirrors what MT5Broker already does, added here
because this venue previously had none of it: every closed Hyperliquid
trade vanished ungraded.

No network: the Hyperliquid client is a minimal fake exposing exactly the
methods execute()/check_exits() call.

Run directly (`python tests/test_hyperliquid_grading.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.hyperliquid.trader import HyperliquidTrader, TradePlan
from bot.screening import TradeScreener
from bot.smc.strategy import SMCStrategy
from bot.trade_grades import GradeBook


class _Position:
    def __init__(self, coin):
        self.coin = coin


class FakeHLClient:
    """Just enough surface for execute()/check_exits(): long()/short() return
    a filled bulk-order response, attach_bracket() a clean two-leg one, and
    fills() returns whatever the test pre-loads -- no real network shape
    needed beyond the fields trader.py actually reads."""

    def __init__(self):
        self._fills: list[dict] = []

    def _filled_resp(self, price, size):
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(size), "avgPx": str(price), "oid": 1}}
        ]}}}

    def long(self, coin, usd, leverage=None):
        return self._filled_resp(100.0, usd / 100.0)

    def short(self, coin, usd, leverage=None):
        return self._filled_resp(100.0, usd / 100.0)

    def attach_bracket(self, coin, is_buy, size, stop_loss, take_profit):
        return {"status": "ok", "response": {"data": {"statuses": [
            {"resting": {"oid": 10}}, {"resting": {"oid": 11}},
        ]}}}

    def set_fills(self, fills):
        self._fills = fills

    def fills(self, limit=500):
        return self._fills


def _plan(coin="BTC", side="long", detectors=("bot.smc.structure",)) -> TradePlan:
    return TradePlan(
        coin=coin, side=side, usd=1000.0, leverage=3,
        entry=100.0, stop_loss=95.0, take_profit=110.0, risk_pct=1.0,
        detectors=detectors,
    )


def _trader(client, grades_path=None) -> HyperliquidTrader:
    return HyperliquidTrader(client, SMCStrategy(), TradeScreener(), grades_path=grades_path)


# --- grades_path=None: the default, recording stays off exactly as before --


def test_default_grades_path_records_nothing(tmp_path):
    client = FakeHLClient()
    trader = _trader(client)  # grades_path defaults to None
    trader.execute(_plan())
    grades_file = tmp_path / "trade_grades.json"
    assert not grades_file.exists(), "no grades_path was configured, nothing should be written"


# --- entry recording ---------------------------------------------------------


def test_execute_records_an_entry_with_its_detectors(tmp_path):
    grades_path = tmp_path / "trade_grades.json"
    client = FakeHLClient()
    trader = _trader(client, grades_path=grades_path)
    trader.execute(_plan(detectors=("bot.smc.structure", "bot.smc.fvg")))

    book = GradeBook.load(grades_path)
    assert len(book.records) == 1
    rec = book.records[0]
    assert rec["symbol"] == "BTC"
    assert rec["side"] == "long"
    assert set(rec["detectors"]) == {"bot.smc.structure", "bot.smc.fvg"}
    assert rec["outcome"] is None  # still open


def test_execute_tracks_detectors_and_open_time_for_the_exit_side(tmp_path):
    client = FakeHLClient()
    trader = _trader(client, grades_path=tmp_path / "trade_grades.json")
    trader.execute(_plan())
    assert trader._tracked["BTC"]["detectors"] == ("bot.smc.structure",)
    assert trader._tracked["BTC"]["opened_at"] is not None


def test_a_failed_bracket_attach_never_records_an_entry(tmp_path):
    """execute() bails out before touching self._tracked when the bracket
    fails to attach (position left unprotected, handled elsewhere) -- grading
    an entry with no corresponding exit-tracking would leave it open forever."""
    class NoBracketClient(FakeHLClient):
        def attach_bracket(self, *a, **kw):
            raise RuntimeError("rejected")

    grades_path = tmp_path / "trade_grades.json"
    trader = _trader(NoBracketClient(), grades_path=grades_path)
    trader.execute(_plan())
    assert not grades_path.exists()


# --- exit recording + P&L lookup ---------------------------------------------


def test_check_exits_grades_a_closed_trade_from_fills(tmp_path):
    grades_path = tmp_path / "trade_grades.json"
    client = FakeHLClient()
    trader = _trader(client, grades_path=grades_path)
    trader.execute(_plan())
    opened_at = trader._tracked["BTC"]["opened_at"]

    # A closing fill after the trade opened, with a nonzero closedPnl.
    client.set_fills([
        {"coin": "BTC", "time": (opened_at + 1) * 1000, "closedPnl": "42.5"},
    ])
    closed = trader.check_exits(open_positions=[])  # BTC no longer open -> closed
    assert len(closed) == 1
    assert closed[0]["coin"] == "BTC"
    assert closed[0]["pnl"] == 42.5
    assert "BTC" not in trader._tracked  # untracked either way

    book = GradeBook.load(grades_path)
    rec = book.records[0]
    assert rec["outcome"] == "win"
    assert rec["pnl"] == 42.5


def test_check_exits_sums_multiple_closing_fills(tmp_path):
    """A position can close across more than one fill (partial fills) --
    every closing fill after entry must be credited, not just the first."""
    grades_path = tmp_path / "trade_grades.json"
    client = FakeHLClient()
    trader = _trader(client, grades_path=grades_path)
    trader.execute(_plan())
    opened_at = trader._tracked["BTC"]["opened_at"]

    client.set_fills([
        {"coin": "BTC", "time": (opened_at + 1) * 1000, "closedPnl": "10.0"},
        {"coin": "BTC", "time": (opened_at + 2) * 1000, "closedPnl": "-3.0"},
    ])
    closed = trader.check_exits(open_positions=[])
    assert closed[0]["pnl"] == 7.0


def test_fills_from_before_this_trade_opened_are_excluded(tmp_path):
    """A prior trade's closing fill on the same coin must not contaminate
    this trade's grade -- only fills at/after opened_at count."""
    grades_path = tmp_path / "trade_grades.json"
    client = FakeHLClient()
    trader = _trader(client, grades_path=grades_path)
    trader.execute(_plan())
    opened_at = trader._tracked["BTC"]["opened_at"]

    client.set_fills([
        {"coin": "BTC", "time": (opened_at - 100) * 1000, "closedPnl": "999.0"},  # stale, excluded
        {"coin": "BTC", "time": (opened_at + 1) * 1000, "closedPnl": "5.0"},
    ])
    closed = trader.check_exits(open_positions=[])
    assert closed[0]["pnl"] == 5.0


def test_no_matching_closing_fill_leaves_the_trade_ungraded_not_flat(tmp_path):
    """No closedPnl found must record NOTHING (pnl=None), never a fabricated
    0.0 -- a fabricated flat outcome would look like a real break-even trade
    and skew every grade drawn from this detector."""
    grades_path = tmp_path / "trade_grades.json"
    client = FakeHLClient()
    trader = _trader(client, grades_path=grades_path)
    trader.execute(_plan())
    client.set_fills([])  # nothing at all

    closed = trader.check_exits(open_positions=[])
    assert closed[0]["pnl"] is None

    book = GradeBook.load(grades_path)
    assert book.records[0]["outcome"] is None, "left open/ungraded, not recorded as flat"


def test_fills_lookup_failure_does_not_raise(tmp_path):
    """A network hiccup fetching fills() must degrade to ungraded, never
    crash the exit-reconciliation pass that also drives ratchet/bracket
    logic elsewhere in the loop."""
    class BrokenFillsClient(FakeHLClient):
        def fills(self, limit=500):
            raise ConnectionError("boom")

    grades_path = tmp_path / "trade_grades.json"
    trader = _trader(BrokenFillsClient(), grades_path=grades_path)
    trader.execute(_plan())
    closed = trader.check_exits(open_positions=[])
    assert closed[0]["pnl"] is None


def test_open_positions_still_tracked_are_not_reported_as_closed(tmp_path):
    client = FakeHLClient()
    trader = _trader(client, grades_path=tmp_path / "trade_grades.json")
    trader.execute(_plan())
    closed = trader.check_exits(open_positions=[_Position("BTC")])
    assert closed == []
    assert "BTC" in trader._tracked


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
