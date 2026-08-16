#!/usr/bin/env python3
"""
Read-only MCP server exposing TraderX's regime/hotness/smart-money signals —
the informational half of the "self-evolving bot" blueprint's Section 9 tool
spec. Deliberately does NOT include place_order, get_entry_signal, or
get_position_size: this server has no trade-execution capability at all, on
purpose. Every tool below queries a venue or a free data provider and
returns numbers; none of them place, close, size, or modify an order.
Real-money execution in this project only ever happens through the existing
manual arm + Fire flow in webapp/dashboard.html, unchanged by this file.

Runs on its own isolated Python 3.13 venv (mcp_server/venv) — the main
project venv is pinned to Python 3.9 for the live trading bot's own
dependencies, and the official `mcp` SDK needs >=3.10, so this is a fully
separate environment that never touches the live bot's runtime.

Point an MCP client at this with:
    command: <repo>/mcp_server/venv/bin/python
    args:    ["<repo>/mcp_server/server.py"]

    python mcp_server/server.py   # (stdio transport — a client launches this, not a human)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import time

from bot.capital_guard import CapitalGuard
from bot.market_snapshot import load_config, compute_snapshot

# Explicit path: an MCP client launches this from an arbitrary cwd, so the
# plain no-arg load_dotenv() (which searches upward from cwd) can't be
# relied on to find the repo's .env.
load_dotenv(dotenv_path=REPO_ROOT / ".env")
mcp = FastMCP("traderx-signals")

_cfg = load_config()
_guard_cfg = _cfg.get("capital_guard", {})

# Same 15-minute cache webapp/server.py's /api/regime uses, for the same
# reason: a single question can easily lead to several of these tools being
# called back to back (e.g. get_regime + get_hotness_signal +
# get_smart_money_aggregate), and without a cache each call independently
# re-fetches Yahoo/CoinGecko/Deribit/Hyperliquid from scratch.
_SNAPSHOT_CACHE_SECONDS = 900
_snapshot_cache: dict = {"data": None, "fetched_at": 0.0}


def _snapshot() -> dict:
    now = time.time()
    if _snapshot_cache["data"] is None or now - _snapshot_cache["fetched_at"] > _SNAPSHOT_CACHE_SECONDS:
        try:
            _snapshot_cache["data"] = compute_snapshot(_cfg)
            _snapshot_cache["fetched_at"] = now
        except Exception:
            if _snapshot_cache["data"] is None:
                raise
    return _snapshot_cache["data"]


@mcp.tool()
def get_regime() -> dict:
    """Macro/crypto regime score (0-100) and label — blueprint Section 1.
    RISK_ON_GROWTH / RISK_ON_INFLATION / NEUTRAL / STAGFLATION / RISK_OFF."""
    r = _snapshot()["regime"]
    return {"regime": r["label"], "score": r["score"], "missing_inputs": r["missing"]}


@mcp.tool()
def get_meme_score() -> dict:
    """Meme season composite oscillator (0-100), its zone, and the
    recommended action — blueprint Section 3. The action is a
    recommendation only (e.g. "take profits") — nothing executes it."""
    m = _snapshot()["meme_score"]
    return {"score": m["score"], "zone": m["zone"], "action": m["action"]}


@mcp.tool()
def get_hotness_signal() -> dict:
    """4-factor BTC.D/MEME/OTHERS.D/STABLE.C hotness signal — blueprint
    Section 2. MEME_ROTATION_ACTIVE / ALT_SEASON_STARTING / RISK_OFF_WARNING
    / NEUTRAL, with `confidence` reflecting how much trend history has
    accumulated so far (this bootstraps from real snapshots over time, see
    bot/timeseries.py — it isn't fully warmed up on a fresh install)."""
    h = _snapshot()["hotness"]
    return {"signal": h["signal"], "multiplier": h["multiplier"], "confidence": h["confidence"]}


@mcp.tool()
def get_cvd_signal() -> dict:
    """Volume-delta signal for BTC — an approximation of Section 4's CVD
    module, not real tick-level cumulative volume delta: Hyperliquid's REST
    API has no market-wide trade tape, only account-specific fills, so this
    derives BUY/SELL/NEUTRAL + strength from green-vs-red candle volume."""
    return _snapshot()["smart_money"]["modules"]["cvd"]


@mcp.tool()
def get_gex_signal() -> dict:
    """Open-interest concentration proxy for BTC options (Deribit, free/
    public) — NOT real gamma exposure (that needs dealers' actual signed
    positioning, which nobody publishes). Only ever CAUTION (price near the
    heaviest-OI strike) or NEUTRAL — never a directional call, because OI
    concentration alone can't honestly support one."""
    return _snapshot()["smart_money"]["modules"]["gex"]


@mcp.tool()
def get_flow_signal() -> dict:
    """Stablecoin flow / SSR signal — real, computed from CoinGecko's free
    global + category data (BTC market cap / stablecoin market cap)."""
    return _snapshot()["smart_money"]["modules"]["stablecoin_flow"]


@mcp.tool()
def get_heatmap_signal() -> dict:
    """Liquidation-heatmap proxy for BTC: reuses this project's existing,
    already-live SMC liquidity-pool sweep detector (equal highs/lows are
    genuinely where stops/liquidations tend to cluster) rather than a
    fabricated heatmap. Only reports "BUY" (a sweep was just confirmed) or
    NEUTRAL — not the blueprint's CAUTION half, which would need exposing
    not-yet-swept pools from code the live strategy depends on."""
    return _snapshot()["smart_money"]["modules"]["liquidation_heatmap"]


@mcp.tool()
def get_narrative_decay() -> dict:
    """Social/narrative-decay signal — always unavailable. No free data
    source exists for this (would need LunarCrush or similar); never faked."""
    return _snapshot()["smart_money"]["modules"]["narrative_decay"]


@mcp.tool()
def get_divergence_signal() -> dict:
    """BTC vs. S&P 500 (Yahoo Finance, free) 24h divergence — BUY when they
    decouple (genuine safe-haven behavior), HEDGE when BTC is just tracking
    equity risk sentiment with no independent thesis."""
    return _snapshot()["smart_money"]["modules"]["divergence"]


@mcp.tool()
def get_session_signal() -> dict:
    """Trading-session liquidity window (UTC, pure clock logic, no data
    source needed) — EXECUTE during Asia/London/New York, HOLD during the
    21:00-24:00 UTC dead zone between NY close and Asia open."""
    return _snapshot()["smart_money"]["modules"]["session"]


@mcp.tool()
def get_regulatory_news_signal() -> dict:
    """Real, current crypto regulatory/legislative headlines (CoinTelegraph,
    free, no key), scanned for a curated topic list (CLARITY Act, GENIUS
    Act, SEC, CFTC, ETF, Congress, ...) and classified only when a headline
    also contains an explicit directional verb (approved/passed/cleared =
    bullish-tilted; sued/warned/blocked/banned = bearish-tilted). A topic
    mention with no directional verb is NEUTRAL, never guessed. This is a
    crude keyword heuristic, not NLP sentiment analysis — `relevant_headlines`
    lists exactly which real headlines drove the signal, so it's auditable
    rather than a black box. Counts as a 9th input into
    get_smart_money_aggregate() below, added at explicit user request so
    real regulatory news can push the vote toward a buy or sell."""
    n = _snapshot()["regulatory_news"]
    return {
        "signal": n["signal"],
        "bullish_count": n["bullish_count"],
        "bearish_count": n["bearish_count"],
        "relevant_headlines": n["relevant_headlines"],
    }


@mcp.tool()
def get_smart_money_aggregate() -> dict:
    """The blueprint's Section 4 aggregation rule (4+ modules agree -> 2.0x,
    3 -> 1.0x, 2 -> 0.5x, <2 -> hold cash) applied across the original 8
    modules PLUS a 9th, get_regulatory_news_signal() above (added at
    explicit user request). Note the built-in asymmetry: the heatmap and
    divergence modules only ever emit BUY-or-NEUTRAL by design (matching the
    blueprint's own BUY/CAUTION and BUY/HEDGE pairs, which have no SELL
    option) — so a BEARISH read draws on at most 4 of the 9 modules."""
    sm = _snapshot()["smart_money"]
    return {
        "direction": sm["direction"],
        "bullish_count": sm["bullish_count"],
        "bearish_count": sm["bearish_count"],
        "multiplier": sm["multiplier"],
    }


@mcp.tool()
def get_major_entry_signal() -> dict:
    """Section 5's BTC/ETH "majors" checklist, evaluated for BTC against
    real daily+4h candles. Each check is True / False / None — None means no
    data was available to evaluate it (this project has no free source for
    ETF 7-day flow, so that check is always None here), never silently
    treated as pass or fail. verdict is BUY only if every check is
    explicitly True, NO_SETUP if any is explicitly False, INCOMPLETE
    otherwise — given the missing ETF-flow input, this realistically caps at
    INCOMPLETE rather than ever reaching BUY, which is honest, not a bug.
    Report-only: this is a second, independent classic-TA opinion, not a
    replacement for the live seven-gate SMC screener."""
    return _snapshot()["entry_rules_major"]


@mcp.tool()
def get_suggested_position_size() -> dict:
    """Section 6's sizing formula (base risk x regime alloc weight x
    hotness multiplier x volatility adjust), computed for BTC from the
    current regime/hotness snapshot and real ATR. Returns a suggested risk
    PERCENTAGE only — not a dollar amount or coin quantity, and not
    consumed anywhere in the live trading loop (see bot/position_sizing.py's
    docstring)."""
    return _snapshot()["suggested_sizing"]


@mcp.tool()
def get_mt5_watchlist_signals() -> dict:
    """Per-symbol smart-money signals for every symbol in config.yaml's
    mt5_watchlist (forex pairs + gold). Only runs the modules that
    genuinely generalize to any instrument's own candles (CVD proxy,
    liquidation-heatmap proxy, SMC+Fib, session timing) — GEX, stablecoin
    flow/SSR, narrative decay, divergence, and the Section 5 majors/meme/alt
    entry rules are crypto-dominance/perp-specific concepts with no forex or
    commodity equivalent (no funding rate, no SSR, no BTC.D for EURUSD), so
    they're skipped here rather than faked. Gold (XAU) gets a suggested
    sizing (blueprint's "commodity" bucket); every FX pair's
    suggested_sizing is null — the blueprint never defines a forex base-risk
    bucket, and this doesn't invent one."""
    return {"symbols": _snapshot()["mt5_watchlist_analysis"]}


@mcp.tool()
def get_risk_status() -> dict:
    """Capital guard state (bot/capital_guard.py), read from its persisted
    file — the SAME guard the live trading loop checks before every entry.
    This is a read of existing state, not a new check: it does not open,
    close, or modify anything. `can_trade` reflects only the guard's own
    halted/not-halted state (checked with zero assumed open risk) — a real
    trade could still separately hit the concurrent-trades or open-risk caps,
    which need an actual position list this tool doesn't have."""
    guard = CapitalGuard.load(
        **{k: _guard_cfg[k] for k in CapitalGuard.__dataclass_fields__ if k in _guard_cfg}
    )
    can_trade, reason = guard.can_open_new_trade([], 0.0)
    return {
        "can_trade": can_trade,
        "reason": reason,
        "size_multiplier": guard.size_multiplier,
        "halted": guard.halted,
    }


if __name__ == "__main__":
    mcp.run()
