#!/usr/bin/env python3
"""
Local control panel for the SMC Hyperliquid bot — scan on demand, view
positions, arm or disarm live trading. Binds to 127.0.0.1 only; never
exposed to the network.

Every read endpoint (/api/status, /api/scan, /api/positions) is read-only —
it queries the venue directly, independent of whatever the 24/7 trading
loop process is doing. The only state-changing endpoints are:
  - POST /api/activate — flips the shared runtime arm/disarm flag
    (bot/live_state.py) that the trading loop checks every pass.
  - POST /api/fire — places a real order, but ONLY for a coin whose signal
    re-validates as fully approved in THIS SAME request, right now — never
    a bypass of the seven-gate screen, and it re-checks the armed flag and
    capital guard server-side regardless of what the browser sends.

    python webapp/server.py
"""

from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from bot import live_state
from bot.capital_guard import CapitalGuard
from bot.combined_ledger import fetch_combined_balance
from bot.hyperliquid.client import HyperliquidClient
from bot.hyperliquid.trader import HyperliquidTrader
from bot.exchange import Exchange
from bot.market_snapshot import compute_snapshot
from bot.marketdata import binance_symbol, coingecko_top_by_market_cap
from bot.mt5.client import MT5Client
from bot import pending_trades
from bot.position_sizing import risk_pct_for_fixed_usd, staged_fixed_risk_usd
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.strategy import SMCStrategy, SignalType
from bot.unified_screen import evaluate_unified
from bot.wallet import DefiWallet

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
with open(ROOT / "config.yaml") as f:
    CFG = yaml.safe_load(f) or {}
HL_CFG = CFG.get("hyperliquid", {})
WATCHLIST = list(dict.fromkeys((HL_CFG.get("majors") or []) + (HL_CFG.get("memecoins") or [])))
MT5_CENT_DIVISOR = 100.0 if CFG.get("mt5_cent_account") else 1.0

NEWS_FEED_URL = "https://investinglive.com/feed/news"
NEWS_CACHE_SECONDS = 300  # headlines don't need re-fetching every 15s poll
_news_cache: dict = {"items": [], "fetched_at": 0.0}

# Regime/hotness data comes from free third-party endpoints (Yahoo, CoinGecko)
# with real rate limits — the blueprint this implements calls for a 4h update
# cadence anyway, so a 15-minute server-side cache is both correct and safe
# regardless of how often the dashboard itself polls this endpoint.
REGIME_CACHE_SECONDS = 900
_regime_cache: dict = {"data": None, "fetched_at": 0.0}

# CoinGecko's free tier is rate-limited (roughly 10-30 calls/min, shared
# across every free caller worldwide) — a 60s server-side cache keeps any
# number of open dashboard tabs to one upstream call/minute between them.
TOP_MCAP_CACHE_SECONDS = 60
_top_mcap_cache: dict = {"rows": [], "fetched_at": 0.0}


# One batch ccxt fetch_tickers() call regardless of watchlist size, so the
# rate-limit cost of caching this is trivial either way -- cached anyway so
# repeat polls (any number of open tabs) don't re-hit Binance every 15s.
WATCHLIST_CACHE_SECONDS = 30
_watchlist_cache: dict = {"rows": [], "fetched_at": 0.0}

app = Flask(__name__, static_folder=None)


@app.errorhandler(Exception)
def handle_venue_error(e):
    """A transient venue error (DNS blip, timeout, rate limit) must not surface
    as a raw 500/traceback to the browser — same "log it, don't crash" stance
    hypertrade.py's own loop takes on the exact same class of errors. 503 so
    the frontend's periodic status/position polls read as "temporarily
    unreachable, try again" rather than a broken app. Real HTTP errors (404 on
    an unknown route, etc.) pass through unchanged — this is only for
    unexpected failures inside a route's own logic."""
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("request failed")
    return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 503


def _client() -> HyperliquidClient:
    wallet = DefiWallet.from_env() or DefiWallet.load()
    return HyperliquidClient.connect(
        private_key=wallet.private_key if wallet else "",
        testnet=HL_CFG.get("testnet", True),
    )


def _pct_return(pnl: float, current_balance: float) -> float | None:
    """% return over a window, against the balance at the START of the window,
    approximated as (current_balance - pnl) since we only have discrete trade
    events, not a continuously recorded balance history.

    Returns None (shown as "—") when that derived start balance is non-positive.
    That happens when the window's realized P&L exceeds the current balance —
    i.e. deposits/withdrawals moved the balance and the trade-only approximation
    breaks down. Reporting a percentage there would be nonsense (a positive
    dollar gain divided by a negative base yields a negative %), so we decline
    to show a % rather than a misleading one. The dollar figure stays correct."""
    base = current_balance - pnl
    if base <= 1e-9:
        return None
    return pnl / base * 100


def _trader(client: HyperliquidClient) -> HyperliquidTrader:
    strategy = SMCStrategy(
        swing_lookback=CFG.get("swing_lookback", 5),
        order_block_lookback=CFG.get("order_block_lookback", 20),
        fvg_min_size_pct=CFG.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=CFG.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=CFG.get("reward_risk_ratio", 2.0),
        stop_loss_pct=CFG.get("stop_loss_pct"),
    )
    screener = TradeScreener(ScreenConfig.from_dict(CFG.get("screening", {})))
    guard_cfg = CFG.get("capital_guard", {})
    guard = CapitalGuard.load(**{k: guard_cfg[k] for k in CapitalGuard.__dataclass_fields__ if k in guard_cfg})

    # Same staged fixed-dollar risk the 24/7 loops (hypertrade.py,
    # bot/runner.py) apply, so a manual Fire from this dashboard sizes
    # identically to what the autonomous loop would have done. Best-effort:
    # an unreachable MT5 bridge just falls back to risk_per_trade_pct,
    # same as the loops do on a failed combined-balance fetch.
    risk_pct = CFG.get("risk_per_trade_pct", 1.0)
    fixed_risk_cfg = CFG.get("fixed_risk_usd", {})
    if fixed_risk_cfg.get("enabled"):
        try:
            mt5_cent_divisor = 100.0 if CFG.get("mt5_cent_account") else 1.0
            combined = fetch_combined_balance(client, _mt5_client(), mt5_cent_divisor)
            if combined is not None:
                account_value = client.account().account_value
                risk_usd = staged_fixed_risk_usd(
                    combined.total,
                    low_risk_usd=fixed_risk_cfg.get("low", 3.0),
                    high_risk_usd=fixed_risk_cfg.get("high", 6.0),
                    threshold_usd=fixed_risk_cfg.get("threshold_usd", 100.0),
                )
                risk_pct = risk_pct_for_fixed_usd(risk_usd, account_value)
        except Exception:
            pass  # MT5 unreachable from the dashboard process — fall back to risk_per_trade_pct

    return HyperliquidTrader(
        client, strategy, screener,
        risk_pct=risk_pct,
        leverage=HL_CFG.get("default_leverage", 3),
        capital_guard=guard,
    )


def _mt5_client() -> MT5Client:
    return MT5Client.connect(
        host=os.getenv("MT5_HOST", "127.0.0.1"),
        port=os.getenv("MT5_PORT", "18812"),
        login=os.getenv("MT5_LOGIN", ""),
        password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""),
        terminal_path=os.getenv("MT5_TERMINAL_PATH", ""),
    )


def _symbol_ticker(mt5: MT5Client, symbol: str) -> dict:
    """Live price + 24h/7d % change for one MT5 symbol. Hourly candles are
    used as a stand-in for exact time offsets (N candles back = N hours ago),
    which holds cleanly for symbols that trade continuously — for FX/weekend-
    closed pairs the underlying candle count may run short near a weekend."""
    bid, ask = mt5.tick(symbol)
    mid = (bid + ask) / 2
    out = {"symbol": symbol, "bid": bid, "ask": ask, "mid": mid}
    try:
        candles = mt5.copy_rates(symbol, "1h", count=200)
        if len(candles) > 24:
            price_24h_ago = float(candles.iloc[-25]["close"])
            out["change_24h_pct"] = (mid - price_24h_ago) / price_24h_ago * 100
        if len(candles) > 168:
            price_7d_ago = float(candles.iloc[-169]["close"])
            out["change_7d_pct"] = (mid - price_7d_ago) / price_7d_ago * 100
    except Exception:
        pass
    return out


@app.get("/")
def index():
    return send_from_directory(Path(__file__).parent, "dashboard.html")


@app.get("/performance")
def performance_page():
    return send_from_directory(Path(__file__).parent, "performance.html")


@app.get("/api/performance")
def performance():
    """Read-only performance snapshot for the live MT5 account — the account
    actually being traded right now. Hyperliquid is deliberately excluded
    (tracked separately via /api/status and /api/positions on the control
    panel) so these P&L figures reflect one real account, not a blend."""
    out: dict = {}

    try:
        mt5 = _mt5_client()
        raw_balance = mt5.account_balance()
        balance_usd = raw_balance / MT5_CENT_DIVISOR
        deals = mt5.closed_deals(days=30)
        realized = sum(d["profit"] for d in deals)
        wins = sum(1 for d in deals if d["profit"] > 0)
        running = 0.0
        curve = []
        for d in sorted(deals, key=lambda d: d["time"]):
            running += d["profit"]
            curve.append({"time": d["time"] * 1000, "value": running / MT5_CENT_DIVISOR})

        now_s = time.time()
        pnl_24h = sum(d["profit"] for d in deals if d["time"] >= now_s - 24 * 3600) / MT5_CENT_DIVISOR
        pnl_7d = sum(d["profit"] for d in deals if d["time"] >= now_s - 7 * 24 * 3600) / MT5_CENT_DIVISOR

        # Bug fix: all_positions() returns raw account units (cents for a cent
        # account) — this was previously passed straight to the frontend and
        # displayed with a "$" prefix, showing ~100x the real dollar P&L.
        open_positions = mt5.all_positions()
        for p in open_positions:
            p["profit"] = p["profit"] / MT5_CENT_DIVISOR
        unrealized_usd = sum(p["profit"] for p in open_positions)
        realized_usd = realized / MT5_CENT_DIVISOR

        watchlist_symbols = CFG.get("mt5_watchlist") or [CFG.get("mt5_symbol", "EURUSDc")]
        watchlist = []
        for sym in watchlist_symbols:
            try:
                row = _symbol_ticker(mt5, sym)
            except Exception as e:
                row = {"symbol": sym, "error": f"{type(e).__name__}: {str(e)[:120]}"}
            row["venue"] = "mt5"
            watchlist.append(row)
        btc_price = next((w for w in watchlist if w.get("symbol") == "BTCUSDc" and "mid" in w), None)

        out["mt5"] = {
            "balance_usd": balance_usd,
            "open_positions": open_positions,
            "realized_pnl_usd": realized_usd,
            "unrealized_pnl_usd": unrealized_usd,
            "live_pnl_usd": realized_usd + unrealized_usd,
            "trade_count": len(deals),
            "wins": wins,
            "win_rate": (wins / len(deals)) if deals else None,
            "pnl_24h_usd": pnl_24h,
            "pnl_7d_usd": pnl_7d,
            "pnl_24h_pct": _pct_return(pnl_24h, balance_usd),
            "pnl_7d_pct": _pct_return(pnl_7d, balance_usd),
            "equity_curve": curve,
            "btc_price": btc_price,
            "watchlist": watchlist,
            "recent_trades": [
                {"venue": "mt5", "symbol": d["symbol"], "side": "",
                 "pnl": d["profit"] / MT5_CENT_DIVISOR, "price": d["price"],
                 "time": d["time"] * 1000}
                for d in deals[:50]
            ],
        }
    except Exception as e:
        out["mt5"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    # Merged watchlist: MT5 (FX/metals/crypto CFDs) + Hyperliquid (crypto perps)
    # in one list, each row venue-tagged. Built independently of the P&L blocks
    # above so it still populates if one venue is momentarily unreachable. BTC
    # and ETH appear on both venues by design — same asset, different market.
    merged: list[dict] = []
    mt5_data = out.get("mt5", {})
    if isinstance(mt5_data, dict) and mt5_data.get("watchlist"):
        merged.extend(mt5_data["watchlist"])
    try:
        hl_coins = list(dict.fromkeys((HL_CFG.get("majors") or []) + (HL_CFG.get("memecoins") or [])))
        if hl_coins:
            hl_client = _client()
            for row in hl_client.watchlist_tickers(hl_coins):
                row["venue"] = "hl"
                merged.append(row)
    except Exception as e:
        out["watchlist_hl_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    out["watchlist"] = merged

    return jsonify(out)


@app.get("/api/news")
def news():
    """Live forex/market news headlines, server-side cached for
    NEWS_CACHE_SECONDS — no need to re-hit the upstream feed on every
    dashboard poll, and a stale cache is served if the feed is unreachable."""
    now = time.time()
    if now - _news_cache["fetched_at"] > NEWS_CACHE_SECONDS:
        try:
            resp = requests.get(NEWS_FEED_URL, timeout=8)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = []
            for item in root.findall(".//item")[:15]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date_raw = (item.findtext("pubDate") or "").strip()
                pub_ms = None
                if pub_date_raw:
                    try:
                        pub_ms = int(parsedate_to_datetime(pub_date_raw).timestamp() * 1000)
                    except ValueError:
                        pub_ms = None
                if title:
                    items.append({"title": title, "link": link, "time": pub_ms})
            _news_cache["items"] = items
            _news_cache["fetched_at"] = now
        except Exception as e:
            if not _news_cache["items"]:
                return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}", "items": []}), 503
            # Stale cache beats no news — the fetch error is logged, not surfaced.
            app.logger.warning("news feed refresh failed, serving stale cache: %s", e)

    return jsonify({"items": _news_cache["items"], "fetched_at": _news_cache["fetched_at"]})


@app.get("/api/regime")
def api_regime():
    """Read-only Section 1-4 snapshot (regime score, hotness signal, meme
    season score, smart-money module aggregate) — informational only.
    Nothing here changes position sizing or entry decisions yet; see
    bot/position_sizing.py's docstring for why that's a deliberately
    separate, not-yet-taken step. Computation lives in bot/market_snapshot.py
    so this dashboard and the read-only MCP server (mcp_server/server.py)
    share one implementation."""
    now = time.time()
    if _regime_cache["data"] is None or now - _regime_cache["fetched_at"] > REGIME_CACHE_SECONDS:
        try:
            _regime_cache["data"] = compute_snapshot(CFG)
            _regime_cache["fetched_at"] = now
        except Exception as e:
            if _regime_cache["data"] is None:
                return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 503
            app.logger.warning("regime refresh failed, serving stale cache: %s", e)
    return jsonify(_regime_cache["data"])


@app.get("/api/top-market-cap")
def api_top_market_cap():
    """Read-only top-20-by-market-cap leaderboard (rank, price, 1h %, 24h %,
    market cap, volume) via CoinGecko — the same table CoinMarketCap's
    homepage shows. Purely a dashboard display: this feed never touches the
    watchlist or screening/entry logic, which stays config.yaml's fixed
    majors/memecoins list."""
    now = time.time()
    if not _top_mcap_cache["rows"] or now - _top_mcap_cache["fetched_at"] > TOP_MCAP_CACHE_SECONDS:
        rows = coingecko_top_by_market_cap(limit=20)
        if rows:
            _top_mcap_cache["rows"] = rows
            _top_mcap_cache["fetched_at"] = now
        elif not _top_mcap_cache["rows"]:
            return jsonify({"error": "CoinGecko fetch failed", "rows": []}), 503
        else:
            app.logger.warning("top-market-cap refresh failed, serving stale cache")
    return jsonify({"rows": _top_mcap_cache["rows"], "fetched_at": _top_mcap_cache["fetched_at"]})


@app.get("/api/watchlist")
def api_watchlist():
    """Read-only live snapshot of the coins actually on the trading
    watchlist (config.yaml majors+memecoins), sourced from Binance --
    distinct from /api/top-market-cap's CoinGecko top-20-by-market-cap,
    which includes coins this bot never trades. A coin with no Binance spot
    listing (e.g. HYPE, Hyperliquid's own token) is reported with
    available=False rather than silently dropped."""
    now = time.time()
    if not _watchlist_cache["rows"] or now - _watchlist_cache["fetched_at"] > WATCHLIST_CACHE_SECONDS:
        try:
            symbols = [binance_symbol(c) for c in WATCHLIST]
            # No explicit `symbols` filter: ccxt's binance.fetch_tickers raises
            # BadSymbol if ANY requested symbol isn't a real Binance market
            # (e.g. HYPE/USDT, Hyperliquid's own token) rather than omitting
            # it -- fetching everything and looking up by key sidesteps that
            # entirely and is still exactly one HTTP call either way.
            tickers = Exchange(exchange_id="binance", mode="paper").client.fetch_tickers()
            rows = []
            for coin, symbol in zip(WATCHLIST, symbols):
                t = tickers.get(symbol)
                if t is None:
                    rows.append({"coin": coin, "symbol": symbol, "available": False})
                    continue
                rows.append({
                    "coin": coin, "symbol": symbol, "available": True,
                    "price": t.get("last"), "change_24h_pct": t.get("percentage"),
                    "high_24h": t.get("high"), "low_24h": t.get("low"),
                    "volume_24h": t.get("quoteVolume"),
                })
            _watchlist_cache["rows"] = rows
            _watchlist_cache["fetched_at"] = now
        except Exception as e:
            if not _watchlist_cache["rows"]:
                return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}", "rows": []}), 503
            app.logger.warning("watchlist refresh failed, serving stale cache: %s", e)
    return jsonify({"rows": _watchlist_cache["rows"], "fetched_at": _watchlist_cache["fetched_at"]})


@app.get("/api/coin-chart")
def api_coin_chart():
    """Read-only recent candles + windowed stats for ONE coin, for the
    dashboard's click-a-watchlist-row detail panel. venue=crypto sources
    Binance (same coin mapping as /api/watchlist); venue=mt5 sources the
    MT5 bridge directly -- forex/commodities aren't listed on Binance, so
    that IS the live venue here, not a fallback source.

    change/high/low are computed over the returned candle window, not a
    strict trailing-24h clock -- same documented approximation _symbol_ticker
    already makes for the MT5 signals table."""
    symbol = request.args.get("symbol", "")
    venue = request.args.get("venue", "crypto")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        if venue == "mt5":
            mt5 = _mt5_client()
            ltf = CFG.get("mt5_timeframe", CFG.get("timeframe", "15m"))
            df = mt5.copy_rates(symbol, ltf, count=100)
            bid, ask = mt5.tick(symbol)
            last = (bid + ask) / 2
        else:
            df = Exchange(exchange_id="binance", mode="paper").fetch_ohlcv(
                binance_symbol(symbol), CFG.get("timeframe", "15m"), limit=100
            )
            last = float(df.iloc[-1]["close"]) if len(df) else None

        candles = [
            {"time": int(row["timestamp"].timestamp() * 1000), "close": float(row["close"])}
            for _, row in df.iterrows()
        ]
        change_pct = high = low = None
        if len(df) > 1:
            closes = df["close"].astype(float)
            high = float(df["high"].astype(float).max())
            low = float(df["low"].astype(float).min())
            first = float(closes.iloc[0])
            if first:
                change_pct = (float(closes.iloc[-1]) - first) / first * 100
        return jsonify({
            "symbol": symbol, "venue": venue, "candles": candles,
            "last": last, "change_pct": change_pct, "high": high, "low": low,
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 503


@app.get("/api/status")
def status():
    client = _client()
    acct = client.account()

    # Read-only: reports whatever the live trading loop's own guard.update()
    # calls last persisted to capital_guard_state.json — this endpoint never
    # calls update() itself, so it can't invent or change the guard's state,
    # only display it. Previously computed and used internally (guard_check
    # on /api/fire) but never actually shown anywhere, so a silent halt was
    # invisible until you tried to fire and got rejected.
    guard_cfg = CFG.get("capital_guard", {})
    guard = CapitalGuard.load(**{k: guard_cfg[k] for k in CapitalGuard.__dataclass_fields__ if k in guard_cfg})

    return jsonify({
        "venue": "testnet" if HL_CFG.get("testnet", True) else "mainnet",
        "armed": live_state.is_armed(),
        "min_confidence": live_state.get_min_confidence(),
        "auto_fire_pct": live_state.get_auto_fire_pct(),
        "pending_count": len(pending_trades.list_pending()),
        "config_min_confidence": CFG.get("screening", {}).get("min_confidence", 0.55),
        "account_value": acct.account_value,
        "withdrawable": acct.withdrawable,
        "open_positions": len(acct.positions),
        "watchlist_size": len(WATCHLIST),
        "capital_guard": {
            "halted": guard.halted,
            "halt_reason": guard.halt_reason,
            "size_multiplier": guard.size_multiplier,
        },
    })


@app.post("/api/activate")
def activate():
    body = request.get_json(silent=True) or {}
    armed = bool(body.get("armed"))
    live_state.set_armed(armed)
    return jsonify({"armed": live_state.is_armed()})


@app.post("/api/threshold")
def threshold():
    """Set the runtime authorization floor — only signals at/above this
    confidence auto-fire. Applies to both venues' live loops (shared
    live_state), and is re-enforced server-side in /api/fire below."""
    body = request.get_json(silent=True) or {}
    try:
        value = float(body.get("min_confidence"))
    except (TypeError, ValueError):
        return jsonify({"error": "min_confidence must be a number 0..1"}), 400
    live_state.set_min_confidence(value)
    return jsonify({"min_confidence": live_state.get_min_confidence()})


@app.post("/api/autofire")
def autofire():
    """Set the hands-off threshold (0-100) against the unified gate's blended
    final_pct. At/above it a setup fires unattended; below it, it's queued for
    Approve/Cancel. Raising this to 100 effectively means "review everything"."""
    body = request.get_json(silent=True) or {}
    try:
        value = float(body.get("auto_fire_pct"))
    except (TypeError, ValueError):
        return jsonify({"error": "auto_fire_pct must be a number 0..100"}), 400
    live_state.set_auto_fire_pct(value)
    return jsonify({"auto_fire_pct": live_state.get_auto_fire_pct()})


def _get_smart_money() -> tuple[str, int, int]:
    """(direction, bullish_count, bearish_count) for the unified gate
    (bot/unified_screen.py) — reuses /api/regime's own 900s snapshot cache
    (_regime_cache) rather than fetching a second time, since both want the
    exact same market-wide read. Degrades to NEUTRAL (never blocks) on any
    failure, same as bot/runner.py and hypertrade.py's live loops."""
    now = time.time()
    if _regime_cache["data"] is None or now - _regime_cache["fetched_at"] > REGIME_CACHE_SECONDS:
        try:
            _regime_cache["data"] = compute_snapshot(CFG)
            _regime_cache["fetched_at"] = now
        except Exception:
            pass  # fall through to stale/absent cache below
    sm = (_regime_cache["data"] or {}).get("smart_money")
    if not sm:
        return "NEUTRAL", 0, 0
    return sm.get("direction", "NEUTRAL"), sm.get("bullish_count", 0), sm.get("bearish_count", 0)


def _scan_mt5_rows() -> list[dict]:
    """Same seven-gate screen /api/scan runs for Hyperliquid, applied to the
    MT5 watchlist — read-only, evaluates each symbol, never sizes or sends
    anything. Each row is tagged venue="mt5" so the merged /api/scan
    response can't be confused with the Hyperliquid rows.

    Uses the same strategy/screener config (including stop_loss_pct) as the
    live MT5 MODE=live loop (bot/runner.py), which now runs this identical
    seven-gate screen itself (unified since task #73) — so this dashboard
    scan reflects what the live bot would actually do, not a stricter
    preview of it."""
    mt5 = _mt5_client()
    strategy = SMCStrategy(
        swing_lookback=CFG.get("swing_lookback", 5),
        order_block_lookback=CFG.get("order_block_lookback", 20),
        fvg_min_size_pct=CFG.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=CFG.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=CFG.get("reward_risk_ratio", 2.0),
        stop_loss_pct=CFG.get("stop_loss_pct"),
    )
    screener = TradeScreener(ScreenConfig.from_dict(CFG.get("screening", {})))
    ltf = CFG.get("mt5_timeframe", CFG.get("timeframe", "15m"))
    htf = CFG.get("higher_timeframe", "1h")
    symbols = CFG.get("mt5_watchlist") or [CFG.get("mt5_symbol", "EURUSDc")]
    sm_direction, sm_bullish, sm_bearish = _get_smart_money()

    out = []
    for symbol in symbols:
        row = {"coin": symbol, "venue": "mt5"}
        try:
            df = mt5.copy_rates(symbol, ltf, count=200)
            htf_df = mt5.copy_rates(symbol, htf, count=100)
            signal = strategy.analyze(df, htf_df)
            if signal.type == SignalType.NONE:
                row.update(status="no_setup", reason=signal.reason)
                out.append(row)
                continue
            result = screener.screen(signal, df, htf_df)
            unified = evaluate_unified(signal, result, sm_direction, sm_bullish, sm_bearish)
            if unified.approved:
                row.update(
                    status="approved", side=signal.type.value, confidence=signal.confidence,
                    entry=signal.entry, stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                    checks=[{"name": c.name, "passed": c.passed, "detail": c.detail} for c in result.checks],
                    sizable=False,  # MT5 rows aren't lot-sized here (no account/broker context in this scan)
                    final_pct=unified.final_pct, smart_money_direction=unified.smart_money_direction,
                    smart_money_agreement=unified.smart_money_agreement_count,
                )
            elif not unified.structure_ok:
                failed = next((c.name for c in result.checks if not c.passed), "?")
                row.update(status="rejected", side=signal.type.value, confidence=signal.confidence, failed_gate=failed)
            else:
                row.update(status="rejected", side=signal.type.value, confidence=signal.confidence,
                           failed_gate=unified.reason)
        except Exception as e:
            row.update(status="error", error=f"{type(e).__name__}: {str(e)[:120]}")
        out.append(row)
    return out


@app.get("/api/scan")
def scan():
    client = _client()
    trader = _trader(client)
    acct = client.account()
    sm_direction, sm_bullish, sm_bearish = _get_smart_money()
    rows = trader.scan(
        WATCHLIST, CFG.get("timeframe", "15m"), CFG.get("higher_timeframe", "1h"),
        acct.account_value, acct.withdrawable, sm_direction, sm_bullish, sm_bearish,
    )
    out = []
    for coin, signal, result, plan, unified, err in rows:
        if err:
            out.append({"coin": coin, "venue": "hl", "status": "error", "error": err[:120]})
        elif signal.type == SignalType.NONE:
            # signal.reason carries the diagnostic clues (why no setup).
            out.append({"coin": coin, "venue": "hl", "status": "no_setup", "reason": signal.reason})
        elif unified.approved:
            out.append({
                "coin": coin, "venue": "hl", "status": "approved", "side": signal.type.value,
                "confidence": signal.confidence,
                "entry": signal.entry, "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
                "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in result.checks],
                "sizable": plan is not None,
                "usd": plan.usd if plan else None,
                "final_pct": unified.final_pct, "smart_money_direction": unified.smart_money_direction,
                "smart_money_agreement": unified.smart_money_agreement_count,
            })
        elif not unified.structure_ok:
            failed = next((c.name for c in result.checks if not c.passed), "?")
            out.append({
                "coin": coin, "venue": "hl", "status": "rejected", "side": signal.type.value,
                "confidence": signal.confidence, "failed_gate": failed,
            })
        else:
            out.append({
                "coin": coin, "venue": "hl", "status": "rejected", "side": signal.type.value,
                "confidence": signal.confidence, "failed_gate": unified.reason,
                "final_pct": unified.final_pct,
            })

    # Merged with the MT5 watchlist's own seven-gate scan — one combined
    # list, venue-tagged, same convention /api/performance's watchlist merge
    # already uses. Best-effort: an unreachable MT5 bridge shouldn't blank
    # out the Hyperliquid rows that already succeeded.
    try:
        out.extend(_scan_mt5_rows())
    except Exception as e:
        app.logger.warning("MT5 scan failed, showing Hyperliquid rows only: %s", e)

    return jsonify({"account_value": acct.account_value, "withdrawable": acct.withdrawable, "rows": out})


@app.get("/api/positions")
def positions():
    client = _client()
    acct = client.account()
    out = []
    for p in acct.positions:
        legs = [
            {"triggerPx": o.get("triggerPx"), "orderType": o.get("orderType")}
            for o in client.open_trigger_orders(p.coin)
        ]
        out.append({
            "coin": p.coin, "side": p.side, "size": p.size, "entry": p.entry,
            "unrealized_pnl": p.unrealized_pnl, "leverage": p.leverage, "brackets": legs,
        })
    return jsonify({"positions": out})


@app.post("/api/fire")
def fire():
    body = request.get_json(silent=True) or {}
    coin = body.get("coin")
    if not coin:
        return jsonify({"error": "coin required"}), 400

    client = _client()
    trader = _trader(client)
    acct = client.account()
    sm_direction, sm_bullish, sm_bearish = _get_smart_money()
    signal, result, plan, unified = trader.evaluate(
        coin, CFG.get("timeframe", "15m"), CFG.get("higher_timeframe", "1h"),
        acct.account_value, acct.withdrawable, sm_direction, sm_bullish, sm_bearish,
    )
    if not unified.approved:
        return jsonify({"error": f"{coin} is not currently approved — cannot fire ({unified.reason})"}), 400
    if plan is None:
        return jsonify({"error": f"{coin} approved but not sizable (free margin or $10 min)"}), 400
    if not live_state.is_armed():
        return jsonify({"error": "disarmed — flip Activate on first"}), 403
    allowed, reason = trader.guard_check(acct.account_value, confidence=signal.confidence)
    if not allowed:
        return jsonify({"error": f"blocked: {reason}"}), 403

    resp = trader.execute(plan)
    return jsonify({"result": resp, "plan": {"coin": coin, "side": plan.side, "usd": plan.usd}})


@app.get("/api/pending")
def pending_list():
    """Setups that cleared the unified gate but landed below the auto-fire
    threshold, awaiting Approve/Cancel. Read-only; expired entries are purged
    on read so the panel never shows a stale setup as actionable."""
    pending_trades.purge_expired()
    return jsonify({
        "pending": pending_trades.list_pending(),
        "auto_fire_pct": live_state.get_auto_fire_pct(),
        "now": time.time(),
    })


@app.post("/api/pending/cancel")
def pending_cancel():
    entry_id = (request.get_json(silent=True) or {}).get("id")
    if not entry_id:
        return jsonify({"error": "id required"}), 400
    entry = pending_trades.resolve(entry_id, "cancelled")
    if entry is None:
        return jsonify({"error": "already resolved or expired"}), 404
    return jsonify({"cancelled": entry_id, "symbol": entry["symbol"]})


@app.post("/api/pending/approve")
def pending_approve():
    """Approve a queued setup and send the order.

    RE-SCREENS against live market data rather than replaying the queued
    plan: an entry can sit for minutes while price moves, so the stored
    prices are a record of what was seen, never an instruction to trade. If
    the setup no longer clears the gate, this refuses and drops it instead
    of firing a stale idea. Every gate /api/fire enforces (approved, sizable,
    armed, capital guard) is enforced here too — approving must not be a way
    to bypass them.

    Hyperliquid only: MT5 orders go through a separate broker path
    (bot/mt5/broker.py) that this control panel has no client for, so an MT5
    entry can be Cancelled here but must be actioned from the MT5 terminal.
    """
    entry_id = (request.get_json(silent=True) or {}).get("id")
    if not entry_id:
        return jsonify({"error": "id required"}), 400

    entry = pending_trades.get(entry_id)
    if entry is None:
        return jsonify({"error": "already resolved or expired"}), 404
    if entry["venue"] != "hl":
        return jsonify({
            "error": f"{entry['symbol']} is an MT5 setup — approve it from the MT5 "
                     f"terminal; this panel can only fire Hyperliquid orders"
        }), 400
    if not live_state.is_armed():
        return jsonify({"error": "disarmed — flip Activate on first"}), 403

    coin = entry["symbol"]
    client = _client()
    trader = _trader(client)
    acct = client.account()
    sm_direction, sm_bullish, sm_bearish = _get_smart_money()
    try:
        signal, result, plan, unified = trader.evaluate(
            coin, CFG.get("timeframe", "15m"), CFG.get("higher_timeframe", "1h"),
            acct.account_value, acct.withdrawable, sm_direction, sm_bullish, sm_bearish,
        )
    except Exception as e:
        # Re-screening itself failed — a delisted/renamed coin, or a venue
        # hiccup. Either way we have no fresh read, so we must NOT fall back
        # to the queued prices and fire blind. Drop the entry and say why.
        pending_trades.resolve(entry_id, "stale")
        return jsonify({
            "error": f"{coin} could not be re-screened ({type(e).__name__}) — dropped, no order sent"
        }), 409
    if not unified.approved:
        pending_trades.resolve(entry_id, "stale")
        return jsonify({"error": f"{coin} no longer clears the gate ({unified.reason}) — dropped"}), 409
    if plan is None:
        return jsonify({"error": f"{coin} approved but not sizable (free margin or $10 min)"}), 400
    if plan.side != entry["side"]:
        pending_trades.resolve(entry_id, "stale")
        return jsonify({
            "error": f"{coin} has flipped to {plan.side.upper()} since it was queued "
                     f"({entry['side'].upper()}) — dropped rather than fired"
        }), 409
    allowed, reason = trader.guard_check(acct.account_value, confidence=signal.confidence)
    if not allowed:
        return jsonify({"error": f"blocked: {reason}"}), 403

    # Resolve BEFORE executing: if the order succeeds but resolve() somehow
    # didn't run, the entry would stay live and could be approved twice.
    if pending_trades.resolve(entry_id, "approved") is None:
        return jsonify({"error": "already resolved or expired"}), 404
    resp = trader.execute(plan)
    return jsonify({
        "result": resp,
        "plan": {"coin": coin, "side": plan.side, "usd": plan.usd,
                 "final_pct": unified.final_pct},
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8420, debug=False)
