"""Tests for bot/smc/candles.py — the candlestick entry-trigger layer.

No network. Run directly (`python tests/test_smc_candles.py`) or under pytest.

Candles are constructed by hand rather than sampled from market data, because
the point of each test is one specific geometric relationship (body engulfs
body, wick is 2x body, range sits inside the prior range). A real OHLC sample
would make it unclear which property is actually being asserted.

The direction tests matter most. A pin bar's signal is the OPPOSITE of its
wick -- a long LOWER wick is BULLISH, because sellers were rejected -- and a
detector that got that backwards would place entries on the wrong side of every
rejection it found.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc import candles


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_bullish_engulfing():
    # small down candle, then a bigger up candle swallowing its body
    df = _df([(100, 101, 99, 99.5), (99.0, 102, 98.5, 101.5)])
    p = candles.detect_engulfing(df, 1)
    assert p is not None and p.direction == "bullish", p


def test_bearish_engulfing():
    df = _df([(100, 101, 99.5, 100.8), (101.0, 101.2, 98, 99.0)])
    p = candles.detect_engulfing(df, 1)
    assert p is not None and p.direction == "bearish", p


def test_engulfing_requires_a_direction_change():
    # two up candles: the second is bigger but it is not a reversal
    df = _df([(100, 101, 99.5, 100.5), (99.0, 103, 98.9, 102.0)])
    assert candles.detect_engulfing(df, 1) is None


def test_pin_bar_direction_is_opposite_the_wick():
    # long LOWER wick, small body near the top -> sellers rejected -> BULLISH
    df = _df([(100, 100.5, 100, 100.2), (100, 100.6, 96.0, 100.3)])
    p = candles.detect_pin_bar(df, 1)
    assert p is not None and p.direction == "bullish", p

    # long UPPER wick -> buyers rejected -> BEARISH
    df2 = _df([(100, 100.5, 100, 100.2), (100, 104.0, 99.7, 100.1)])
    p2 = candles.detect_pin_bar(df2, 1)
    assert p2 is not None and p2.direction == "bearish", p2


def test_fat_bodied_candle_is_not_a_pin_bar():
    df = _df([(100, 100.5, 100, 100.2), (96, 104, 95.5, 103.5)])
    assert candles.detect_pin_bar(df, 1) is None


def test_doji_is_neutral():
    df = _df([(100, 101, 99, 100.02)])
    p = candles.detect_doji(df, 0)
    assert p is not None and p.direction == "neutral"


def test_inside_and_outside_bars():
    inside = _df([(100, 105, 95, 101), (100, 103, 97, 99)])
    p = candles.detect_inside_bar(inside, 1)
    assert p is not None and p.name == "inside_bar"

    outside = _df([(100, 103, 97, 99), (98, 106, 94, 105)])
    q = candles.detect_outside_bar(outside, 1)
    assert q is not None and q.direction == "bullish"


def test_marubozu_needs_almost_no_wick():
    df = _df([(100, 110.05, 99.98, 110.0)])
    p = candles.detect_marubozu(df, 0)
    assert p is not None and p.direction == "bullish"
    assert candles.detect_marubozu(_df([(100, 120, 90, 105)]), 0) is None


def test_confirms_ignores_neutral_patterns():
    # A doji is indecision. Treating it as confirmation would make the gate
    # weaker than having no gate at all.
    doji = _df([(100, 101, 99, 100.01), (100, 101, 99, 100.01)])
    assert candles.confirms(doji, "long") is None


def test_confirms_matches_direction():
    df = _df([(100, 101, 99, 99.5), (99.0, 102, 98.5, 101.5)])
    assert candles.confirms(df, "long") is not None
    assert candles.confirms(df, "short") is None


def test_closed_beyond_distinguishes_wick_from_close():
    # wick pierces 105 but the candle closes back below: NOT acceptance
    wick_through = _df([(100, 106, 99, 104)])
    assert candles.closed_beyond(wick_through, 105.0, "long") is False
    # closes above: acceptance
    closed = _df([(100, 106, 99, 105.5)])
    assert candles.closed_beyond(closed, 105.0, "long") is True


def test_detectors_never_raise_on_degenerate_input():
    flat = _df([(100, 100, 100, 100), (100, 100, 100, 100)])
    for fn in (candles.detect_engulfing, candles.detect_pin_bar,
               candles.detect_doji, candles.detect_inside_bar,
               candles.detect_outside_bar, candles.detect_marubozu):
        fn(flat, 1)
    assert candles.detect_candles(_df([])) == []
    assert candles.closed_beyond(_df([]), 1.0, "long") is False


def _run_all() -> bool:
    ok = True
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                ok = False
                print(f"  FAIL {name}: {exc}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
