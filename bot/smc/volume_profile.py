"""
Volume profile and VWAP — where volume actually traded, not just where price went.

The single most-discussed concept group in the ingested corpus (4,128
mentions: `volume_profile` 3,536 + `vwap` 592), and the one nothing in
bot/smc/ modelled. Every other detector here reads the price path; this reads
the volume distribution ACROSS price, which answers a different question:
which levels did the market actually do business at, and which did it pass
through in a hurry?

Two outputs:

  - POC (point of control): the price bin that traded the most volume. Acts
    as a magnet — price tends to return to it.
  - Value area: the contiguous band around the POC holding `value_area_pct`
    (conventionally 70%) of total volume. Inside the value area is balance;
    outside is imbalance, and edges of the value area are where reactions
    cluster.

Volume is distributed across every bin a bar's high-low range spans, not
dumped into the bar's typical-price bin. Bucketing a wide-range bar at a
single price would put volume where trading demonstrably did not happen, and
those are exactly the volatile bars whose placement matters most.

VWAP is kept separate and simple: a running volume-weighted mean of typical
price. It is a fair-value reference, not a profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

DEFAULT_BINS = 24
DEFAULT_VALUE_AREA_PCT = 0.70


@dataclass
class VolumeProfile:
    poc: float  # point of control — highest-volume price
    value_area_high: float
    value_area_low: float
    bin_edges: list[float] = field(default_factory=list)
    bin_volumes: list[float] = field(default_factory=list)
    total_volume: float = 0.0

    def in_value_area(self, price: float) -> bool:
        return self.value_area_low <= price <= self.value_area_high


def build_volume_profile(
    df: pd.DataFrame,
    bins: int = DEFAULT_BINS,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
    lookback: int | None = None,
) -> VolumeProfile | None:
    """Volume profile over the last `lookback` bars (all bars when None).

    Returns None when a profile is meaningless: no bars, no volume, or a
    completely flat price range. Callers treat None as "no read", which is
    honest — a degenerate profile whose POC is an arbitrary bin would be
    worse than no answer.
    """
    if len(df) == 0 or bins <= 0:
        return None

    frame = df if lookback is None else df.iloc[-lookback:]
    if len(frame) == 0:
        return None

    highs = frame["high"].astype(float).values
    lows = frame["low"].astype(float).values
    volumes = frame["volume"].astype(float).values if "volume" in frame else None
    if volumes is None:
        return None

    lo = float(lows.min())
    hi = float(highs.max())
    total_volume = float(volumes.sum())
    if hi <= lo or total_volume <= 0:
        return None

    width = (hi - lo) / bins
    edges = [lo + width * i for i in range(bins + 1)]
    bin_volumes = [0.0] * bins

    for h, l, v in zip(highs, lows, volumes):
        if v <= 0:
            continue
        first = int((l - lo) / width)
        last = int((h - lo) / width)
        first = max(0, min(bins - 1, first))
        last = max(0, min(bins - 1, last))
        span = last - first + 1
        share = v / span
        for b in range(first, last + 1):
            bin_volumes[b] += share

    poc_idx = max(range(bins), key=lambda i: bin_volumes[i])
    poc = (edges[poc_idx] + edges[poc_idx + 1]) / 2

    # Grow outward from the POC, always taking the heavier neighbour, until
    # the requested share of volume is enclosed.
    target = total_volume * value_area_pct
    low_idx = high_idx = poc_idx
    covered = bin_volumes[poc_idx]
    while covered < target and (low_idx > 0 or high_idx < bins - 1):
        below = bin_volumes[low_idx - 1] if low_idx > 0 else -1.0
        above = bin_volumes[high_idx + 1] if high_idx < bins - 1 else -1.0
        if above >= below:
            high_idx += 1
            covered += bin_volumes[high_idx]
        else:
            low_idx -= 1
            covered += bin_volumes[low_idx]

    return VolumeProfile(
        poc=poc,
        value_area_high=edges[high_idx + 1],
        value_area_low=edges[low_idx],
        bin_edges=edges,
        bin_volumes=bin_volumes,
        total_volume=total_volume,
    )


def vwap(df: pd.DataFrame, lookback: int | None = None) -> float | None:
    """Volume-weighted average price of typical price ((h+l+c)/3).

    None when there are no bars or no volume — same reasoning as
    build_volume_profile: no read beats a meaningless number.
    """
    if len(df) == 0:
        return None
    frame = df if lookback is None else df.iloc[-lookback:]
    if len(frame) == 0 or "volume" not in frame:
        return None

    typical = (
        frame["high"].astype(float)
        + frame["low"].astype(float)
        + frame["close"].astype(float)
    ) / 3.0
    volumes = frame["volume"].astype(float)
    total = float(volumes.sum())
    if total <= 0:
        return None
    return float((typical * volumes).sum() / total)


def vwap_bias(price: float, vwap_value: float | None) -> str:
    """"bullish" above VWAP, "bearish" below, "neutral" without a reading."""
    if vwap_value is None:
        return "neutral"
    if price > vwap_value:
        return "bullish"
    if price < vwap_value:
        return "bearish"
    return "neutral"
