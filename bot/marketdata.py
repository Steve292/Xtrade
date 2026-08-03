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

_UA = {"User-Agent": "Mozilla/5.0 (compatible; TraderX/1.0)"}
_TIMEOUT = 10


def _default_fetch(url: str, **kwargs):
    return requests.get(url, **kwargs)


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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
        return None


def coingecko_top_by_market_cap(limit: int = 10, fetch=_default_fetch) -> list[dict] | None:
    """Top `limit` coins ranked by market cap, via CoinGecko's public (no-key)
    markets endpoint — the same leaderboard CoinMarketCap's homepage shows
    (rank, price, 24h %, market cap, volume), for the dashboard's "Top by
    Market Cap" widget. Purely informational: this feed is never consulted
    by the watchlist or screening/entry logic (see config.yaml's fixed
    majors/memecoins list for what the bot actually trades)."""
    try:
        resp = fetch(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": limit,
                "page": 1,
                "price_change_percentage": "24h",
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
                "change_24h_pct": r.get("price_change_percentage_24h"),
                "market_cap": r.get("market_cap"),
                "volume_24h": r.get("total_volume"),
            }
            for r in rows
        ]
        return out or None
    except Exception:
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
    except Exception:
        return None
