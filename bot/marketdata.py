"""
Free, no-API-key market data backing the regime/hotness engines.

Every function here does real network I/O against a public endpoint and
returns plain numbers — no scoring/signal logic lives here, matching the
existing split between "talk to the venue" (bot/hyperliquid/client.py,
bot/mt5/client.py) and "decide what to do" (bot/regime.py, bot/hotness.py).
`fetch` is injectable on every call so callers — and every test — never hit
the real network.

Checked against this project's .env.example before writing this: there are
no Glassnode / CoinGlass / LunarCrush keys anywhere in this repo. Those would
be the real sources for ETF net flow, exchange reserves, GEX, liquidation
heatmaps, and social/narrative data — none of which have a free equivalent,
so this module deliberately does not attempt to fake them. Callers treat
those as optional inputs (see bot/regime.py, bot/hotness.py) until a real key
is wired in.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import requests

from bot.exchange import Exchange

_UA = {"User-Agent": "Mozilla/5.0 (compatible; TraderX/1.0)"}
_TIMEOUT = 10

_BINANCE_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _default_fetch(url: str, **kwargs):
    return requests.get(url, **kwargs)

def _log_fetch_failure(source: str, e: Exception) -> None:
    """One line, everywhere a fetch degrades to None. Every function in this
    module returns None on ANY failure by design -- a transient outage must
    degrade the caller's weighting, never crash the regime/hotness engines --
    but until this existed that degradation was silent. A source that has
    been failing for days looked identical to one that has never been
    reachable at all, with no way to tell the two apart from the logs."""
    print(f"  [marketdata] {source} unavailable ({type(e).__name__}: {str(e)[:120]})")


def yahoo_change_pct(
    symbol: str,
    bars_back: int = 1,
    interval: str = "1d",
    range_: str = "5d",
    fetch=_default_fetch,
) -> float | None:
    """% change of `symbol`'s close from `bars_back` bars ago to the latest
    close, via Yahoo Finance's public chart API (no key, but needs a
    browser-like User-Agent or it 429s). Returns None on any fetch/shape
    problem — a transient outage degrades the caller's weighting, it must
    never crash the regime engine."""
    try:
        resp = fetch(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": range_, "interval": interval},
            headers=_UA,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) <= bars_back:
            return None
        prior, latest = closes[-1 - bars_back], closes[-1]
        if not prior:
            return None
        return (latest - prior) / prior * 100
    except Exception as e:
        _log_fetch_failure("yahoo_change_pct", e)
        return None


def yahoo_level(symbol: str, fetch=_default_fetch) -> float | None:
    """Latest traded level for `symbol` (e.g. VIX's raw level rather than a
    % change)."""
    try:
        resp = fetch(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": "5d", "interval": "1d"},
            headers=_UA,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except Exception as e:
        _log_fetch_failure("yahoo_level", e)
        return None


def coingecko_global(fetch=_default_fetch) -> dict | None:
    """Live snapshot: total crypto market cap + per-coin dominance %. No
    history is available on the free tier — bot/timeseries.py bootstraps a
    trend from repeated snapshots instead of needing one."""
    try:
        resp = fetch("https://api.coingecko.com/api/v3/global", headers=_UA, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()["data"]
        return {
            "total_market_cap_usd": float(data["total_market_cap"]["usd"]),
            "market_cap_percentage": {k: float(v) for k, v in data["market_cap_percentage"].items()},
        }
    except Exception as e:
        _log_fetch_failure("coingecko_global", e)
        return None


def coingecko_category_cap(category_id: str, fetch=_default_fetch) -> float | None:
    """Market cap (USD) for one CoinGecko category — e.g. "meme-token" or
    "stablecoins" — the free source for MEME.D / STABLE.C and (combined with
    coingecko_global's total) SSR."""
    try:
        resp = fetch(
            "https://api.coingecko.com/api/v3/coins/categories", headers=_UA, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        for row in resp.json():
            if row.get("id") == category_id:
                return float(row["market_cap"])
        return None
    except Exception as e:
        _log_fetch_failure("coingecko_category_cap", e)
        return None


def coingecko_category_change_24h(category_id: str, fetch=_default_fetch) -> float | None:
    """24h % change of a CoinGecko category's total market cap — the free
    source for the blueprint's "MEME.D Trend" / narrative-momentum inputs."""
    try:
        resp = fetch(
            "https://api.coingecko.com/api/v3/coins/categories", headers=_UA, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        for row in resp.json():
            if row.get("id") == category_id:
                return float(row["market_cap_change_24h"])
        return None
    except Exception as e:
        _log_fetch_failure("coingecko_category_change_24h", e)
        return None


def deribit_option_oi_by_strike(currency: str = "BTC", fetch=_default_fetch) -> dict[float, float] | None:
    """Open interest summed across calls+puts per strike, from Deribit's
    public (no-key) options book summary — the free source for a GEX-style
    "flip zone" proxy. NOT real gamma exposure: that needs dealers' actual
    signed positioning, which nobody publishes (paid GEX products infer/model
    it, they don't observe it) — see bot/smart_money.py's gex_signal for how
    this gets used honestly."""
    try:
        resp = fetch(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            params={"currency": currency, "kind": "option"},
            headers=_UA,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()["result"]
        by_strike: dict[float, float] = {}
        for row in rows:
            parts = (row.get("instrument_name") or "").split("-")
            if len(parts) != 4:  # {currency}-{expiry}-{strike}-{C|P}
                continue
            try:
                strike = float(parts[2])
            except ValueError:
                continue
            by_strike[strike] = by_strike.get(strike, 0.0) + float(row.get("open_interest") or 0)
        return by_strike or None
    except Exception as e:
        _log_fetch_failure("deribit_option_oi_by_strike", e)
        return None


def crypto_news_headlines(limit: int = 30, fetch=_default_fetch) -> list[dict] | None:
    """Recent crypto headlines from CoinTelegraph's public RSS feed (free,
    no key). Real, current news — verified live while building this: the
    top headline was literally about the CLARITY Act. Returns None on any
    fetch/parse problem rather than raising."""
    try:
        resp = fetch("https://cointelegraph.com/rss", headers=_UA, timeout=_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        out = []
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            if title:
                out.append({"title": title, "link": (item.findtext("link") or "").strip()})
        return out or None
    except Exception as e:
        _log_fetch_failure("crypto_news_headlines", e)
        return None


def coingecko_top_by_market_cap(limit: int = 10, fetch=_default_fetch) -> list[dict] | None:
    """Top `limit` coins ranked by market cap, via CoinGecko's public (no-key)
    markets endpoint — the same leaderboard CoinMarketCap's homepage shows
    (rank, price, 1h %, 24h %, market cap, volume), for the dashboard's "Top
    by Market Cap" widget. Purely informational: this feed is never consulted
    by the watchlist or screening/entry logic (see config.yaml's fixed
    majors/memecoins list for what the bot actually trades).

    Note the two different 24h fields CoinGecko returns. `price_change_
    percentage_24h` is always present; the `_in_currency` variants only
    appear for the windows named in the `price_change_percentage` parameter.
    1h has no plain form at all, so it must be read from
    `price_change_percentage_1h_in_currency` — reading `..._1h` would
    silently yield None on every row."""
    try:
        resp = fetch(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": limit,
                "page": 1,
                "price_change_percentage": "1h,24h",
            },
            headers=_UA,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        out = [
            {
                "rank": r.get("market_cap_rank"),
                "symbol": (r.get("symbol") or "").upper(),
                "name": r.get("name"),
                "price": r.get("current_price"),
                "change_1h_pct": r.get("price_change_percentage_1h_in_currency"),
                "change_24h_pct": r.get("price_change_percentage_24h"),
                "market_cap": r.get("market_cap"),
                "volume_24h": r.get("total_volume"),
            }
            for r in rows
        ]
        return out or None
    except Exception as e:
        _log_fetch_failure("coingecko_top_by_market_cap", e)
        return None


def coingecko_top_movers_avg_7d_pct(category_id: str, top_n: int = 10, fetch=_default_fetch) -> float | None:
    """Average 7-day return of the top `top_n` coins (by market cap) in a
    CoinGecko category — the free source for "MEME.C Momentum" (avg 7D
    return of the top 10 memes)."""
    try:
        resp = fetch(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "category": category_id,
                "order": "market_cap_desc",
                "per_page": top_n,
                "page": 1,
                "price_change_percentage": "7d",
            },
            headers=_UA,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        returns = [
            float(r["price_change_percentage_7d_in_currency"])
            for r in rows
            if r.get("price_change_percentage_7d_in_currency") is not None
        ]
        if not returns:
            return None
        return sum(returns) / len(returns)
    except Exception as e:
        _log_fetch_failure("coingecko_top_movers_avg_7d_pct", e)
        return None


def binance_symbol(coin: str) -> str:
    """Hyperliquid coin name -> Binance spot symbol. Hyperliquid k-prefixes
    1000x-denominated tokens ("kPEPE", "kBONK", "kSHIB" -- see config.yaml's
    memecoins comment); Binance lists the plain token, so the prefix is
    stripped before appending /USDT."""
    base = coin[1:] if coin.startswith("k") and len(coin) > 1 and coin[1].isupper() else coin
    return f"{base}/USDT"


def _default_binance_exchange() -> Exchange:
    return Exchange(exchange_id="binance", mode="paper")


def binance_candles(
    interval: str, lookback_hours: int, symbol: str = "BTC/USDT", exchange_factory=_default_binance_exchange
):
    """OHLCV candles from Binance via ccxt (bot/exchange.py, already used by
    backtest.py/scan.py) -- same DataFrame shape (timestamp/open/high/low/
    close/volume) as HyperliquidClient.candles() and MT5Client.copy_rates(),
    so this is a drop-in substitute wherever either of those is used.
    `exchange_factory` is injectable so tests never hit the real network."""
    minutes = _BINANCE_INTERVAL_MINUTES.get(interval, 15)
    limit = min(1000, max(2, int(lookback_hours * 60 / minutes) + 1))
    return exchange_factory().fetch_ohlcv(symbol, interval, limit=limit)


def candles_with_binance_fallback(
    venue_client, coin: str, interval: str, lookback_hours: int, exchange_factory=_default_binance_exchange
):
    """`venue_client.candles(coin, interval=interval, lookback_hours=lookback_hours)`,
    falling back to Binance on any failure (rate limit, connection reset,
    `venue_client` itself being None). BTC/ETH/majors trade at near-identical
    prices across venues, so Binance is a legitimate stand-in specifically
    for a coin's own price-derived technical checks -- never a substitute
    for anything venue-specific (positions, funding rate, order book, actual
    fill price), which is why callers only use this for candles feeding
    signal generation, not execution itself.

    `venue_client` must expose `.candles(coin, interval=..., lookback_hours=...)`
    (HyperliquidClient's signature) -- callers with a different-shaped client
    (e.g. MT5Client.copy_rates) should call binance_candles() directly instead,
    since there's no real Binance equivalent for forex/commodity symbols anyway."""
    if venue_client is not None:
        try:
            return venue_client.candles(coin, interval=interval, lookback_hours=lookback_hours)
        except Exception as e:
            _log_fetch_failure("candles_with_binance_fallback (venue, falling back to Binance)", e)
    try:
        return binance_candles(interval, lookback_hours, symbol=f"{coin}/USDT", exchange_factory=exchange_factory)
    except Exception as e:
        _log_fetch_failure("candles_with_binance_fallback", e)
        return None
