"""Inducement (IDM) — the liquidity taken before price reaches the real zone.

The largest unmapped concept the corpus surfaced: 115 of 639 documents, 8 of
10 channels, 302 mentions. Larger than breaker and wyckoff, both of which had
detectors while this had none.

WHAT IT IS
----------
Inside the leg that produced the most recent swing high, there is a pullback
low. Stops sit under it. Educators describe price reaching down to take that
liquidity BEFORE continuing to the order block or demand zone that is the
actual point of interest -- so an entry placed at the zone without waiting for
the inducement to be taken is an entry placed in front of a stop hunt.

That gives a testable definition rather than a vibe:

    long  -> the last swing LOW preceding the most recent swing HIGH.
             Its stops are sell-side liquidity.
    short -> the last swing HIGH preceding the most recent swing LOW.
             Its stops are buy-side liquidity.

`taken` is then simply whether price has since traded through that level.

WHAT IT IS NOT
--------------
It is not a signal. Inducement present-and-untaken is a reason to WAIT, and
inducement taken is a precondition, never a trigger on its own -- the same
shape as bot/smc/mitigation.py. Nothing here proposes a direction; the caller
already has one and is asking whether the liquidity in front of it is gone.

Pure functions over an OHLC frame. Not wired into the live path by default.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.smc.structure import SwingPoint, find_swing_points


@dataclass
class Inducement:
    index: int
    level: float
    kind: str          # "sell_side" (below price) | "buy_side" (above price)
    taken: bool        # has price traded through it since it formed
    distance_pct: float  # |current price - level| / level


def find_inducement(
    df: pd.DataFrame,
    direction: str,
    swings: list[SwingPoint] | None = None,
    lookback: int = 5,
) -> Inducement | None:
    """The inducement standing between price and its point of interest.

    Returns None when the structure has not formed one -- which is a real
    answer, not a failure. A leg with no interior pullback has no inducement,
    and inventing one would put a level on the chart that nobody's stops are
    actually behind.
    """
    if direction not in ("long", "short"):
        return None
    if df is None or len(df) < 3:
        return None

    swings = swings if swings is not None else find_swing_points(df, lookback)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if not highs or not lows:
        return None

    if direction == "long":
        anchor = highs[-1]
        prior = [s for s in lows if s.index < anchor.index]
        kind = "sell_side"
    else:
        anchor = lows[-1]
        prior = [s for s in highs if s.index < anchor.index]
        kind = "buy_side"
    if not prior:
        return None

    idm = prior[-1]
    # A leg with no displacement is not a leg. On a flat or near-flat series
    # swing detection can mark ties as both highs and lows, which would report
    # an "inducement" at exactly the anchor price -- a level nobody's stops are
    # behind. Reject it rather than emit a meaningless one.
    if idm.price == anchor.price:
        return None
    after = df.iloc[idm.index + 1:]
    if len(after) == 0:
        taken = False
    elif kind == "sell_side":
        taken = float(after["low"].min()) < idm.price
    else:
        taken = float(after["high"].max()) > idm.price

    price = float(df.iloc[-1]["close"])
    dist = abs(price - idm.price) / idm.price if idm.price else 0.0
    return Inducement(idm.index, float(idm.price), kind, bool(taken), dist)


def inducement_taken(
    df: pd.DataFrame,
    direction: str,
    swings: list[SwingPoint] | None = None,
    lookback: int = 5,
) -> bool:
    """True when the liquidity in front of the trade is already gone.

    Absence of an inducement returns True, deliberately. The gate this feeds
    asks "is there liquidity still to be taken before my zone" -- and if the
    structure never built one, the honest answer is no, not "refuse". Returning
    False there would block every clean leg for failing to contain a trap.
    """
    idm = find_inducement(df, direction, swings=swings, lookback=lookback)
    return True if idm is None else idm.taken


def describe(idm: Inducement | None) -> str:
    """One-line reading for a screen check's detail column."""
    if idm is None:
        return "no inducement in this leg"
    return (f"{idm.kind} @ {idm.level:.6g} "
            f"({'taken' if idm.taken else 'NOT taken'}, "
            f"{idm.distance_pct:.2%} away)")
