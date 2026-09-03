"""
Tests for bot/indicators.py — EMA, RSI, MACD. No network.

Run directly (`python tests/test_indicators.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.indicators import (
    ema,
    is_sloping_up,
    macd,
    macd_bearish_crossover,
    macd_bullish_crossover,
    rsi,
)


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


# --- ema -----------------------------------------------------------------


def test_ema_matches_hand_computed_recursion():
    # span=3 -> alpha=0.5, seeded at the first value.
    s = pd.Series([10.0, 11.0, 12.0, 11.0])
    result = ema(s, period=3)
    expected = [10.0, 10.5, 11.25, 11.125]
    for i, e in enumerate(expected):
        assert _close(result.iloc[i], e), f"row {i}: {result.iloc[i]} != {e}"


# --- rsi -------------------------------------------------------------------


def test_rsi_is_100_for_a_strictly_rising_series():
    s = pd.Series([float(i) for i in range(1, 30)])
    r = rsi(s, period=14)
    assert r.iloc[-1] == 100.0


def test_rsi_is_0_for_a_strictly_falling_series():
    s = pd.Series([float(30 - i) for i in range(30)])
    r = rsi(s, period=14)
    assert r.iloc[-1] == 0.0


def test_rsi_is_nan_for_a_flat_series():
    s = pd.Series([10.0] * 30)
    r = rsi(s, period=14)
    assert pd.isna(r.iloc[-1])


def test_rsi_is_nan_before_enough_bars():
    s = pd.Series([float(i) for i in range(1, 10)])  # only 9 bars, period=14
    r = rsi(s, period=14)
    assert pd.isna(r.iloc[-1])


def test_rsi_stays_within_0_and_100():
    s = pd.Series([10, 11, 10.5, 12, 11, 13, 12.5, 14, 13, 15, 14.5, 16, 15, 17, 16.5, 18])
    r = rsi(s, period=14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


# --- macd ------------------------------------------------------------------


def test_macd_line_is_fast_minus_slow_ema():
    s = pd.Series([float(i) for i in range(1, 60)])
    result = macd(s, fast=12, slow=26, signal=9)
    expected_macd_line = ema(s, 12) - ema(s, 26)
    for i in range(len(s)):
        assert _close(result.macd_line.iloc[i], expected_macd_line.iloc[i])


def test_macd_histogram_is_macd_minus_signal():
    s = pd.Series([float(i) for i in range(1, 60)])
    result = macd(s)
    for i in range(len(s)):
        assert _close(result.histogram.iloc[i], result.macd_line.iloc[i] - result.signal_line.iloc[i])


def _any_prefix_triggers(s: pd.Series, detector, **macd_kwargs) -> bool:
    """Feeds growing prefixes of `s` through macd() + `detector` — detector
    functions only ever look at the last two histogram values, so this
    checks whether the crossover fires at ANY point as the series unfolds."""
    for i in range(2, len(s) + 1):
        result = macd(s.iloc[:i], **macd_kwargs)
        if detector(result):
            return True
    return False


def test_macd_bullish_crossover_detected():
    # A sharp reversal from decline to climb should eventually cross bullish.
    s = pd.Series([50.0, 48, 46, 44, 42, 40, 42, 45, 50, 56, 63, 71])
    assert _any_prefix_triggers(s, macd_bullish_crossover, fast=3, slow=6, signal=3)


def test_macd_bearish_crossover_detected():
    s = pd.Series([40.0, 42, 44, 46, 48, 50, 48, 45, 40, 34, 27, 19])
    assert _any_prefix_triggers(s, macd_bearish_crossover, fast=3, slow=6, signal=3)


def test_macd_crossover_needs_at_least_two_bars():
    s = pd.Series([1.0])
    result = macd(s)
    assert not macd_bullish_crossover(result)
    assert not macd_bearish_crossover(result)


# --- is_sloping_up -------------------------------------------------------


def test_is_sloping_up_true_for_rising_series():
    assert is_sloping_up(pd.Series([1.0, 2, 3, 4, 5]), lookback=3)


def test_is_sloping_up_false_for_falling_series():
    assert not is_sloping_up(pd.Series([5.0, 4, 3, 2, 1]), lookback=3)


def test_is_sloping_up_false_with_too_few_bars():
    assert not is_sloping_up(pd.Series([1.0, 2.0]), lookback=3)


def test_is_sloping_up_handles_nan():
    assert not is_sloping_up(pd.Series([float("nan"), 2.0, 3.0, 4.0]), lookback=3)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
