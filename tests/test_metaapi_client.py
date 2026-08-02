"""
Tests for MetaApiClient — no network, no real background thread. Stub
connection/account methods are plain `async def`s; MetaApiClient._run()
falls back to `asyncio.run()` when built with loop=None (the documented
test-stub path on the class), so these run synchronously like every other
test in this repo.

Run directly (`python tests/test_metaapi_client.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metaapi_cloud_sdk.clients.metaapi.trade_exception import TradeException

from bot.mt5.metaapi_client import MetaApiClient


class StubConnection:
    def __init__(self):
        self.calls = []
        self.positions = []
        self.trade_fails = False

    async def get_account_information(self):
        return {"balance": 1234.5, "equity": 1230.0}

    async def get_symbol_specification(self, symbol):
        return {"digits": 5, "point": 0.00001, "tickSize": 0.00001, "minVolume": 0.01,
                "maxVolume": 100.0, "volumeStep": 0.01, "contractSize": 100000.0}

    async def get_symbol_price(self, symbol):
        return {"bid": 1.10000, "ask": 1.10012, "profitTickValue": 0.91, "lossTickValue": 0.91}

    async def get_positions(self):
        return self.positions

    async def create_market_buy_order(self, symbol, volume, stop_loss=None, take_profit=None, options=None):
        self.calls.append(("buy", symbol, volume, stop_loss, take_profit, options))
        if self.trade_fails:
            raise TradeException("no money", 10019, "TRADE_RETCODE_NO_MONEY")
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE", "positionId": "555"}

    async def create_market_sell_order(self, symbol, volume, stop_loss=None, take_profit=None, options=None):
        self.calls.append(("sell", symbol, volume, stop_loss, take_profit, options))
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE", "positionId": "556"}

    async def close_position(self, position_id, options=None):
        self.calls.append(("close", position_id))
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE"}


class StubAccount:
    def __init__(self):
        self.calls = []
        self.no_candles = False

    async def get_historical_candles(self, symbol, timeframe, start_time=None, limit=None):
        self.calls.append((symbol, timeframe, start_time, limit))
        if self.no_candles:
            return []
        return [
            {"time": "2025-01-01T00:00:00Z", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 100},
            {"time": "2025-01-01T00:15:00Z", "open": 1.15, "high": 1.25, "low": 1.05, "close": 1.2, "volume": 120},
        ]


def _client(connection=None, account=None):
    return MetaApiClient(connection or StubConnection(), account or StubAccount(), loop=None, thread=None)


def test_account_balance():
    c = _client()
    assert c.account_balance() == 1234.5


def test_symbol_info_uses_spec_and_price():
    c = _client()
    info = c.symbol_info("EURUSD")
    assert info.digits == 5 and info.tick_size == 0.00001
    assert info.tick_value == 0.91  # profitTickValue — no static tick value in MetaApi's spec
    assert info.volume_min == 0.01 and info.volume_max == 100.0
    assert info.contract_size == 100000.0


def test_tick_returns_bid_ask():
    c = _client()
    bid, ask = c.tick("EURUSD")
    assert bid == 1.10000 and ask == 1.10012


def test_copy_rates_shapes_dataframe():
    c = _client()
    df = c.copy_rates("EURUSD", "15m", count=2)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.iloc[0]["close"] == 1.15


def test_copy_rates_raises_clearly_when_empty():
    account = StubAccount()
    account.no_candles = True
    c = _client(account=account)
    try:
        c.copy_rates("EURUSD", "15m")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "G1-only" in str(e)


def test_market_order_long_routes_to_buy():
    connection = StubConnection()
    c = _client(connection=connection)
    result = c.market_order("EURUSD", "long", 0.1, sl=1.05, tp=1.20, comment="test")
    assert connection.calls[-1][0] == "buy"
    assert c.order_succeeded(result)


def test_market_order_short_routes_to_sell():
    connection = StubConnection()
    c = _client(connection=connection)
    c.market_order("EURUSD", "short", 0.1, sl=1.20, tp=1.05)
    assert connection.calls[-1][0] == "sell"


def test_market_order_failure_becomes_result_not_exception():
    connection = StubConnection()
    connection.trade_fails = True
    c = _client(connection=connection)
    result = c.market_order("EURUSD", "long", 0.1, sl=1.05, tp=1.20)
    assert not c.order_succeeded(result)
    assert result["numericCode"] == 10019


def test_get_position_filters_by_symbol():
    connection = StubConnection()
    connection.positions = [{"id": 1, "symbol": "GBPUSD"}, {"id": 2, "symbol": "EURUSD"}]
    c = _client(connection=connection)
    pos = c.get_position("EURUSD")
    assert pos["id"] == 2


def test_get_position_returns_none_when_absent():
    c = _client()
    assert c.get_position("EURUSD") is None


def test_close_position_uses_id():
    connection = StubConnection()
    c = _client(connection=connection)
    result = c.close_position({"id": 42, "symbol": "EURUSD"})
    assert connection.calls[-1] == ("close", "42")
    assert c.order_succeeded(result)


def test_order_succeeded_rejects_non_dict():
    c = _client()
    assert not c.order_succeeded(None)
    assert not c.order_succeeded("ok")


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
