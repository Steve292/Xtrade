"""
Tests for bot/market_snapshot.py's Binance candle fallback -- no network.
Every test injects a fake hl_client / exchange_factory rather than hitting
Hyperliquid or Binance for real.

Run directly (`python tests/test_market_snapshot.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.market_snapshot import _binance_candles, _candles_with_fallback


def _df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n, "close": [1.0] * n, "volume": [1.0] * n,
    })


class FakeExchange:
    def __init__(self, df=None, raise_exc=None):
        self._df = df if df is not None else _df()
        self._raise = raise_exc
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit=200):
        self.calls.append((symbol, timeframe, limit))
        if self._raise:
            raise self._raise
        return self._df


class FakeHlClient:
    def __init__(self, df=None, raise_exc=None):
        self._df = df
        self._raise = raise_exc
        self.calls = []

    def candles(self, coin, interval, lookback_hours):
        self.calls.append((coin, interval, lookback_hours))
        if self._raise:
            raise self._raise
        return self._df


def test_binance_candles_computes_limit_from_lookback_and_interval():
    fake = FakeExchange()
    _binance_candles("15m", lookback_hours=48, symbol="BTC/USDT", exchange_factory=lambda: fake)
    assert fake.calls == [("BTC/USDT", "15m", 193)]  # 48*60/15 + 1


def test_binance_candles_caps_limit_at_1000():
    fake = FakeExchange()
    _binance_candles("1m", lookback_hours=24 * 220, symbol="BTC/USDT", exchange_factory=lambda: fake)
    assert fake.calls[0][2] == 1000


def test_binance_candles_returns_same_shape_as_hyperliquid():
    df = _df(5)
    fake = FakeExchange(df=df)
    result = _binance_candles("15m", 48, exchange_factory=lambda: fake)
    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(result) == 5


def test_candles_with_fallback_prefers_hyperliquid_when_it_succeeds():
    hl_df = _df(2)
    hl = FakeHlClient(df=hl_df)
    fake_exchange = FakeExchange()
    result = _candles_with_fallback(hl, "BTC", "15m", 48, exchange_factory=lambda: fake_exchange)
    assert result is hl_df
    assert fake_exchange.calls == []  # never touched Binance


def test_candles_with_fallback_uses_binance_when_hyperliquid_raises():
    hl = FakeHlClient(raise_exc=RuntimeError("no candles"))
    binance_df = _df(4)
    fake_exchange = FakeExchange(df=binance_df)
    result = _candles_with_fallback(hl, "BTC", "15m", 48, exchange_factory=lambda: fake_exchange)
    assert result is binance_df
    assert fake_exchange.calls[0][0] == "BTC/USDT"


def test_candles_with_fallback_uses_binance_when_hl_client_is_none():
    binance_df = _df(1)
    fake_exchange = FakeExchange(df=binance_df)
    result = _candles_with_fallback(None, "ETH", "1h", 200, exchange_factory=lambda: fake_exchange)
    assert result is binance_df
    assert fake_exchange.calls[0][0] == "ETH/USDT"


def test_candles_with_fallback_returns_none_when_both_fail():
    hl = FakeHlClient(raise_exc=RuntimeError("down"))
    fake_exchange = FakeExchange(raise_exc=RuntimeError("also down"))
    result = _candles_with_fallback(hl, "BTC", "15m", 48, exchange_factory=lambda: fake_exchange)
    assert result is None


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
