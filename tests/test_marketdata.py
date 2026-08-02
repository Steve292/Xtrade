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

from bot.marketdata import (
    coingecko_category_cap,
    coingecko_category_change_24h,
    coingecko_global,
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
