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


# --- VWAP -----------------------------------------------------------------
#
# Added after ingesting two channels selected for methodological similarity to
# the original source. VWAP appeared in 3% of the first 329 documents and was
# dismissed as noise; in the 101 related-channel documents it appears in 32%.
# That jump is the single clearest thing the comparison surfaced, and it was
# invisible until channels were chosen on a different criterion.
#
# VWAP answers "what did the average unit of volume actually pay", which is a
# different question from the profile above. The profile says WHERE business
# happened across a range; VWAP says what the volume-weighted consensus price
# is right now, and it moves. Institutions benchmark fills against it, which is
# why price so often reacts to it.
#
# The same tick-volume caveat applies and is worth repeating: on MT5 feeds
# `volume` is tick_volume, a count of price changes rather than contracts, so a
# forex VWAP is a proxy. On the CEX side it is genuine.


@dataclass
class VWAPState:
    vwap: float
    upper: float          # +n standard deviations
    lower: float          # -n standard deviations
    stdev: float
    anchor_index: int
    bars: int

    def side(self, price: float, tolerance_pct: float = 0.001) -> str:
        """'above' | 'below' | 'at' -- where price sits relative to VWAP."""
        pad = max(abs(price) * tolerance_pct, 1e-12)
        if price > self.vwap + pad:
            return "above"
        if price < self.vwap - pad:
            return "below"
        return "at"


def typical_price(df: pd.DataFrame) -> pd.Series:
    """(H+L+C)/3 -- the standard VWAP input, not the close.

    Using the close instead would ignore where the bar actually traded, which
    is the whole point of a volume-WEIGHTED average.
    """
    return (df["high"] + df["low"] + df["close"]) / 3.0


def vwap_series(df: pd.DataFrame, anchor_index: int = 0) -> pd.Series | None:
    """Cumulative VWAP from `anchor_index` forward. None if unusable.

    Anchoring matters more than the formula. A VWAP running from the start of
    whatever data happened to be fetched is an artifact of the fetch window,
    not a level anyone is trading against. Anchor it to something real -- a
    session open, a swing extreme, the start of the current range.
    """
    if df is None or len(df) == 0 or "volume" not in df.columns:
        return None
    anchor_index = max(0, min(int(anchor_index), len(df) - 1))
    window = df.iloc[anchor_index:]
    vol = window["volume"].astype(float)
    if float(vol.sum()) <= 0:
        return None
    tp = typical_price(window)
    return (tp * vol).cumsum() / vol.cumsum()


def vwap_state(df: pd.DataFrame, anchor_index: int = 0,
               stdevs: float = 2.0) -> VWAPState | None:
    """Current VWAP plus volume-weighted standard-deviation bands."""
    series = vwap_series(df, anchor_index)
    if series is None or len(series) == 0:
        return None
    window = df.iloc[max(0, min(int(anchor_index), len(df) - 1)):]
    vol = window["volume"].astype(float)
    tp = typical_price(window)
    current = float(series.iloc[-1])
    total = float(vol.sum())
    if total <= 0:
        return None
    # Volume-weighted variance about the running VWAP, not a plain std of
    # price: a plain std would weight a one-lot print the same as the heaviest
    # bar of the session and produce bands nobody trades.
    var = float((vol * (tp - series) ** 2).sum() / total)
    sd = var ** 0.5 if var > 0 else 0.0
    return VWAPState(vwap=current, upper=current + sd * stdevs,
                     lower=current - sd * stdevs, stdev=sd,
                     anchor_index=int(anchor_index), bars=len(window))


def anchor_to_recent_extreme(df: pd.DataFrame, lookback: int = 100,
                             kind: str = "low") -> int:
    """Index of the most recent swing extreme, for anchoring VWAP to it."""
    if df is None or len(df) == 0:
        return 0
    window = df.iloc[-lookback:] if len(df) > lookback else df
    offset = len(df) - len(window)
    col = "low" if kind == "low" else "high"
    pos = window[col].idxmin() if kind == "low" else window[col].idxmax()
    try:
        return int(df.index.get_loc(pos))
    except Exception:
        return offset


def at_vwap(df: pd.DataFrame, price: float, direction: str,
            tolerance_pct: float = 0.002,
            anchor_index: int | None = None,
            stdevs: float = 2.0) -> bool:
    """Is `price` on the side of VWAP that `direction` wants?

    Long below/at VWAP, short above/at it -- buying under the volume-weighted
    consensus price and selling above it. Buying well ABOVE VWAP is paying more
    than the average participant, which is the entry these educators warn
    against, so it fails rather than abstains.
    """
    if anchor_index is None:
        anchor_index = anchor_to_recent_extreme(
            df, kind="low" if direction == "long" else "high")
    st = vwap_state(df, anchor_index, stdevs)
    if st is None:
        return False
    side = st.side(price, tolerance_pct)
    if side == "at":
        return True
    return side == "below" if direction == "long" else side == "above"
