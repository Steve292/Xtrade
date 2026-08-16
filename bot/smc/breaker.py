"""Breaker blocks — the zone that failed, flipped, and now works the other way.

Second-largest gap found by the corpus: "breaker" appears in 78 of 157 videos
(50%) from the confirmed educator channel, with nothing here modelling it.

A breaker is an order block that DIDN'T hold. Price closed straight through it,
and the zone then flips polarity: former resistance becomes support, former
support becomes resistance. The trade is the retest from the new side.

The relationship to the neighbouring modules is worth stating precisely,
because all three describe the same zone at different points in its life:

    order_blocks.py  zone forms
    mitigation.py    price returns, zone HOLDS      -> trade with the zone
    breaker.py       price returns, zone BREAKS     -> trade against the old
                                                       zone, from the far side

Direction inverts, and that is the whole point. A BEARISH order block that
price closes above becomes a BULLISH breaker: the sellers who defended it are
now trapped, and their stops sit above. Getting this backwards would produce a
detector that reliably enters on the wrong side, so the inversion is asserted
in the tests rather than left implicit.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .order_blocks import OrderBlock, raw_order_blocks


@dataclass
class BreakerBlock:
    index: int              # where the original (failed) zone formed
    direction: str          # direction of the BREAKER, already inverted
    origin_direction: str   # direction of the order block it came from
    top: float
    bottom: float
    broken_at: int          # bar whose close broke the zone
    retested: bool          # has price come back to the flipped zone since
    retest_at: int | None = None


def detect_breakers(df: pd.DataFrame,
                    lookback: int = 20,
                    impulse_threshold: float = 0.005,
                    blocks: list[OrderBlock] | None = None) -> list[BreakerBlock]:
    """Order blocks that were closed through, expressed as flipped zones.

    Requires a CLOSE beyond the zone, not a wick. A wick through an order block
    is the zone being tested and holding -- which is a mitigation, the opposite
    conclusion. Using highs/lows here instead of closes would classify every
    successful defence as a failure.
    """
    if blocks is None:
        # RAW, not detect_order_blocks(): that helper drops broken zones,
        # and a broken zone is exactly what a breaker is.
        blocks = raw_order_blocks(df, lookback=lookback,
                                  impulse_threshold=impulse_threshold)
    out: list[BreakerBlock] = []
    for b in blocks:
        after = df.iloc[b.index + 1:]
        if after.empty:
            continue
        if b.direction == "bullish":
            broken = after.index[after["close"] < b.bottom]
        else:
            broken = after.index[after["close"] > b.top]
        if len(broken) == 0:
            continue
        broken_i = int(df.index.get_loc(broken[0]))

        # Polarity flip: a failed bullish zone becomes bearish and vice versa.
        flipped = "bearish" if b.direction == "bullish" else "bullish"

        rest = df.iloc[broken_i + 1:]
        retested, retest_at = False, None
        if not rest.empty:
            if flipped == "bearish":
                hits = rest.index[rest["high"] >= b.bottom]
            else:
                hits = rest.index[rest["low"] <= b.top]
            if len(hits) > 0:
                retested = True
                retest_at = int(df.index.get_loc(hits[0]))

        out.append(BreakerBlock(
            index=b.index, direction=flipped, origin_direction=b.direction,
            top=b.top, bottom=b.bottom, broken_at=broken_i,
            retested=retested, retest_at=retest_at,
        ))
    return out


def active_breaker(df: pd.DataFrame, price: float, direction: str,
                   tolerance_pct: float = 0.002,
                   **kwargs) -> BreakerBlock | None:
    """A retested breaker that `price` is sitting in, on the right side."""
    want = "bullish" if direction == "long" else "bearish"
    best: BreakerBlock | None = None
    for br in detect_breakers(df, **kwargs):
        if br.direction != want or not br.retested:
            continue
        pad = price * tolerance_pct
        if (br.bottom - pad) <= price <= (br.top + pad):
            if best is None or br.broken_at > best.broken_at:
                best = br
    return best
