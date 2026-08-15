"""Volume profile — where volume actually traded, not when.

Second-largest gap the ingested corpus found: 333 mentions across 84 of 157
videos (54%), and nothing here computed it. The quotes are methodological, not
passing references -- "a requirement for a range deviation trade is that we have
a break back inside the volume profile".

A normal chart plots volume against TIME. A volume profile plots it against
PRICE: how much traded at each level, regardless of when. That turns "the market
spent a long time here" into "the market did a lot of business here", which is
what makes the point of control and value area meaningful as support and
resistance.

    POC          the single price level with the most volume
    Value area   the narrowest band holding `value_area_pct` of all volume
                 (70% by convention)
    HVN          high volume node -- price accepted, tends to attract and hold
    LVN          low volume node -- price rejected, tends to move through fast

A CAVEAT THAT MATTERS ON FOREX. bot/mt5/client.py renames MT5's `tick_volume`
to `volume`, and tick volume counts PRICE CHANGES, not contracts. It correlates
with real activity well enough to be useful, but it is not traded size, and a
profile built on it is a proxy. On the CEX side (bot/exchange.py) the volume is
genuine. Same code, different confidence -- worth knowing before treating a POC
on EURUSDc as though it meant what a POC on BTC/USDT means.

Each candle's volume is spread evenly across the bins its high-low range covers.
That is the standard approximation: without tick-level data there is no way to
know where inside the bar the volume actually printed, and pretending otherwise
(assigning it all to the close, say) produces a profile shaped by bar timing
rather than by price.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class VolumeNode:
    low: float
    high: float
    volume: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class VolumeProfile:
    poc: float                       # price of the point of control
    value_area_low: float
    value_area_high: float
    total_volume: float
    nodes: list[VolumeNode] = field(default_factory=list)

    def contains(self, price: float) -> bool:
        return self.value_area_low <= price <= self.value_area_high


def build_profile(df: pd.DataFrame, bins: int = 50,
                  lookback: int | None = None,
                  value_area_pct: float = 0.70) -> VolumeProfile | None:
    """Volume distribution across price. None when there is nothing to profile."""
    if df is None or len(df) == 0 or "volume" not in df.columns:
        return None
    window = df if lookback is None else df.iloc[-lookback:]
    if len(window) == 0:
        return None

    lo = float(window["low"].min())
    hi = float(window["high"].max())
    if not (hi > lo):
        return None
    bins = max(2, int(bins))
    step = (hi - lo) / bins
    buckets = [0.0] * bins

    for _idx, row in window.iterrows():
        r_low, r_high = float(row["low"]), float(row["high"])
        vol = float(row.get("volume") or 0.0)
        if vol <= 0:
            continue
        first = min(bins - 1, max(0, int((r_low - lo) / step)))
        last = min(bins - 1, max(0, int((r_high - lo) / step)))
        span = last - first + 1
        share = vol / span
        for b in range(first, last + 1):
            buckets[b] += share

    total = sum(buckets)
    if total <= 0:
        return None

    nodes = [VolumeNode(lo + i * step, lo + (i + 1) * step, v)
             for i, v in enumerate(buckets)]
    poc_i = max(range(bins), key=lambda i: buckets[i])

    # Grow outward from the POC, always taking the heavier neighbour, until the
    # requested share of volume is enclosed. This is the standard construction:
    # the value area is the NARROWEST band containing that volume, so it has to
    # be grown from the peak rather than taken as a fixed percentile of price.
    lo_i = hi_i = poc_i
    acc = buckets[poc_i]
    target = total * value_area_pct
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        below = buckets[lo_i - 1] if lo_i > 0 else -1.0
        above = buckets[hi_i + 1] if hi_i < bins - 1 else -1.0
        if above >= below:
            hi_i += 1
            acc += buckets[hi_i]
        else:
            lo_i -= 1
            acc += buckets[lo_i]

    return VolumeProfile(
        poc=nodes[poc_i].mid,
        value_area_low=nodes[lo_i].low,
        value_area_high=nodes[hi_i].high,
        total_volume=total,
        nodes=nodes,
    )


def high_volume_nodes(profile: VolumeProfile,
                      threshold: float = 1.5) -> list[VolumeNode]:
    """Bins carrying `threshold`x the mean — price the market accepted."""
    if not profile.nodes:
        return []
    mean = profile.total_volume / len(profile.nodes)
    return [n for n in profile.nodes if n.volume >= mean * threshold]


def low_volume_nodes(profile: VolumeProfile,
                     threshold: float = 0.35) -> list[VolumeNode]:
    """Bins carrying less than `threshold`x the mean — price the market rejected.

    LVNs are the tradable half of the idea: price moves through them quickly, so
    they make poor targets and good stop placement, and a move that stalls in one
    is a move losing conviction.
    """
    if not profile.nodes:
        return []
    mean = profile.total_volume / len(profile.nodes)
    return [n for n in profile.nodes if n.volume <= mean * threshold]


def nearest_node(price: float, nodes: list[VolumeNode]) -> VolumeNode | None:
    if not nodes:
        return None
    return min(nodes, key=lambda n: abs(n.mid - price))


def at_value_area_edge(df: pd.DataFrame, price: float,
                       tolerance_pct: float = 0.002,
                       **kwargs) -> str | None:
    """'low' | 'high' | None — is price at an edge of the value area?

    The edges are where the range-deviation trades in the corpus happen: price
    leaves the value area, fails, and breaks back inside. Being in the MIDDLE of
    the value area is the opposite -- fair value, no edge, nothing to trade.
    """
    profile = build_profile(df, **kwargs)
    if profile is None:
        return None
    pad = price * tolerance_pct
    if abs(price - profile.value_area_low) <= pad:
        return "low"
    if abs(price - profile.value_area_high) <= pad:
        return "high"
    return None
