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


def detect_liquidity_pools(
    df: pd.DataFrame, tolerance_pct: float = 0.0005, lookback: int = 50
) -> list[LiquidityPool]:
    """
    Detect equal highs (buy-side liquidity) and equal lows (sell-side liquidity).
    Institutions often sweep these levels before reversing.
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

    return _mark_swept(df, pools)


def _mark_swept(df: pd.DataFrame, pools: list[LiquidityPool]) -> list[LiquidityPool]:
    active: list[LiquidityPool] = []
    for pool in pools:
        future = df.iloc[pool.index + 1 :]
        if pool.kind == "buy_side":
            pool.swept = (future["high"] > pool.level).any()
        else:
            pool.swept = (future["low"] < pool.level).any()

        if pool.swept:
            active.append(pool)

    return active


def recent_sweep(pools: list[LiquidityPool], df: pd.DataFrame, bars: int = 5) -> LiquidityPool | None:
    """Return the most recent liquidity sweep within the last N bars."""
    cutoff = len(df) - bars
    recent_sweeps = [p for p in pools if p.swept and p.index >= cutoff]
    return recent_sweeps[-1] if recent_sweeps else None
