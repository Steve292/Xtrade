"""Candlestick patterns — the entry-trigger layer this bot never had.

Nothing in this repo could read an individual candle before now. bot/indicators.py
stops at EMA/RSI/MACD; the rest of bot/smc/ reasons about swings, zones and gaps.
So the seven-gate screen could tell you price was sitting in a discounted demand
zone after a liquidity sweep, and still had no way to require that the market
actually *rejected* from it before committing.

That gap is not theoretical. Ingesting 157 videos (102 hours) from the confirmed
educator channel found "wait for the candle close" in 49 of them and pin-bar /
rejection-wick language in 29 — consistently used as the final confirmation
before entry, and consistently absent here.

Everything in this module is a pure function over an OHLC frame. Nothing is
wired into the live path by default; bot/screening.py exposes these as gates
that are off unless explicitly enabled, following the same convention
capital_guard.py uses for its newer breakers.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class CandlePattern:
    index: int
    name: str          # engulfing | pin_bar | doji | inside_bar | outside_bar | marubozu
    direction: str     # "bullish" | "bearish" | "neutral"
    strength: float    # 0.0-1.0, pattern-specific conviction


def _parts(df: pd.DataFrame, i: int) -> tuple[float, float, float, float]:
    row = df.iloc[i]
    return (float(row["open"]), float(row["high"]),
            float(row["low"]), float(row["close"]))


def body(df: pd.DataFrame, i: int) -> float:
    o, _h, _l, c = _parts(df, i)
    return abs(c - o)


def candle_range(df: pd.DataFrame, i: int) -> float:
    _o, h, l, _c = _parts(df, i)
    return h - l


def upper_wick(df: pd.DataFrame, i: int) -> float:
    o, h, _l, c = _parts(df, i)
    return h - max(o, c)


def lower_wick(df: pd.DataFrame, i: int) -> float:
    o, _h, l, c = _parts(df, i)
    return min(o, c) - l


def is_bullish(df: pd.DataFrame, i: int) -> bool:
    o, _h, _l, c = _parts(df, i)
    return c > o


def detect_engulfing(df: pd.DataFrame, i: int,
                     min_body_ratio: float = 1.0) -> CandlePattern | None:
    """Current body fully engulfs the previous body, in the other direction.

    Bodies, not ranges: an engulfing candle is about where the market OPENED
    and CLOSED relative to the prior candle. Comparing high-to-low instead
    would fire on any wide-ranging bar and mean nothing.
    """
    if i < 1:
        return None
    po, _ph, _pl, pc = _parts(df, i - 1)
    o, _h, _l, c = _parts(df, i)
    prev_body, cur_body = abs(pc - po), abs(c - o)
    if prev_body <= 0 or cur_body < prev_body * min_body_ratio:
        return None
    prev_bull = pc > po
    cur_bull = c > o
    if prev_bull == cur_bull:
        return None                      # must reverse direction
    if cur_bull and c >= max(po, pc) and o <= min(po, pc):
        return CandlePattern(i, "engulfing", "bullish",
                             min(1.0, cur_body / (prev_body * 2)))
    if not cur_bull and c <= min(po, pc) and o >= max(po, pc):
        return CandlePattern(i, "engulfing", "bearish",
                             min(1.0, cur_body / (prev_body * 2)))
    return None


def detect_pin_bar(df: pd.DataFrame, i: int,
                   wick_ratio: float = 2.0,
                   max_body_pct: float = 0.35) -> CandlePattern | None:
    """A long rejection wick with a small body — hammer / shooting star.

    Direction is the OPPOSITE of the wick: a long lower wick means sellers were
    rejected, which is bullish. This trips people up constantly, so it is worth
    naming explicitly rather than leaving to the reader.
    """
    rng = candle_range(df, i)
    if rng <= 0:
        return None
    b = body(df, i)
    if b / rng > max_body_pct:
        return None
    up, low = upper_wick(df, i), lower_wick(df, i)
    if low >= up * wick_ratio and low / rng >= 0.5:
        return CandlePattern(i, "pin_bar", "bullish", min(1.0, low / rng))
    if up >= low * wick_ratio and up / rng >= 0.5:
        return CandlePattern(i, "pin_bar", "bearish", min(1.0, up / rng))
    return None


def detect_doji(df: pd.DataFrame, i: int,
                max_body_pct: float = 0.1) -> CandlePattern | None:
    """Open and close nearly equal — indecision, direction deliberately neutral."""
    rng = candle_range(df, i)
    if rng <= 0:
        return None
    if body(df, i) / rng <= max_body_pct:
        return CandlePattern(i, "doji", "neutral", 1.0 - (body(df, i) / rng))
    return None


def detect_inside_bar(df: pd.DataFrame, i: int) -> CandlePattern | None:
    """Range contained entirely within the previous candle — compression."""
    if i < 1:
        return None
    _o, ph, pl, _c = _parts(df, i - 1)
    _o2, h, l, _c2 = _parts(df, i)
    if h <= ph and l >= pl:
        prev_rng = ph - pl
        ratio = (h - l) / prev_rng if prev_rng > 0 else 0.0
        return CandlePattern(i, "inside_bar", "neutral", 1.0 - min(1.0, ratio))
    return None


def detect_outside_bar(df: pd.DataFrame, i: int) -> CandlePattern | None:
    """Range fully contains the previous candle — expansion."""
    if i < 1:
        return None
    _o, ph, pl, _c = _parts(df, i - 1)
    _o2, h, l, _c2 = _parts(df, i)
    if h >= ph and l <= pl and (h - l) > (ph - pl):
        return CandlePattern(i, "outside_bar",
                             "bullish" if is_bullish(df, i) else "bearish",
                             min(1.0, (h - l) / (ph - pl) - 1.0))
    return None


def detect_marubozu(df: pd.DataFrame, i: int,
                    min_body_pct: float = 0.9) -> CandlePattern | None:
    """Almost all body, almost no wick — conviction / displacement."""
    rng = candle_range(df, i)
    if rng <= 0:
        return None
    ratio = body(df, i) / rng
    if ratio >= min_body_pct:
        return CandlePattern(i, "marubozu",
                             "bullish" if is_bullish(df, i) else "bearish", ratio)
    return None


_DETECTORS = (detect_engulfing, detect_pin_bar, detect_marubozu,
              detect_outside_bar, detect_inside_bar, detect_doji)


def detect_candles(df: pd.DataFrame, lookback: int = 5) -> list[CandlePattern]:
    """Every pattern on the last `lookback` closed candles, newest last."""
    out: list[CandlePattern] = []
    start = max(1, len(df) - lookback)
    for i in range(start, len(df)):
        for fn in _DETECTORS:
            found = fn(df, i)
            if found is not None:
                out.append(found)
    return out


def confirms(df: pd.DataFrame, direction: str, lookback: int = 3,
             min_strength: float = 0.0) -> CandlePattern | None:
    """The most recent candle pattern agreeing with `direction`, if any.

    This is the function bot/screening.py's optional gate calls. Neutral
    patterns (doji, inside bar) never confirm: indecision is not a reason to
    enter, and treating it as one would make the gate weaker than no gate.
    """
    if direction not in ("long", "short"):
        return None
    want = "bullish" if direction == "long" else "bearish"
    for p in reversed(detect_candles(df, lookback=lookback)):
        if p.direction == want and p.strength >= min_strength:
            return p
    return None


def closed_beyond(df: pd.DataFrame, level: float, direction: str,
                  index: int | None = None) -> bool:
    """Did the candle CLOSE past `level`, rather than merely wick through it?

    "Wait for the candle close" was the single most repeated confirmation rule
    in the ingested corpus (49 of 157 videos). A wick through a level is a test;
    a close beyond it is acceptance, and the distinction is the whole point.
    """
    if len(df) == 0:
        return False
    i = len(df) - 1 if index is None else index
    close = float(df.iloc[i]["close"])
    return close > level if direction == "long" else close < level
