"""Mitigation blocks — the zone price returns to before continuing.

The most-discussed concept in the whole ingested corpus and the one with the
largest gap here: "mitigation" appears in 143 of 157 videos (91%) from the
confirmed educator channel, and nothing in this repo modelled it.

The idea, stated plainly. When price leaves a zone in a hurry it leaves orders
behind unfilled. Before continuing, it frequently returns to that zone, fills
them, and only then goes on — the return is the *mitigation*. The tradable
moment is not the zone forming, it is price coming back to it and reacting.

This is deliberately NOT the same as bot/smc/order_blocks.py:

    detect_order_blocks  -> "here is a zone that formed"
    detect_mitigations   -> "here is a zone price CAME BACK to and reacted from"

An order block that price never revisits is not a setup; an order block price
sliced through is invalidated. Only the middle case — returned to, respected —
is what the educator material means by mitigation, and separating the three is
the entire value of this module.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .order_blocks import OrderBlock, detect_order_blocks


@dataclass
class MitigationBlock:
    index: int              # where the zone formed
    direction: str          # "bullish" | "bearish"
    top: float
    bottom: float
    mitigated_at: int       # bar index where price returned into the zone
    reaction_pct: float     # how far price moved away after the return
    respected: bool         # returned AND reacted, without invalidating


def _reaction(df: pd.DataFrame, block: OrderBlock, entry_i: int) -> float:
    """Signed move away from the zone after price re-entered it."""
    after = df.iloc[entry_i + 1:]
    if after.empty:
        return 0.0
    ref = block.top if block.direction == "bullish" else block.bottom
    if ref == 0:
        return 0.0
    if block.direction == "bullish":
        return float((after["high"].max() - ref) / ref)
    return float((ref - after["low"].min()) / ref)


def detect_mitigations(df: pd.DataFrame,
                       lookback: int = 20,
                       impulse_threshold: float = 0.005,
                       min_reaction_pct: float = 0.003,
                       blocks: list[OrderBlock] | None = None) -> list[MitigationBlock]:
    """Zones price returned to and respected.

    min_reaction_pct is what separates a real mitigation from price drifting
    sideways through a zone. Without it, any zone price wandered into counts,
    which would make this fire almost everywhere and mean nothing.
    """
    if blocks is None:
        blocks = detect_order_blocks(df, lookback=lookback,
                                     impulse_threshold=impulse_threshold)
    out: list[MitigationBlock] = []
    for b in blocks:
        after = df.iloc[b.index + 1:]
        if after.empty:
            continue
        # First bar that traded back inside the zone.
        if b.direction == "bullish":
            hits = after.index[(after["low"] <= b.top) & (after["high"] >= b.bottom)]
        else:
            hits = after.index[(after["high"] >= b.bottom) & (after["low"] <= b.top)]
        if len(hits) == 0:
            continue
        entry_i = int(df.index.get_loc(hits[0]))

        # Invalidated? A close through the far side means the zone failed --
        # that is a breaker (see bot/smc/breaker.py), not a mitigation.
        rest = df.iloc[entry_i:]
        if b.direction == "bullish":
            invalidated = bool((rest["close"] < b.bottom).any())
        else:
            invalidated = bool((rest["close"] > b.top).any())

        reaction = _reaction(df, b, entry_i)
        out.append(MitigationBlock(
            index=b.index, direction=b.direction, top=b.top, bottom=b.bottom,
            mitigated_at=entry_i, reaction_pct=reaction,
            respected=bool(not invalidated and reaction >= min_reaction_pct),
        ))
    return out


def active_mitigation(df: pd.DataFrame, price: float, direction: str,
                      tolerance_pct: float = 0.002,
                      **kwargs) -> MitigationBlock | None:
    """A respected mitigation zone that `price` is currently sitting in.

    What bot/screening.py's optional gate calls: not "did a mitigation ever
    happen" but "are we in one right now, on the right side".
    """
    want = "bullish" if direction == "long" else "bearish"
    best: MitigationBlock | None = None
    for m in detect_mitigations(df, **kwargs):
        if not m.respected or m.direction != want:
            continue
        pad = (m.top - m.bottom) * 0.0 + price * tolerance_pct
        if (m.bottom - pad) <= price <= (m.top + pad):
            if best is None or m.mitigated_at > best.mitigated_at:
                best = m
    return best
