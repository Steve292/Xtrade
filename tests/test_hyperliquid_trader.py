"""
Tests for HyperliquidTrader.evaluate()'s Binance-fallback candle wiring
(bot/hyperliquid/trader.py). No network: candles_with_binance_fallback is
monkeypatched at the module level (evaluate() calls it directly, with no
injectable factory parameter of its own -- callers never need one, only
tests do) rather than reaching for a mocking library this codebase doesn't
otherwise use.

Run directly (`python tests/test_hyperliquid_trader.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import bot.hyperliquid.trader as trader_module
from bot.hyperliquid.trader import HyperliquidTrader
from bot.screening import TradeScreener
from bot.smc.strategy import SMCStrategy


def _flat_df(n: int = 60) -> pd.DataFrame:
    # Flat/insufficient-structure data is fine here -- these tests only
    # care that evaluate() runs to completion (or raises) as expected, not
    # what signal a real market shape would produce.
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.0] * n, "volume": [10.0] * n,
    })


class FakeClient:
    def __init__(self, raise_exc=None):
        self._raise = raise_exc

    def candles(self, coin, interval, lookback_hours):
        if self._raise:
            raise self._raise
        return _flat_df()


def _trader(client) -> HyperliquidTrader:
    return HyperliquidTrader(client, SMCStrategy(), TradeScreener())


def test_evaluate_succeeds_via_hyperliquid_when_healthy():
    trader = _trader(FakeClient())
    signal, result, plan, unified = trader.evaluate("BTC", "15m", "1h", 10_000.0)
    assert signal is not None and unified is not None


def test_evaluate_falls_back_to_binance_when_hyperliquid_raises():
    original = trader_module.candles_with_binance_fallback
    calls = []

    def fake_fallback(venue_client, coin, interval, lookback_hours):
        calls.append((coin, interval))
        try:
            return venue_client.candles(coin, interval=interval, lookback_hours=lookback_hours)
        except Exception:
            return _flat_df()  # stand-in for a successful Binance recovery

    trader_module.candles_with_binance_fallback = fake_fallback
    try:
        trader = _trader(FakeClient(raise_exc=RuntimeError("429")))
        signal, result, plan, unified = trader.evaluate("BTC", "15m", "1h", 10_000.0)
        assert signal is not None and unified is not None
        assert len(calls) == 2  # ltf + htf, both recovered via the fallback
    finally:
        trader_module.candles_with_binance_fallback = original


def test_evaluate_raises_when_both_hyperliquid_and_binance_fail():
    original = trader_module.candles_with_binance_fallback
    trader_module.candles_with_binance_fallback = lambda *a, **kw: None  # both sources exhausted
    try:
        trader = _trader(FakeClient())
        raised = False
        try:
            trader.evaluate("BTC", "15m", "1h", 10_000.0)
        except RuntimeError as e:
            raised = True
            assert "BTC" in str(e)
        assert raised
    finally:
        trader_module.candles_with_binance_fallback = original


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
