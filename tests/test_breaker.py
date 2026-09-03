"""
Tests for bot/smc/breaker.py — order blocks that failed and flipped polarity.
No network.

Run directly (`python tests/test_breaker.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc.breaker import BreakerBlock, active_breaker, detect_breakers, price_in_breaker
from bot.smc.order_blocks import detect_order_blocks, detect_raw_order_blocks


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _bar(o, h, l, c, v=100.0):
    return [o, h, l, c, v]


def _bullish_ob_then_break_then_retest() -> pd.DataFrame:
    """A down candle, a strong rally off it (making it a bullish OB), then a
    close back below its low (the break), then a return into the zone."""
    rows = [_bar(100, 101, 99, 100) for _ in range(3)]
    rows.append(_bar(100, 100.5, 98, 98.5))          # the OB candle (bearish)
    rows += [_bar(98.5, 104, 98.4, 103.5), _bar(103.5, 108, 103, 107.5)]  # impulse
    rows += [_bar(107.5, 108, 104, 104.5), _bar(104.5, 105, 97, 97.2)]    # break below 98
    rows += [_bar(97.2, 99.5, 97, 99.0)]             # retest back into 98–100.5
    rows += [_bar(99, 99.5, 98.5, 99.2) for _ in range(3)]
    return _df(rows)


# --- the raw/filtered split the module depends on ---------------------------


def test_raw_detection_is_a_superset_of_the_filtered_view():
    df = _bullish_ob_then_break_then_retest()
    raw = detect_raw_order_blocks(df)
    filtered = detect_order_blocks(df)
    assert len(raw) >= len(filtered)


# --- polarity flip ----------------------------------------------------------


def test_broken_bullish_order_block_becomes_a_bearish_breaker():
    df = _bullish_ob_then_break_then_retest()
    breakers = detect_breakers(df)
    assert breakers, "expected a breaker from a broken bullish order block"
    assert all(b.direction == "bearish" for b in breakers), (
        "a broken BULLISH block must flip to act BEARISH — the flip is the "
        "whole point of a breaker"
    )


def test_break_is_recorded_after_the_block_that_formed_it():
    df = _bullish_ob_then_break_then_retest()
    for b in detect_breakers(df):
        assert b.break_index > b.index


# --- a wick through is not a break ------------------------------------------


def test_every_breaker_was_broken_by_a_close_not_a_wick():
    """Only a CLOSE through the far side breaks a block; wicks through are
    noise. Asserted as an invariant over random walks rather than a hand-built
    frame — a frame tuned to isolate one block tends to create a second one
    somewhere else, and then the test is measuring the wrong thing.

    The returned direction is already FLIPPED, so the original block's side is
    recovered by flipping it back before checking the break condition.
    """
    import numpy as np

    rng = np.random.default_rng(3)
    checked = 0
    for _ in range(60):
        price, rows = 100.0, []
        for _ in range(150):
            o = price
            c = price + rng.normal(0, 1.5)
            h = max(o, c) + abs(rng.normal(0, 1.0))
            l = min(o, c) - abs(rng.normal(0, 1.0))
            rows.append(_bar(o, h, l, c))
            price = c
        df = _df(rows)
        closes = df["close"].values
        for b in detect_breakers(df):
            original = "bullish" if b.direction == "bearish" else "bearish"
            close_at_break = closes[b.break_index]
            if original == "bullish":
                assert close_at_break < b.bottom, "bullish block broken without a close below it"
            else:
                assert close_at_break > b.top, "bearish block broken without a close above it"
            checked += 1

    assert checked > 0, "sample produced no breakers — the invariant went untested"


# --- retest requirement -----------------------------------------------------


def test_active_breaker_ignores_one_that_was_never_retested():
    never = BreakerBlock(index=1, direction="bearish", top=105.0, bottom=100.0,
                         break_index=5, retested=False)
    assert active_breaker(102.0, [never], "bearish") is None

    retested = BreakerBlock(index=1, direction="bearish", top=105.0, bottom=100.0,
                            break_index=5, retested=True)
    assert active_breaker(102.0, [retested], "bearish") is retested


def test_active_breaker_requires_price_inside_the_zone():
    b = BreakerBlock(index=1, direction="bearish", top=105.0, bottom=100.0,
                     break_index=5, retested=True)
    assert active_breaker(150.0, [b], "bearish") is None
    assert active_breaker(102.0, [b], "bullish") is None  # wrong direction


def test_price_in_breaker_is_inclusive_of_both_edges():
    b = BreakerBlock(index=0, direction="bullish", top=105.0, bottom=100.0,
                     break_index=1, retested=True)
    assert price_in_breaker(100.0, b) and price_in_breaker(105.0, b)
    assert not price_in_breaker(99.99, b) and not price_in_breaker(105.01, b)


# --- degenerate input -------------------------------------------------------


def test_empty_and_flat_frames_yield_no_breakers():
    assert detect_breakers(_df([])) == []
    assert detect_breakers(_df([_bar(100, 100, 100, 100)] * 40)) == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
