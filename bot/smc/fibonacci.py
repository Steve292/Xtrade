"""
Fibonacci retracement + Optimal Trade Entry (OTE) for SMC screening.

In SMC/ICT, price is bought/sold on a retracement into the "golden pocket" —
the 0.618-0.786 band of the last impulse leg (the OTE zone). A long is only a
sniper entry when price pulls back into the OTE of the most recent up-leg (a
discount within the move); a short mirrors it on the last down-leg.
"""

from __future__ import annotations

from .structure import SwingPoint

FIB_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.705, 0.786]
OTE_LOW, OTE_HIGH = 0.618, 0.786


def retracement_levels(start: float, end: float) -> dict[float, float]:
    """Retracement prices for a leg that ran start -> end.

    r=0 sits at `end`, r=1 back at `start`, so it works for up-legs
    (start=low, end=high) and down-legs (start=high, end=low) alike.
    """
    span = end - start
    return {r: end - span * r for r in FIB_RATIOS}


def ote_band(start: float, end: float, lo: float = OTE_LOW, hi: float = OTE_HIGH) -> tuple[float, float]:
    """Return the (low_price, high_price) bounds of the OTE zone for a leg."""
    span = end - start
    a = end - span * lo
    b = end - span * hi
    return (min(a, b), max(a, b))


def in_ote(price: float, start: float, end: float, lo: float = OTE_LOW, hi: float = OTE_HIGH) -> bool:
    low, high = ote_band(start, end, lo, hi)
    return low <= price <= high


def recent_leg(swings: list[SwingPoint], direction: str) -> tuple[float, float] | None:
    """The most recent impulse leg in the trade direction, as (start, end).

    long  -> last swing low into the swing high that followed it (up-leg)
    short -> last swing high into the swing low that followed it (down-leg)
    Returns None if there aren't enough swings to define a leg.
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if not highs or not lows:
        return None

    if direction == "long":
        end_swing = highs[-1]  # top of the up-leg
        prior_lows = [s for s in lows if s.index < end_swing.index]
        if not prior_lows:
            return None
        return (prior_lows[-1].price, end_swing.price)  # (low, high)
    else:
        end_swing = lows[-1]  # bottom of the down-leg
        prior_highs = [s for s in highs if s.index < end_swing.index]
        if not prior_highs:
            return None
        return (prior_highs[-1].price, end_swing.price)  # (high, low)
