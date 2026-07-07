from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class FairValueGap:
    index: int
    direction: str  # "bullish" | "bearish"
    top: float
    bottom: float
    filled: bool = False


def detect_fvg(df: pd.DataFrame, min_size_pct: float = 0.001) -> list[FairValueGap]:
    """
    Detect Fair Value Gaps (imbalances):
    Bullish FVG: candle[i-1].high < candle[i+1].low
    Bearish FVG: candle[i-1].low > candle[i+1].high
    """
    gaps: list[FairValueGap] = []
    highs = df["high"].values
    lows = df["low"].values

    for i in range(1, len(df) - 1):
        # Bullish FVG
        if lows[i + 1] > highs[i - 1]:
            size = (lows[i + 1] - highs[i - 1]) / highs[i - 1]
            if size >= min_size_pct:
                gaps.append(
                    FairValueGap(
                        index=i,
                        direction="bullish",
                        top=float(lows[i + 1]),
                        bottom=float(highs[i - 1]),
                    )
                )

        # Bearish FVG
        if highs[i + 1] < lows[i - 1]:
            size = (lows[i - 1] - highs[i + 1]) / lows[i - 1]
            if size >= min_size_pct:
                gaps.append(
                    FairValueGap(
                        index=i,
                        direction="bearish",
                        top=float(lows[i - 1]),
                        bottom=float(highs[i + 1]),
                    )
                )

    return _mark_filled(df, gaps)


def _mark_filled(df: pd.DataFrame, gaps: list[FairValueGap]) -> list[FairValueGap]:
    active: list[FairValueGap] = []
    for gap in gaps:
        future = df.iloc[gap.index + 1 :]
        if gap.direction == "bullish":
            gap.filled = (future["low"] <= gap.bottom).any()
        else:
            gap.filled = (future["high"] >= gap.top).any()

        if not gap.filled:
            active.append(gap)

    return active


def price_in_fvg(price: float, gap: FairValueGap) -> bool:
    return gap.bottom <= price <= gap.top
