"""
Breaker blocks — order blocks that failed, and flipped polarity.

An order block marks where institutions entered. When price closes clean
through it, those positions are trapped: the level did not hold. What the
corpus calls a "breaker" is that broken block on its RETEST — the trapped
side defends the level from the other direction, so a broken bullish block
(former support) becomes resistance, and a broken bearish block becomes
support.

The polarity flip is the whole point, and it is why this can't be folded into
bot/smc/order_blocks.py: detect_order_blocks() returns blocks that are still
UNMITIGATED and still act in their original direction. It discards broken
ones. A breaker is made of exactly the blocks it throws away, which is what
detect_raw_order_blocks() was split out to expose.

Sequence required, in order:
  1. an order block forms
  2. price CLOSES through its far side (the break — a wick through is not
     enough; wicks through levels are noise, closes are commitment)
  3. price RETURNS to the zone afterwards (the retest)

Without step 3 there is nothing to trade: a level price has left behind and
never revisited is history, not a setup.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .order_blocks import OrderBlock, detect_raw_order_blocks


@dataclass
class BreakerBlock:
    index: int  # index of the original order block candle
    direction: str  # "bullish" | "bearish" — the FLIPPED direction it now acts in
    top: float
    bottom: float
    break_index: int  # bar whose close broke the original block
    retested: bool  # price has returned to the zone since the break


def detect_breakers(
    df: pd.DataFrame, lookback: int = 20, impulse_threshold: float = 0.005
) -> list[BreakerBlock]:
    """Breaker blocks from the last `lookback` candidate order blocks.

    Only blocks that were broken by a CLOSE are considered, and the returned
    direction is already flipped relative to the original block.
    """
    if len(df) == 0:
        return []

    raw: list[OrderBlock] = detect_raw_order_blocks(df, impulse_threshold)
    if not raw:
        return []

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    breakers: list[BreakerBlock] = []
    for block in raw[-lookback:]:
        start = block.index + 1
        if start >= n:
            continue

        break_idx = None
        for j in range(start, n):
            if block.direction == "bullish" and closes[j] < block.bottom:
                break_idx = j
                break
            if block.direction == "bearish" and closes[j] > block.top:
                break_idx = j
                break

        if break_idx is None:
            continue

        # Retest: did price come back into the zone after the break?
        retested = False
        for j in range(break_idx + 1, n):
            if lows[j] <= block.top and highs[j] >= block.bottom:
                retested = True
                break

        breakers.append(
            BreakerBlock(
                index=block.index,
                # The flip: a broken bullish block now acts bearish.
                direction="bearish" if block.direction == "bullish" else "bullish",
                top=float(block.top),
                bottom=float(block.bottom),
                break_index=int(break_idx),
                retested=retested,
            )
        )

    return breakers


def price_in_breaker(price: float, breaker: BreakerBlock) -> bool:
    return breaker.bottom <= price <= breaker.top


def active_breaker(
    price: float, breakers: list[BreakerBlock], direction: str
) -> BreakerBlock | None:
    """The most recent retested breaker of `direction` that price sits inside,
    or None. Un-retested breakers are skipped — see the module docstring."""
    for b in reversed(breakers):
        if b.direction == direction and b.retested and price_in_breaker(price, b):
            return b
    return None
