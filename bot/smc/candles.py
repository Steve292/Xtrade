"""
Candle-level rejection signatures: wicks, pin bars, and close quality.

The corpus this project ingests talks about `wick`, `candle_close` and
`pin_bar` more than almost anything else (3,143 mentions across 955
transcripts) — they are the bar-by-bar confirmation traders layer on top of
the structural read. Nothing in bot/smc/ modelled them: order blocks and S/D
zones answer "which level?", this answers "is price actually rejecting it
right now?".

A pin bar is a single candle whose wick dominates its body, marking a level
that was probed and refused:

  - Bullish pin (hammer): long LOWER wick — price pushed down, sellers failed,
    close recovered. Rejection of lower prices.
  - Bearish pin (shooting star): long UPPER wick — the mirror.

Close quality is separate and blunter: where in the bar's own range the close
landed. A close in the top fifth of the range is a strong bullish close
regardless of wick geometry.

Everything here is a pure read over the OHLCV frame — no state, no config
coupling. Callers decide what a signature is worth.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# A wick must be at least this multiple of the body to count as a pin.
DEFAULT_WICK_BODY_RATIO = 2.0
# ...and the opposing wick at most this fraction of the range, so a doji with
# two long wicks (indecision, not rejection) doesn't read as a pin either way.
MAX_OPPOSING_WICK_PCT = 0.25
# Close within this fraction of the range's extreme = a "strong" close.
STRONG_CLOSE_PCT = 0.20


@dataclass
class CandleSignature:
    index: int
    kind: str  # "bullish_pin" | "bearish_pin" | "bullish_close" | "bearish_close" | "neutral"
    upper_wick_pct: float  # fraction of the bar's total range
    lower_wick_pct: float
    body_pct: float
    close_position: float  # 0.0 = closed at the low, 1.0 = closed at the high
    strength: float  # 0.0 - 1.0, how pronounced the signature is


def classify_candle(df: pd.DataFrame, index: int = -1) -> CandleSignature:
    """Classify one bar's rejection signature. A zero-range bar (open == high
    == low == close, seen on illiquid symbols and synthetic data) is neutral
    with zero strength rather than a division error."""
    row = df.iloc[index]
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])

    idx = index if index >= 0 else len(df) + index
    rng = h - l
    if rng <= 0:
        return CandleSignature(idx, "neutral", 0.0, 0.0, 0.0, 0.5, 0.0)

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    upper_pct = upper_wick / rng
    lower_pct = lower_wick / rng
    body_pct = body / rng
    close_position = (c - l) / rng

    # Pin bars first — the stronger claim, since they require geometry on both
    # sides of the body, not just where the close landed.
    if body > 0:
        wick_body = lower_wick / body if body else 0.0
        if wick_body >= DEFAULT_WICK_BODY_RATIO and upper_pct <= MAX_OPPOSING_WICK_PCT:
            return CandleSignature(
                idx, "bullish_pin", upper_pct, lower_pct, body_pct, close_position,
                strength=min(lower_pct, 1.0),
            )
        wick_body = upper_wick / body if body else 0.0
        if wick_body >= DEFAULT_WICK_BODY_RATIO and lower_pct <= MAX_OPPOSING_WICK_PCT:
            return CandleSignature(
                idx, "bearish_pin", upper_pct, lower_pct, body_pct, close_position,
                strength=min(upper_pct, 1.0),
            )

    # Fall back to close quality.
    if close_position >= 1.0 - STRONG_CLOSE_PCT:
        return CandleSignature(
            idx, "bullish_close", upper_pct, lower_pct, body_pct, close_position,
            strength=close_position,
        )
    if close_position <= STRONG_CLOSE_PCT:
        return CandleSignature(
            idx, "bearish_close", upper_pct, lower_pct, body_pct, close_position,
            strength=1.0 - close_position,
        )

    return CandleSignature(
        idx, "neutral", upper_pct, lower_pct, body_pct, close_position, strength=0.0
    )


def detect_pin_bars(df: pd.DataFrame, lookback: int = 20) -> list[CandleSignature]:
    """Pin bars within the last `lookback` bars, oldest first. Close-quality
    signatures are NOT included — this is the pin-only view."""
    if len(df) == 0:
        return []
    start = max(0, len(df) - lookback)
    out: list[CandleSignature] = []
    for i in range(start, len(df)):
        sig = classify_candle(df, i)
        if sig.kind in ("bullish_pin", "bearish_pin"):
            out.append(sig)
    return out


def rejection_bias(df: pd.DataFrame, bars: int = 3) -> tuple[str, float]:
    """Net rejection direction over the last `bars` candles.

    Returns (direction, confidence) where direction is "bullish" | "bearish" |
    "neutral". Pin bars carry full weight, close-quality signatures half —
    a strong close is weaker evidence than an outright wick rejection.
    Confidence is the net score normalised by the number of bars read, so it
    stays in [0, 1] regardless of how many bars were requested.
    """
    if len(df) == 0 or bars <= 0:
        return "neutral", 0.0

    start = max(0, len(df) - bars)
    read = len(df) - start
    net = 0.0
    for i in range(start, len(df)):
        sig = classify_candle(df, i)
        if sig.kind == "bullish_pin":
            net += sig.strength
        elif sig.kind == "bearish_pin":
            net -= sig.strength
        elif sig.kind == "bullish_close":
            net += sig.strength * 0.5
        elif sig.kind == "bearish_close":
            net -= sig.strength * 0.5

    score = abs(net) / read if read else 0.0
    if net > 0:
        return "bullish", min(score, 1.0)
    if net < 0:
        return "bearish", min(score, 1.0)
    return "neutral", 0.0
