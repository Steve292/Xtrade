"""
Classic TA indicators — EMA, RSI, MACD — backing Section 5's asset-specific
entry rules (bot/entry_rules.py). None of these existed anywhere in this
codebase before: the live SMC strategy (bot/smc/strategy.py) is pure
price-action/structure (swings, order blocks, FVGs, liquidity sweeps), it
never touches a classic indicator. These are plain pandas functions, no
venue/network dependency, fully unit-testable against hand-computed values.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard EMA (alpha = 2/(period+1))."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. NaN for the first `period` bars (not enough data), NaN
    on a perfectly flat run (0/0 — undefined, not a bug)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


@dataclass
class MACDResult:
    macd_line: pd.Series
    signal_line: pd.Series
    histogram: pd.Series


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> MACDResult:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return MACDResult(macd_line=macd_line, signal_line=signal_line, histogram=macd_line - signal_line)


def macd_bullish_crossover(result: MACDResult) -> bool:
    """True if the histogram just flipped from <=0 to >0 on the latest bar."""
    hist = result.histogram
    if len(hist) < 2:
        return False
    return bool(hist.iloc[-2] <= 0 and hist.iloc[-1] > 0)


def macd_bearish_crossover(result: MACDResult) -> bool:
    """True if the histogram just flipped from >=0 to <0 on the latest bar."""
    hist = result.histogram
    if len(hist) < 2:
        return False
    return bool(hist.iloc[-2] >= 0 and hist.iloc[-1] < 0)


def is_sloping_up(series: pd.Series, lookback: int = 3) -> bool:
    """True if the series' latest value is above its value `lookback` bars
    ago (used for "200 EMA sloping up" / "50 EMA with positive slope")."""
    if len(series) <= lookback or pd.isna(series.iloc[-1]) or pd.isna(series.iloc[-1 - lookback]):
        return False
    return bool(series.iloc[-1] > series.iloc[-1 - lookback])
