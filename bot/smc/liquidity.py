from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class LiquidityPool:
    index: int
    kind: str  # "buy_side" | "sell_side"
    level: float
    swept: bool = False
    # True when price wicked through the level and CLOSED back on the
    # original side -- a stop-hunt that was rejected. False when price closed
    # through and stayed there, which is a break, not a sweep. The default
    # `swept` flag cannot tell these apart; see mark_sweeps().
    reclaimed: bool = False
    # How many equal-level touches formed this pool. 2 is the minimum; more
    # means a level price has repeatedly respected, where more stops rest.
    touches: int = 2


def detect_liquidity_pools(
    df: pd.DataFrame,
    tolerance_pct: float = 0.0005,
    lookback: int = 50,
    merge: bool = False,
    require_reclaim: bool = False,
    include_unswept: bool = False,
) -> list[LiquidityPool]:
    """
    Detect equal highs (buy-side liquidity) and equal lows (sell-side liquidity).
    Institutions often sweep these levels before reversing.

    All three keyword flags default to the original behaviour, so existing
    callers -- including bot/screening.py's live gate -- are unaffected.

    merge
        Collapse pools whose levels are within tolerance into one, carrying a
        `touches` count. Without it every matching PAIR of bars emits its own
        pool, so a level touched five times produces ten near-identical
        entries; that is why a 200-bar gold frame reports 343 "pools" for a
        handful of actual levels.

    require_reclaim
        Keep only sweeps price CLOSED back from (LiquidityPool.reclaimed).
        Without it, "swept" means merely traded through -- which is what
        price does when it breaks a level and keeps going, the opposite of a
        stop-hunt that reverses.

    include_unswept
        Also return levels that have NOT been taken. The original returns only
        swept pools, so resting liquidity -- the levels price may still run
        to -- is invisible to every caller.
    """
    pools: list[LiquidityPool] = []
    recent = df.tail(lookback)
    highs = recent["high"].values
    lows = recent["low"].values
    offset = len(df) - len(recent)  # 0 when df is shorter than lookback

    # Equal highs — buy-side liquidity resting above
    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            if abs(highs[i] - highs[j]) / highs[i] <= tolerance_pct:
                level = (highs[i] + highs[j]) / 2
                pools.append(
                    LiquidityPool(
                        index=offset + j,
                        kind="buy_side",
                        level=float(level),
                    )
                )

    # Equal lows — sell-side liquidity resting below
    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            if abs(lows[i] - lows[j]) / lows[i] <= tolerance_pct:
                level = (lows[i] + lows[j]) / 2
                pools.append(
                    LiquidityPool(
                        index=offset + j,
                        kind="sell_side",
                        level=float(level),
                    )
                )

    if merge:
        pools = _merge_pools(pools, tolerance_pct)
    return _mark_swept(df, pools, require_reclaim=require_reclaim,
                       include_unswept=include_unswept)


def _merge_pools(pools: list[LiquidityPool], tolerance_pct: float) -> list[LiquidityPool]:
    """Collapse pools of the same kind sitting within tolerance of each other,
    keeping the LATEST index (the most recent touch, which is what recency
    checks key on) and counting how many touches formed the level."""
    merged: list[LiquidityPool] = []
    for pool in sorted(pools, key=lambda p: (p.kind, p.level)):
        hit = None
        for m in merged:
            if m.kind == pool.kind and m.level and \
                    abs(m.level - pool.level) / m.level <= tolerance_pct:
                hit = m
                break
        if hit is None:
            merged.append(pool)
        else:
            hit.touches += 1
            hit.index = max(hit.index, pool.index)
            hit.level = (hit.level + pool.level) / 2
    return sorted(merged, key=lambda p: p.index)


def _mark_swept(
    df: pd.DataFrame,
    pools: list[LiquidityPool],
    require_reclaim: bool = False,
    include_unswept: bool = False,
) -> list[LiquidityPool]:
    active: list[LiquidityPool] = []
    for pool in pools:
        future = df.iloc[pool.index + 1 :]
        if pool.kind == "buy_side":
            pierced = future["high"] > pool.level
            # Reclaimed: some bar wicked ABOVE the level but CLOSED back below
            # it -- the level was probed and refused. A bar closing above and
            # staying there is a break.
            pool.reclaimed = bool((pierced & (future["close"] < pool.level)).any())
        else:
            pierced = future["low"] < pool.level
            pool.reclaimed = bool((pierced & (future["close"] > pool.level)).any())
        pool.swept = bool(pierced.any())

        if pool.swept and (pool.reclaimed or not require_reclaim):
            active.append(pool)
        elif include_unswept and not pool.swept:
            active.append(pool)

    return active


def recent_sweep(pools: list[LiquidityPool], df: pd.DataFrame, bars: int = 5) -> LiquidityPool | None:
    """Return the most recent liquidity sweep within the last N bars."""
    cutoff = len(df) - bars
    recent_sweeps = [p for p in pools if p.swept and p.index >= cutoff]
    return recent_sweeps[-1] if recent_sweeps else None
