"""Wyckoff — trading ranges, springs, upthrusts, and climaxes.

Third-largest gap the corpus found: 399 mentions across 61 of 157 videos (39%)
before three further channels were even ingested, and nothing here modelled any
of it. What ICT calls "smart money", Wyckoff called the composite operator; SMC
is largely a re-labelling of this older framework, so the vocabulary shows up
constantly in SMC material.

WHAT THIS DOES AND DOES NOT DO. Wyckoff's full schematic -- phases A through E,
with named events at each -- is a reading of a chart, and much of it is
genuinely subjective. Automating "this is Phase C" would be inventing precision
that is not there. So this module detects only the parts with an unambiguous
mechanical definition:

    trading range   price contained between a high and a low for N bars
    spring          a dip BELOW the range low that CLOSES back inside
                    -> a failed breakdown; the sellers who broke it are trapped
    upthrust        a poke ABOVE the range high that CLOSES back inside
                    -> a failed breakout; the mirror image
    climax          an unusually wide, unusually heavy bar at a range extreme

and infers a directional bias from those, rather than claiming a phase label.

The close is what separates a spring from a genuine breakdown, exactly as in
bot/smc/breaker.py. A wick below the range is the range being tested; a CLOSE
below it is the range failing. Using lows instead of closes here would report
every real breakdown as a spring and invert the signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class TradingRange:
    start: int
    end: int
    high: float
    low: float

    @property
    def width_pct(self) -> float:
        return (self.high - self.low) / self.low if self.low else 0.0

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class WyckoffEvent:
    index: int
    kind: str          # spring | upthrust | selling_climax | buying_climax
    direction: str     # "bullish" | "bearish"
    price: float


@dataclass
class WyckoffState:
    trading_range: TradingRange | None
    events: list[WyckoffEvent] = field(default_factory=list)
    bias: str = "neutral"      # accumulation | distribution | neutral

    @property
    def direction(self) -> str:
        return {"accumulation": "bullish",
                "distribution": "bearish"}.get(self.bias, "neutral")


def detect_range(df: pd.DataFrame, lookback: int = 40,
                 max_width_pct: float = 0.12,
                 min_bars: int = 10,
                 edge_quantile: float = 0.10) -> TradingRange | None:
    """The most recent consolidation, or None if price is trending.

    Boundaries are QUANTILES of the highs and lows, not the absolute max and
    min. This is not a refinement, it is required for the module to work at
    all: a spring is defined as trading below the range floor, so if the floor
    is the lowest low then the spring's own excursion *becomes* the floor and
    can never be below it. Every spring and upthrust would go undetected, and
    the module would silently report empty. Drawing the edge where price
    repeatedly turned -- which is what a human does by eye -- leaves the single
    excursion outside it, where it belongs.

    max_width_pct then makes the result mean something. Any slice of any chart
    has a highest and a lowest point, so without a width limit this would
    return a "range" for a market in free fall, and every event would be
    measured against boundaries nothing ever respected.
    """
    if df is None or len(df) < min_bars:
        return None
    window = df.iloc[-lookback:] if len(df) > lookback else df
    if len(window) < min_bars:
        return None
    q = min(max(edge_quantile, 0.0), 0.49)
    hi = float(window["high"].quantile(1.0 - q))
    lo = float(window["low"].quantile(q))
    if lo <= 0 or hi <= lo or (hi - lo) / lo > max_width_pct:
        return None
    start = len(df) - len(window)
    return TradingRange(start=start, end=len(df) - 1, high=hi, low=lo)


def detect_events(df: pd.DataFrame, rng: TradingRange,
                  climax_volume_mult: float = 2.0,
                  edge_tolerance: float = 0.15) -> list[WyckoffEvent]:
    """Springs, upthrusts and climaxes within `rng`."""
    events: list[WyckoffEvent] = []
    if rng is None or len(df) == 0:
        return events

    height = rng.high - rng.low
    if height <= 0:
        return events
    has_volume = "volume" in df.columns
    mean_vol = float(df["volume"].tail(60).mean()) if has_volume else 0.0

    for i in range(rng.start, rng.end + 1):
        row = df.iloc[i]
        low, high, close = float(row["low"]), float(row["high"]), float(row["close"])

        # Spring: traded below the floor, closed back above it.
        if low < rng.low and close > rng.low:
            events.append(WyckoffEvent(i, "spring", "bullish", low))
        # Upthrust: traded above the ceiling, closed back below it.
        if high > rng.high and close < rng.high:
            events.append(WyckoffEvent(i, "upthrust", "bearish", high))

        if has_volume and mean_vol > 0:
            vol = float(row.get("volume") or 0.0)
            wide = (high - low) >= height * 0.5
            if vol >= mean_vol * climax_volume_mult and wide:
                # A climax is heavy, wide, and AT an extreme -- heavy and wide
                # in the middle of a range is just noise.
                near_low = (low - rng.low) <= height * edge_tolerance
                near_high = (rng.high - high) <= height * edge_tolerance
                if near_low:
                    events.append(WyckoffEvent(i, "selling_climax", "bullish", low))
                elif near_high:
                    events.append(WyckoffEvent(i, "buying_climax", "bearish", high))
    return events


def analyse(df: pd.DataFrame, **kwargs) -> WyckoffState:
    """Range + events + a directional bias. Never raises."""
    rng = detect_range(df, **{k: v for k, v in kwargs.items()
                              if k in ("lookback", "max_width_pct", "min_bars", "edge_quantile")})
    if rng is None:
        return WyckoffState(None, [], "neutral")
    events = detect_events(df, rng)
    bull = sum(1 for e in events if e.direction == "bullish")
    bear = sum(1 for e in events if e.direction == "bearish")
    if bull > bear:
        bias = "accumulation"
    elif bear > bull:
        bias = "distribution"
    else:
        # Deliberately neutral on a tie rather than picking. A range with equal
        # springs and upthrusts is a market that has rejected BOTH sides, which
        # is genuinely no signal -- not a coin flip.
        bias = "neutral"
    return WyckoffState(rng, events, bias)


def confirms(df: pd.DataFrame, direction: str, **kwargs) -> WyckoffState | None:
    """The state, if its bias agrees with `direction`. For the screening gate."""
    if direction not in ("long", "short"):
        return None
    state = analyse(df, **kwargs)
    want = "bullish" if direction == "long" else "bearish"
    return state if state.direction == want else None
