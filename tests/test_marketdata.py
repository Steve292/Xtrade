"""
Tests for bot/marketdata.py — free data fetchers. No network: every test
injects a fake `fetch` that returns a canned response shaped like the real
Yahoo/CoinGecko payloads (verified against the live endpoints during
development).

Run directly (`python tests/test_marketdata.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.marketdata import (
    binance_candles,
    candles_with_binance_fallback,
    coingecko_category_cap,
    coingecko_category_change_24h,
    coingecko_global,
    coingecko_top_by_market_cap,
    coingecko_top_movers_avg_7d_pct,
    crypto_news_headlines,
    deribit_option_oi_by_strike,
    yahoo_change_pct,
    yahoo_level,
)


class FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("HTTP error")

    def json(self):
        return self._payload


def _yahoo_chart_payload(closes: list[float], price: float) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": price},
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }


def test_yahoo_change_pct_computes_correctly():
    payload = _yahoo_chart_payload([100.0, 105.0, 110.0], 110.0)
    fetch = lambda url, **kw: FakeResponse(payload)
    pct = yahoo_change_pct("^VIX", bars_back=1, fetch=fetch)
    assert pct is not None and abs(pct - (110.0 - 105.0) / 105.0 * 100) < 1e-9


def test_yahoo_change_pct_skips_none_closes():
    payload = _yahoo_chart_payload([None, 100.0, None, 120.0], 120.0)
    fetch = lambda url, **kw: FakeResponse(payload)
    pct = yahoo_change_pct("^TNX", bars_back=1, fetch=fetch)
    assert pct is not None and abs(pct - 20.0) < 1e-9


def test_yahoo_change_pct_returns_none_on_insufficient_history():
    payload = _yahoo_chart_payload([110.0], 110.0)
    fetch = lambda url, **kw: FakeResponse(payload)
    assert yahoo_change_pct("^VIX", bars_back=1, fetch=fetch) is None


def test_yahoo_change_pct_returns_none_on_http_error():
    fetch = lambda url, **kw: FakeResponse({}, status_ok=False)
    assert yahoo_change_pct("^VIX", fetch=fetch) is None


def test_yahoo_change_pct_returns_none_on_network_exception():
    def fetch(url, **kw):
        raise ConnectionError("no network")

    assert yahoo_change_pct("^VIX", fetch=fetch) is None


def test_yahoo_level_reads_regular_market_price():
    payload = _yahoo_chart_payload([18.0, 18.5], 18.58)
    fetch = lambda url, **kw: FakeResponse(payload)
    assert yahoo_level("^VIX", fetch=fetch) == 18.58


def test_coingecko_global_parses_dominance():
    payload = {
        "data": {
            "total_market_cap": {"usd": 2_300_000_000_000.0},
            "market_cap_percentage": {"btc": 56.4, "eth": 10.0},
        }
    }
    fetch = lambda url, **kw: FakeResponse(payload)
    result = coingecko_global(fetch=fetch)
    assert result == {
        "total_market_cap_usd": 2_300_000_000_000.0,
        "market_cap_percentage": {"btc": 56.4, "eth": 10.0},
    }


def test_coingecko_global_returns_none_on_bad_shape():
    fetch = lambda url, **kw: FakeResponse({"data": {}})  # missing expected keys
    assert coingecko_global(fetch=fetch) is None


def test_coingecko_category_cap_finds_matching_row():
    payload = [
        {"id": "stablecoins", "market_cap": 302_953_299_191.0},
        {"id": "meme-token", "market_cap": 26_202_021_508.0},
    ]
    fetch = lambda url, **kw: FakeResponse(payload)
    assert coingecko_category_cap("meme-token", fetch=fetch) == 26_202_021_508.0


def test_coingecko_category_cap_returns_none_when_absent():
    fetch = lambda url, **kw: FakeResponse([{"id": "other", "market_cap": 1.0}])
    assert coingecko_category_cap("meme-token", fetch=fetch) is None


def test_coingecko_category_change_24h_finds_matching_row():
    payload = [{"id": "meme-token", "market_cap": 1.0, "market_cap_change_24h": 4.24}]
    fetch = lambda url, **kw: FakeResponse(payload)
    assert coingecko_category_change_24h("meme-token", fetch=fetch) == 4.24


def test_coingecko_top_movers_avg_7d_pct_averages_returns():
    payload = [
        {"id": "dogecoin", "price_change_percentage_7d_in_currency": 0.6},
        {"id": "shiba-inu", "price_change_percentage_7d_in_currency": 24.0},
    ]
    fetch = lambda url, **kw: FakeResponse(payload)
    result = coingecko_top_movers_avg_7d_pct("meme-token", fetch=fetch)
    assert result is not None and abs(result - 12.3) < 1e-9


def test_coingecko_top_movers_avg_7d_pct_skips_missing_values():
    payload = [
        {"id": "a", "price_change_percentage_7d_in_currency": None},
        {"id": "b", "price_change_percentage_7d_in_currency": 10.0},
    ]
    fetch = lambda url, **kw: FakeResponse(payload)
    assert coingecko_top_movers_avg_7d_pct("meme-token", fetch=fetch) == 10.0


def test_coingecko_top_movers_avg_7d_pct_returns_none_on_empty():
    fetch = lambda url, **kw: FakeResponse([])
    assert coingecko_top_movers_avg_7d_pct("meme-token", fetch=fetch) is None


def test_coingecko_top_by_market_cap_reads_hourly_change_from_in_currency_field():
    """1h has no plain `price_change_percentage_1h` form — CoinGecko only
    returns it as `..._1h_in_currency`, and only when 1h is named in the
    request. Reading the plain key would yield None on every row."""
    captured = {}

    def fetch(url, **kwargs):
        captured.update(kwargs.get("params") or {})
        return FakeResponse([{
            "market_cap_rank": 1, "symbol": "btc", "name": "Bitcoin",
            "current_price": 65000.0,
            "price_change_percentage_1h_in_currency": -0.42,
            "price_change_percentage_24h": 2.5,
            "market_cap": 1_280_000_000_000.0, "total_volume": 30_000_000_000.0,
        }])

    rows = coingecko_top_by_market_cap(limit=1, fetch=fetch)
    assert "1h" in captured["price_change_percentage"], "1h was not requested"
    assert rows[0]["change_1h_pct"] == -0.42
    assert rows[0]["change_24h_pct"] == 2.5


def test_coingecko_top_by_market_cap_tolerates_missing_hourly_change():
    """A row without the 1h field must yield None, not raise — the dashboard
    renders None as an em dash."""
    def fetch(url, **kwargs):
        return FakeResponse([{
            "market_cap_rank": 1, "symbol": "btc", "name": "Bitcoin",
            "current_price": 65000.0, "price_change_percentage_24h": 2.5,
            "market_cap": 1_280_000_000_000.0, "total_volume": 30_000_000_000.0,
        }])

    assert coingecko_top_by_market_cap(limit=1, fetch=fetch)[0]["change_1h_pct"] is None


def test_coingecko_top_by_market_cap_shapes_rows():
    payload = [
        {
            "market_cap_rank": 1, "symbol": "btc", "name": "Bitcoin",
            "current_price": 65000.0, "price_change_percentage_24h": 2.5,
            "market_cap": 1_280_000_000_000.0, "total_volume": 30_000_000_000.0,
        },
        {
            "market_cap_rank": 2, "symbol": "eth", "name": "Ethereum",
            "current_price": 3400.0, "price_change_percentage_24h": -1.1,
            "market_cap": 410_000_000_000.0, "total_volume": 12_000_000_000.0,
        },
    ]
    fetch = lambda url, **kw: FakeResponse(payload)
    result = coingecko_top_by_market_cap(limit=2, fetch=fetch)
    # change_1h_pct is None here: this payload predates the 1h column and
    # carries no `price_change_percentage_1h_in_currency` field.
    assert result == [
        {"rank": 1, "symbol": "BTC", "name": "Bitcoin", "price": 65000.0,
         "change_1h_pct": None,
         "change_24h_pct": 2.5, "market_cap": 1_280_000_000_000.0, "volume_24h": 30_000_000_000.0},
        {"rank": 2, "symbol": "ETH", "name": "Ethereum", "price": 3400.0,
         "change_1h_pct": None,
         "change_24h_pct": -1.1, "market_cap": 410_000_000_000.0, "volume_24h": 12_000_000_000.0},
    ]


def test_coingecko_top_by_market_cap_returns_none_on_empty():
    fetch = lambda url, **kw: FakeResponse([])
    assert coingecko_top_by_market_cap(fetch=fetch) is None


def test_coingecko_top_by_market_cap_returns_none_on_network_exception():
    def fetch(url, **kw):
        raise ConnectionError("no network")

    assert coingecko_top_by_market_cap(fetch=fetch) is None


def test_deribit_option_oi_by_strike_sums_calls_and_puts():
    payload = {
        "result": [
            {"instrument_name": "BTC-28AUG26-60000-C", "open_interest": 10.0},
            {"instrument_name": "BTC-28AUG26-60000-P", "open_interest": 5.0},
            {"instrument_name": "BTC-27DEC26-70000-C", "open_interest": 2.0},
        ]
    }
    fetch = lambda url, **kw: FakeResponse(payload)
    result = deribit_option_oi_by_strike("BTC", fetch=fetch)
    assert result == {60000.0: 15.0, 70000.0: 2.0}


def test_deribit_option_oi_by_strike_skips_malformed_names():
    payload = {"result": [{"instrument_name": "garbage", "open_interest": 10.0}]}
    fetch = lambda url, **kw: FakeResponse(payload)
    assert deribit_option_oi_by_strike("BTC", fetch=fetch) is None


def test_deribit_option_oi_by_strike_returns_none_on_error():
    def fetch(url, **kw):
        raise ConnectionError("down")

    assert deribit_option_oi_by_strike("BTC", fetch=fetch) is None


_RSS_PAYLOAD = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>SEC approves spot Bitcoin ETF</title><link>https://example.com/1</link></item>
<item><title>Exchange hacked, $10M drained</title><link>https://example.com/2</link></item>
</channel></rss>"""


class FakeRssResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def test_crypto_news_headlines_parses_titles_and_links():
    fetch = lambda url, **kw: FakeRssResponse(_RSS_PAYLOAD)
    headlines = crypto_news_headlines(fetch=fetch)
    assert headlines == [
        {"title": "SEC approves spot Bitcoin ETF", "link": "https://example.com/1"},
        {"title": "Exchange hacked, $10M drained", "link": "https://example.com/2"},
    ]


def test_crypto_news_headlines_respects_limit():
    fetch = lambda url, **kw: FakeRssResponse(_RSS_PAYLOAD)
    headlines = crypto_news_headlines(limit=1, fetch=fetch)
    assert len(headlines) == 1


def test_crypto_news_headlines_returns_none_on_error():
    def fetch(url, **kw):
        raise ConnectionError("down")

    assert crypto_news_headlines(fetch=fetch) is None


# ---- binance_candles / candles_with_binance_fallback -----------------------
# No network: every test injects a fake exchange_factory / venue_client
# rather than hitting Binance or a real venue.

def _candle_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n, "close": [1.0] * n, "volume": [1.0] * n,
    })


class FakeExchange:
    def __init__(self, df=None, raise_exc=None):
        self._df = df if df is not None else _candle_df()
        self._raise = raise_exc
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit=200):
        self.calls.append((symbol, timeframe, limit))
        if self._raise:
            raise self._raise
        return self._df


class FakeVenueClient:
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
    binance_candles("15m", lookback_hours=48, symbol="BTC/USDT", exchange_factory=lambda: fake)
    assert fake.calls == [("BTC/USDT", "15m", 193)]  # 48*60/15 + 1


def test_binance_candles_caps_limit_at_1000():
    fake = FakeExchange()
    binance_candles("1m", lookback_hours=24 * 220, symbol="BTC/USDT", exchange_factory=lambda: fake)
    assert fake.calls[0][2] == 1000


def test_binance_candles_returns_same_shape_as_other_venues():
    df = _candle_df(5)
    fake = FakeExchange(df=df)
    result = binance_candles("15m", 48, exchange_factory=lambda: fake)
    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(result) == 5


def test_candles_with_binance_fallback_prefers_venue_client_when_it_succeeds():
    venue_df = _candle_df(2)
    venue_client = FakeVenueClient(df=venue_df)
    fake_exchange = FakeExchange()
    result = candles_with_binance_fallback(venue_client, "BTC", "15m", 48, exchange_factory=lambda: fake_exchange)
    assert result is venue_df
    assert fake_exchange.calls == []  # never touched Binance


def test_candles_with_binance_fallback_uses_binance_when_venue_client_raises():
    venue_client = FakeVenueClient(raise_exc=RuntimeError("no candles"))
    binance_df = _candle_df(4)
    fake_exchange = FakeExchange(df=binance_df)
    result = candles_with_binance_fallback(venue_client, "BTC", "15m", 48, exchange_factory=lambda: fake_exchange)
    assert result is binance_df
    assert fake_exchange.calls[0][0] == "BTC/USDT"


def test_candles_with_binance_fallback_uses_binance_when_venue_client_is_none():
    binance_df = _candle_df(1)
    fake_exchange = FakeExchange(df=binance_df)
    result = candles_with_binance_fallback(None, "ETH", "1h", 200, exchange_factory=lambda: fake_exchange)
    assert result is binance_df
    assert fake_exchange.calls[0][0] == "ETH/USDT"


def test_candles_with_binance_fallback_returns_none_when_both_fail():
    venue_client = FakeVenueClient(raise_exc=RuntimeError("down"))
    fake_exchange = FakeExchange(raise_exc=RuntimeError("also down"))
    result = candles_with_binance_fallback(venue_client, "BTC", "15m", 48, exchange_factory=lambda: fake_exchange)
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
