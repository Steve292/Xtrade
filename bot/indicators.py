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


# ---------------------------------------------------------------------------
# The four indicator concepts the corpus named that nothing here implemented.
# Small by support -- bollinger 1%, divergence 3%, adx 1%, stochastic 0% -- and
# added for completeness of the mapping, not because the evidence demands them.
# Recording that honestly matters: scripts/gate_split.py measured corpus breadth
# against real money and found it INVERSELY related to edge, so "the corpus
# mentions it" is a reason to model it, never a reason to trade it.
# ---------------------------------------------------------------------------


@dataclass
class BollingerResult:
    middle: pd.Series
    upper: pd.Series
    lower: pd.Series
    bandwidth: pd.Series   # (upper - lower) / middle — squeeze detection


def bollinger(series: pd.Series, period: int = 20,
              std_mult: float = 2.0) -> BollingerResult:
    """Bollinger Bands. Population std (ddof=0), matching the original."""
    mid = series.rolling(period, min_periods=period).mean()
    sd = series.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + std_mult * sd, mid - std_mult * sd
    return BollingerResult(mid, upper, lower, (upper - lower) / mid)


def stochastic(df: pd.DataFrame, k_period: int = 14,
               d_period: int = 3) -> tuple[pd.Series, pd.Series]:
    """Stochastic oscillator, returning (%K, %D).

    %K is NaN where the window's range is zero -- a flat window has no position
    within its range, and reporting 50 there would invent a reading.
    """
    low = df["low"].rolling(k_period, min_periods=k_period).min()
    high = df["high"].rolling(k_period, min_periods=k_period).max()
    rng = high - low
    k = 100 * (df["close"] - low) / rng.where(rng != 0)
    return k, k.rolling(d_period, min_periods=d_period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR. Exposed because adx() needs true range anyway."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX — trend STRENGTH, direction-blind by construction.

    Worth stating because it is routinely misread: a rising ADX says the move
    is committed, not which way it is going. Used here only to separate
    "trending" from "ranging", which is the distinction bot/smc/wyckoff.py
    needs and previously had to infer from swing geometry alone.
    """
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    a = atr(df, period)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False,
                                min_periods=period).mean() / a
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False,
                                  min_periods=period).mean() / a
    total = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / total.where(total != 0)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def divergence(price: pd.Series, oscillator: pd.Series,
               lookback: int = 30, pivot: int = 3) -> str:
    """Regular divergence between price and an oscillator.

    Returns "bullish" | "bearish" | "none".

    Compares the last two oscillator PIVOTS rather than the last two raw
    extremes. Raw extremes pick adjacent bars in a noisy series and report
    divergence on essentially every leg; a pivot has to be the extreme of its
    own neighbourhood, which is what makes the comparison mean anything.
    """
    if len(price) < lookback or len(oscillator) < lookback:
        return "none"
    p = price.iloc[-lookback:].reset_index(drop=True)
    o = oscillator.iloc[-lookback:].reset_index(drop=True)
    if o.isna().all():
        return "none"

    lows, highs = [], []
    for i in range(pivot, len(o) - pivot):
        win = o.iloc[i - pivot:i + pivot + 1]
        if win.isna().any():
            continue
        if o.iloc[i] == win.min():
            lows.append(i)
        if o.iloc[i] == win.max():
            highs.append(i)

    # bullish: price makes a lower low, the oscillator does not
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if p.iloc[b] < p.iloc[a] and o.iloc[b] > o.iloc[a]:
            return "bullish"
    # bearish: price makes a higher high, the oscillator does not
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if p.iloc[b] > p.iloc[a] and o.iloc[b] < o.iloc[a]:
            return "bearish"
    return "none"
