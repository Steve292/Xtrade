from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class Trend(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # "high" | "low"


@dataclass
class StructureEvent:
    index: int
    kind: str  # "bos" | "choch"
    direction: str  # "bullish" | "bearish"
    level: float


def find_swing_points(df: pd.DataFrame, lookback: int = 5) -> list[SwingPoint]:
    """Identify swing highs and lows using local extrema."""
    swings: list[SwingPoint] = []
    highs = df["high"].values
    lows = df["low"].values

    for i in range(lookback, len(df) - lookback):
        window_high = highs[i - lookback : i + lookback + 1]
        window_low = lows[i - lookback : i + lookback + 1]

        if highs[i] == np.max(window_high):
            swings.append(SwingPoint(index=i, price=float(highs[i]), kind="high"))
        if lows[i] == np.min(window_low):
            swings.append(SwingPoint(index=i, price=float(lows[i]), kind="low"))

    return swings


def detect_trend(swings: list[SwingPoint]) -> Trend:
    """Determine trend from the last two swing highs and lows."""
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    if len(highs) < 2 or len(lows) < 2:
        return Trend.NEUTRAL

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        return Trend.BULLISH
    if lh and ll:
        return Trend.BEARISH
    return Trend.NEUTRAL


def detect_structure_breaks(
    df: pd.DataFrame, swings: list[SwingPoint]
) -> list[StructureEvent]:
    """Detect BOS (continuation) and CHoCH (reversal) events."""
    events: list[StructureEvent] = []
    trend = Trend.NEUTRAL
    last_high: SwingPoint | None = None
    last_low: SwingPoint | None = None

    for swing in swings:
        close_at_swing = float(df.iloc[swing.index]["close"])

        if swing.kind == "high":
            if last_high and close_at_swing > last_high.price:
                kind = "choch" if trend == Trend.BEARISH else "bos"
                events.append(
                    StructureEvent(
                        index=swing.index,
                        kind=kind,
                        direction="bullish",
                        level=last_high.price,
                    )
                )
                trend = Trend.BULLISH
            last_high = swing

        if swing.kind == "low":
            if last_low and close_at_swing < last_low.price:
                kind = "choch" if trend == Trend.BULLISH else "bos"
                events.append(
                    StructureEvent(
                        index=swing.index,
                        kind=kind,
                        direction="bearish",
                        level=last_low.price,
                    )
                )
                trend = Trend.BEARISH
            last_low = swing

    return events


def premium_discount_zone(
    df: pd.DataFrame, swings: list[SwingPoint]
) -> tuple[float, float, float]:
    """Return (range_low, equilibrium, range_high) from recent swings."""
    if not swings:
        recent = df.tail(50)
        low = float(recent["low"].min())
        high = float(recent["high"].max())
    else:
        recent_swings = swings[-6:]
        low = min(s.price for s in recent_swings if s.kind == "low") if any(
            s.kind == "low" for s in recent_swings
        ) else float(df["low"].tail(50).min())
        high = max(s.price for s in recent_swings if s.kind == "high") if any(
            s.kind == "high" for s in recent_swings
        ) else float(df["high"].tail(50).max())

    equilibrium = (low + high) / 2
    return low, equilibrium, high


def is_in_discount(price: float, zone: tuple[float, float, float]) -> bool:
    low, eq, _ = zone
    return low <= price <= eq


def is_in_premium(price: float, zone: tuple[float, float, float]) -> bool:
    _, eq, high = zone
    return eq <= price <= high
