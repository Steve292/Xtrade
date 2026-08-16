"""
Smart Money Signal Modules — blueprint Section 4.

Every function here is read-only and returns a plain dict — none of them
place, close, or modify an order; "signal" fields are informational output
for a human or the dashboard to read, same boundary as bot/regime.py,
bot/hotness.py, and bot/position_sizing.py.

Two of the blueprint's eight modules genuinely have no free data source and
are NOT faked:
  - narrative_decay_signal(): needs LunarCrush-style social/mention data.
    Always returns unavailable.
  - gex_signal(): Deribit's public options book gives real open-interest-by-
    strike data, but a genuine GEX figure needs dealers' actual signed
    positioning, which nobody publishes — paid GEX products infer/model it,
    they don't observe it either. This only claims what OI concentration
    alone can honestly support (a "pin"/magnet heuristic), and deliberately
    never emits BUY/SELL — just CAUTION near the heaviest strike, or NEUTRAL.

cvd_signal() is also an approximation, not real: Hyperliquid's REST API
(checked against the installed SDK's Info class) has no market-wide trade
tape endpoint, only account-specific user_fills — so this derives a
volume-delta-style proxy from OHLCV candles (green-candle volume minus
red-candle volume) instead of tick-level aggressor data.

liquidation_heatmap_signal() reuses this project's EXISTING, already-tested
bot/smc/liquidity.py sweep detector rather than building a new "heatmap" from
scratch — equal-high/low liquidity pools are genuinely where stops and
liquidations tend to cluster, which is the same idea CoinGlass's heatmap is
getting at, just without their proprietary aggregation. It only implements
the "sweep confirmed" (BUY) half of the blueprint's BUY/CAUTION pair,
deliberately not the "cluster near" half — that would need exposing
not-yet-swept pools from bot/smc/liquidity.py, and that module is live-relied
-upon by the actual trading strategy, so it's left untouched here.

aggregate_smart_money()'s "N modules agree" rule needs every module folded
into one BULLISH/BEARISH/NEUTRAL vocabulary first, but the blueprint's own
per-module outputs aren't uniform (some are inherently directional, some are
timing/caution gates that were never making a directional claim). BUY/SELL
map to BULLISH/BEARISH; everything else (HEDGE/EXECUTE/HOLD/CAUTION/NEUTRAL)
abstains as NEUTRAL. A consequence worth knowing: liquidation_heatmap_signal
and divergence_signal only ever emit BUY or NEUTRAL in this implementation
(matching the blueprint's own asymmetric BUY/CAUTION and BUY/HEDGE pairs, which
have no SELL option) — so a BEARISH read can only ever draw on 3 of the 8
modules (CVD, stablecoin flow, SMC+Fib), capping it at the "3 agree" tier,
never the top one. That asymmetry comes from the blueprint's own module
definitions, not a bug introduced here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd


def cvd_signal(candles: pd.DataFrame, lookback: int = 20, deadzone: float = 0.1) -> dict:
    """Volume-delta proxy from OHLCV (see module docstring for why this
    isn't tick-level CVD): green-candle volume minus red-candle volume over
    the last `lookback` bars, normalized to [-1, 1]. `deadzone` is how far
    from zero the normalized delta must sit before this calls a direction
    instead of NEUTRAL."""
    recent = candles.tail(lookback)
    up_vol = float(recent.loc[recent["close"] >= recent["open"], "volume"].sum())
    down_vol = float(recent.loc[recent["close"] < recent["open"], "volume"].sum())
    total = up_vol + down_vol
    if total <= 0:
        return {"signal": "NEUTRAL", "strength": 0.0}
    delta = (up_vol - down_vol) / total
    if delta > deadzone:
        signal = "BUY"
    elif delta < -deadzone:
        signal = "SELL"
    else:
        signal = "NEUTRAL"
    return {"signal": signal, "strength": abs(delta)}


def gex_signal(spot_price: float, oi_by_strike: dict[float, float] | None, caution_pct: float = 3.0) -> dict:
    """See module docstring — an open-interest "pin" proxy, not real gamma
    exposure. Only ever CAUTION (price is within `caution_pct`% of the
    heaviest-OI strike) or NEUTRAL; never a directional call."""
    if not oi_by_strike or spot_price <= 0:
        return {"signal": "NEUTRAL", "flip_zone": None, "distance_pct": None}
    flip_zone = max(oi_by_strike, key=oi_by_strike.get)
    distance_pct = (flip_zone - spot_price) / spot_price * 100
    signal = "CAUTION" if abs(distance_pct) <= caution_pct else "NEUTRAL"
    return {"signal": signal, "flip_zone": flip_zone, "distance_pct": distance_pct}


def stablecoin_flow_signal(ssr: float | None, ssr_threshold: float = 3.0) -> dict:
    """Reuses the same SSR bot/regime.py already computes for real (BTC
    market cap / stablecoin market cap via CoinGecko) rather than a second
    calculation — low SSR means more stablecoin firepower relative to BTC,
    i.e. more dry powder available to buy risk."""
    if ssr is None:
        return {"signal": "NEUTRAL", "ssr": None}
    return {"signal": "BUY" if ssr < ssr_threshold else "SELL", "ssr": ssr}


def liquidation_heatmap_signal(candles: pd.DataFrame, bars: int = 5) -> dict:
    """See module docstring: wraps the existing, already-tested SMC
    liquidity-pool sweep detector rather than a new heatmap. Only the
    "sweep confirmed" (BUY) half is implemented."""
    from bot.smc.liquidity import detect_liquidity_pools, recent_sweep

    pools = detect_liquidity_pools(candles)
    sweep = recent_sweep(pools, candles, bars=bars)
    if sweep is not None:
        return {"signal": "BUY", "level": sweep.level, "kind": sweep.kind}
    return {"signal": "NEUTRAL", "level": None, "kind": None}


def narrative_decay_signal() -> dict:
    """No free data source (needs LunarCrush-style social/mention data) —
    always unavailable, never faked."""
    return {"signal": "NEUTRAL", "available": False}


def divergence_signal(btc_24h_change_pct: float | None, spx_24h_change_pct: float | None, threshold_pct: float = 1.0) -> dict:
    """BUY when BTC and SPX are moving in opposite directions by more than
    `threshold_pct` points (genuine decoupling — the "safe haven" case the
    blueprint describes); HEDGE when they're moving together (BTC is just
    tracking risk sentiment, no independent thesis)."""
    if btc_24h_change_pct is None or spx_24h_change_pct is None:
        return {"signal": "NEUTRAL", "correlation": None}
    same_direction = (btc_24h_change_pct >= 0) == (spx_24h_change_pct >= 0)
    diverging = not same_direction and abs(btc_24h_change_pct - spx_24h_change_pct) > threshold_pct
    return {"signal": "BUY" if diverging else "HEDGE", "correlation": "diverging" if diverging else "correlated"}


def smc_fib_signal(signal_type: str) -> dict:
    """Thin wrapper — this project's own SMC/Fibonacci strategy (bot/smc/
    strategy.py) already IS the blueprint's "SMC + Fibonacci" module, so this
    just relabels its existing signal.type for aggregation rather than
    computing a second, separate one. Accepts SignalType or a plain
    "long"/"short"/"none" string."""
    if signal_type == "long":
        return {"signal": "BUY"}
    if signal_type == "short":
        return {"signal": "SELL"}
    return {"signal": "NEUTRAL"}


_ASIA = (0, 8)
_LONDON = (8, 16)
_NEW_YORK = (13, 21)


def session_signal(now: datetime | None = None) -> dict:
    """Pure time-of-day logic (UTC), no data source needed. London/NY
    overlap (13:00-16:00 UTC) is peak liquidity; the 21:00-24:00 UTC dead
    zone (after NY close, before Asia open) is thin and prone to erratic
    moves on light volume."""
    now = now or datetime.now(timezone.utc)
    hour = now.hour
    in_asia = _ASIA[0] <= hour < _ASIA[1]
    in_london = _LONDON[0] <= hour < _LONDON[1]
    in_ny = _NEW_YORK[0] <= hour < _NEW_YORK[1]

    if in_london and in_ny:
        return {"signal": "EXECUTE", "session": "LONDON_NY_OVERLAP"}
    if in_london:
        return {"signal": "EXECUTE", "session": "LONDON"}
    if in_ny:
        return {"signal": "EXECUTE", "session": "NEW_YORK"}
    if in_asia:
        return {"signal": "EXECUTE", "session": "ASIA"}
    return {"signal": "HOLD", "session": "DEAD_ZONE"}


@dataclass
class SmartMoneyResult:
    bullish_count: int
    bearish_count: int
    direction: str  # "BULLISH" | "BEARISH" | "NEUTRAL"
    multiplier: float
    modules: dict[str, dict] = field(default_factory=dict)


def _size_multiplier(agree_count: int) -> float:
    if agree_count >= 4:
        return 2.0
    if agree_count == 3:
        return 1.0
    if agree_count == 2:
        return 0.5
    return 0.0  # < 2 agree -> hold cash


def aggregate_smart_money(modules: dict[str, dict]) -> SmartMoneyResult:
    """`modules`: {name: {"signal": ..., ...}} as returned by the functions
    above. See module docstring for the BULLISH/BEARISH/NEUTRAL normalization
    and its consequences."""
    bullish = sum(1 for m in modules.values() if m.get("signal") == "BUY")
    bearish = sum(1 for m in modules.values() if m.get("signal") == "SELL")

    if bullish > bearish:
        return SmartMoneyResult(bullish, bearish, "BULLISH", _size_multiplier(bullish), dict(modules))
    if bearish > bullish:
        return SmartMoneyResult(bullish, bearish, "BEARISH", _size_multiplier(bearish), dict(modules))
    return SmartMoneyResult(bullish, bearish, "NEUTRAL", 0.0, dict(modules))
