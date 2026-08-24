"""
Tests for bot/smc/candles.py — wick / pin-bar / close-quality signatures.
No network: every case is a hand-built OHLCV frame.

Run directly (`python tests/test_candles.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc.candles import classify_candle, detect_pin_bars, rejection_bias


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _bar(o, h, l, c, v=100.0):
    return [o, h, l, c, v]


# --- pin bars ---------------------------------------------------------------


def test_hammer_is_a_bullish_pin():
    # Long lower wick, small body near the top, negligible upper wick.
    sig = classify_candle(_df([_bar(100, 101, 90, 100.5)]))
    assert sig.kind == "bullish_pin"
    assert sig.lower_wick_pct > sig.upper_wick_pct
    assert sig.strength > 0


def test_shooting_star_is_a_bearish_pin():
    sig = classify_candle(_df([_bar(100, 110, 99, 99.5)]))
    assert sig.kind == "bearish_pin"
    assert sig.upper_wick_pct > sig.lower_wick_pct


def test_two_long_wicks_is_indecision_not_a_pin():
    # A doji with wicks both sides is NOT a rejection of either direction —
    # the opposing-wick guard is what keeps it out of the pin classification.
    sig = classify_candle(_df([_bar(100, 110, 90, 100.2)]))
    assert sig.kind not in ("bullish_pin", "bearish_pin")


# --- close quality ----------------------------------------------------------


def test_close_at_top_of_range_is_a_bullish_close():
    sig = classify_candle(_df([_bar(100, 110, 100, 109.8)]))
    assert sig.kind == "bullish_close"
    assert sig.close_position > 0.9


def test_close_at_bottom_of_range_is_a_bearish_close():
    sig = classify_candle(_df([_bar(110, 110, 100, 100.2)]))
    assert sig.kind == "bearish_close"
    assert sig.close_position < 0.1


def test_close_mid_range_is_neutral_with_zero_strength():
    sig = classify_candle(_df([_bar(100, 110, 90, 100.0)]))
    assert sig.kind == "neutral"
    assert sig.strength == 0.0


# --- degenerate input -------------------------------------------------------


def test_zero_range_bar_is_neutral_not_a_division_error():
    sig = classify_candle(_df([_bar(50, 50, 50, 50)]))
    assert sig.kind == "neutral"
    assert sig.strength == 0.0


def test_empty_frame_reads_neutral():
    assert detect_pin_bars(_df([])) == []
    assert rejection_bias(_df([])) == ("neutral", 0.0)


# --- aggregate bias ---------------------------------------------------------


def test_consecutive_hammers_read_bullish():
    df = _df([_bar(100, 101, 90, 100.5)] * 3)
    direction, score = rejection_bias(df, bars=3)
    assert direction == "bullish"
    assert 0.0 < score <= 1.0


def test_consecutive_shooting_stars_read_bearish():
    df = _df([_bar(100, 110, 99, 99.5)] * 3)
    direction, _ = rejection_bias(df, bars=3)
    assert direction == "bearish"


def test_bias_score_stays_normalised_regardless_of_bars_read():
    # Score is per-bar, so asking for more bars must not inflate it past 1.0.
    df = _df([_bar(100, 101, 90, 100.5)] * 50)
    for n in (1, 5, 25, 50):
        _, score = rejection_bias(df, bars=n)
        assert 0.0 <= score <= 1.0


def test_opposing_pins_cancel_toward_neutral():
    df = _df([_bar(100, 101, 90, 100.5), _bar(100, 110, 99, 99.5)])
    _, score = rejection_bias(df, bars=2)
    _, solo = rejection_bias(_df([_bar(100, 101, 90, 100.5)]), bars=1)
    assert score < solo


def test_pin_detection_respects_the_lookback_window():
    df = _df([_bar(100, 101, 90, 100.5)] + [_bar(100, 110, 90, 100.0)] * 30)
    assert detect_pin_bars(df, lookback=5) == []
    assert len(detect_pin_bars(df, lookback=40)) == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
