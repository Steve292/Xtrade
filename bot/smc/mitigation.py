"""
Mitigation blocks — the origin candle of a move that broke structure.

"Mitigation" is the corpus's term (1,564 mentions) for price returning to the
origin of a move so that positions left unfilled there can be filled — the
institution's remaining orders get mitigated on the retrace. The tradeable
object is that origin zone, on the return.

How this differs from its two neighbours in bot/smc/, since all three are
"a zone price came from" and the distinction is easy to lose:

  - order_blocks.py  — anchored on a PRICE IMPULSE (>= impulse_threshold over
    a few bars). Says nothing about whether structure actually changed.
  - supply_demand.py — anchored on a SWING EXTREME the impulse launched from.
  - this module      — anchored on a STRUCTURE BREAK (bot/smc/structure.py's
    BOS/CHoCH events). The move must have actually broken structure, not
    merely been large.

That last distinction is the reason to have it: a big candle that breaks
nothing is momentum; a candle that breaks structure is a change in who is in
control, and the corpus treats the origin of the SECOND kind as the higher-
quality zone. This is a documented interpretation — the corpus names the
concept and maps it here, but does not specify a detection rule — chosen to
reuse the structure detection this project already has rather than invent a
fourth notion of "significant move".

A block stays live until price trades back into it (mitigated=True). Unlike
order blocks, mitigated ones are RETURNED rather than dropped, because the
mitigation event is itself the signal the caller usually wants.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .structure import StructureEvent, SwingPoint, detect_structure_breaks, find_swing_points


@dataclass
class MitigationBlock:
    index: int  # the origin candle
    direction: str  # "bullish" | "bearish" — the direction of the break it launched
    top: float
    bottom: float
    event_index: int  # bar at which structure broke
    event_kind: str  # "bos" | "choch"
    mitigated: bool  # price has traded back into the zone since the break


def detect_mitigation_blocks(
    df: pd.DataFrame,
    swings: list[SwingPoint] | None = None,
    events: list[StructureEvent] | None = None,
    max_origin_distance: int = 10,
) -> list[MitigationBlock]:
    """Origin zones of structure-breaking moves.

    `swings`/`events` are accepted so a caller that already computed them
    (SMCStrategy.analyze does) doesn't pay for them twice; both are derived
    here when omitted. `max_origin_distance` bounds how far back from the
    break the origin candle may sit — beyond that the "origin" is unrelated
    to the break.
    """
    if len(df) == 0:
        return []
    if swings is None:
        swings = find_swing_points(df)
    if events is None:
        events = detect_structure_breaks(df, swings)
    if not events:
        return []

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    blocks: list[MitigationBlock] = []
    for ev in events:
        if ev.index >= n:
            continue
        lo = max(0, ev.index - max_origin_distance)

        # The origin is the last candle OPPOSING the break direction before it:
        # the last down-candle before a bullish break, and vice versa. That is
        # the candle whose unfilled orders the retrace comes back to mitigate.
        origin = None
        for i in range(ev.index, lo - 1, -1):
            is_down = closes[i] < opens[i]
            if ev.direction == "bullish" and is_down:
                origin = i
                break
            if ev.direction == "bearish" and not is_down:
                origin = i
                break

        if origin is None:
            continue

        top = float(highs[origin])
        bottom = float(lows[origin])

        mitigated = False
        for j in range(ev.index + 1, n):
            if lows[j] <= top and highs[j] >= bottom:
                mitigated = True
                break

        blocks.append(
            MitigationBlock(
                index=origin,
                direction=ev.direction,
                top=top,
                bottom=bottom,
                event_index=int(ev.index),
                event_kind=ev.kind,
                mitigated=mitigated,
            )
        )

    return blocks


def price_in_mitigation_block(price: float, block: MitigationBlock) -> bool:
    return block.bottom <= price <= block.top


def nearest_unmitigated(
    price: float, blocks: list[MitigationBlock], direction: str
) -> MitigationBlock | None:
    """Most recent UNMITIGATED block of `direction` that price sits inside.

    Unmitigated is the point: once price has already traded back through the
    zone the orders there are considered filled, and the zone's pull is spent.
    """
    for b in reversed(blocks):
        if b.direction == direction and not b.mitigated and price_in_mitigation_block(price, b):
            return b
    return None
