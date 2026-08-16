"""
Supply & Demand zones.

A demand zone is the price base a swing LOW sits in when a strong impulse
rallied away from it — the footprint of institutional buying that price tends
to react to on a return. A supply zone is the mirror: a swing HIGH that a
strong drop launched from (institutional selling).

Distinct from order blocks (bot/smc/order_blocks.py): an order block is the
*last opposing candle* before an impulse; an S/D zone is anchored to the *swing
extreme* that the impulse originated from. They often overlap but answer
different questions — "which candle?" vs. "which structural level?".

A zone stays valid until price closes back through it (mitigated), at which
point the orders that formed it are considered filled and it's dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .structure import SwingPoint


@dataclass
class SupplyDemandZone:
    index: int
    kind: str  # "demand" | "supply"
    top: float
    bottom: float
    strength: float  # magnitude of the impulse that created the zone
    mitigated: bool = False


def detect_supply_demand_zones(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    impulse_pct: float = 0.01,
    forward: int = 3,
) -> list[SupplyDemandZone]:
    """Unmitigated demand/supply zones, anchored to swing extremes that a
    strong (>= impulse_pct over `forward` bars) move launched away from."""
    if len(df) == 0:
        return []
    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    zones: list[SupplyDemandZone] = []
    for s in swings:
        i = s.index
        if i + forward >= n or closes[i] == 0:
            continue
        move = (closes[i + forward] - closes[i]) / closes[i]

        if s.kind == "low" and move >= impulse_pct:
            # Demand: wick low up to the candle body top.
            bottom = float(lows[i])
            top = float(max(opens[i], closes[i]))
            if top > bottom:
                zones.append(SupplyDemandZone(i, "demand", top, bottom, abs(float(move))))
        elif s.kind == "high" and move <= -impulse_pct:
            # Supply: candle body bottom up to the wick high.
            top = float(highs[i])
            bottom = float(min(opens[i], closes[i]))
            if top > bottom:
                zones.append(SupplyDemandZone(i, "supply", top, bottom, abs(float(move))))

    return _mark_mitigated(df, zones)


def _mark_mitigated(df: pd.DataFrame, zones: list[SupplyDemandZone]) -> list[SupplyDemandZone]:
    """Drop zones price has already closed through — their orders are filled."""
    active: list[SupplyDemandZone] = []
    for z in zones:
        future = df.iloc[z.index + 1 :]
        if z.kind == "demand":
            broken = bool((future["close"] < z.bottom).any())
        else:
            broken = bool((future["close"] > z.top).any())
        z.mitigated = broken
        if not broken:
            active.append(z)
    return active


def price_in_zone(price: float, zone: SupplyDemandZone) -> bool:
    return zone.bottom <= price <= zone.top


def nearest_zone(
    price: float, zones: list[SupplyDemandZone], kind: str, tolerance_pct: float = 0.005
) -> SupplyDemandZone | None:
    """The `kind` ("demand"/"supply") zone price is inside, or within
    tolerance_pct of — i.e. price is reacting at that zone. None if price isn't
    at any zone of that kind."""
    candidates = [z for z in zones if z.kind == kind]
    for z in candidates:  # inside a zone takes priority
        if z.bottom <= price <= z.top:
            return z
    for z in candidates:  # otherwise, within a tolerance band around it
        band = z.top * tolerance_pct
        if z.bottom - band <= price <= z.top + band:
            return z
    return None
