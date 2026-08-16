"""Tests for the multi-candle detectors: stars, soldiers/crows, tweezers.

Built from explicit OHLC geometry rather than fixtures, so a failure points at
the shape that broke. The negative cases matter more than the positive ones
here: these three feed confirms(), which the optional candle gate calls, and
every detector added there makes that gate EASIER to satisfy. A detector that
fires on noise would quietly loosen a live gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.smc.candles import (  # noqa: E402
    detect_candles,
    detect_star,
    detect_three_soldiers,
    detect_tweezer,
)


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


# --------------------------------------------------------------- stars


def _morning_star() -> pd.DataFrame:
    # big down candle, small pause, strong recovery closing deep into candle 1
    return _df([
        (110.0, 110.5, 99.5, 100.0),   # bearish body 10
        (99.8, 100.5, 98.5, 99.5),     # small body 0.3 on range 2.0
        (100.0, 109.0, 99.8, 108.0),   # bullish, closes 8 into the first body
    ])


def test_morning_star_detected():
    p = detect_star(_morning_star(), 2)
    assert p is not None, "morning star not detected"
    assert p.name == "morning_star" and p.direction == "bullish"
    assert 0.0 < p.strength <= 1.0


def test_evening_star_detected():
    df = _df([
        (100.0, 110.5, 99.5, 110.0),   # bullish body 10
        (110.2, 111.5, 109.5, 110.5),  # small body
        (110.0, 110.3, 101.0, 102.0),  # bearish, closes deep into the first
    ])
    p = detect_star(df, 2)
    assert p is not None and p.name == "evening_star"
    assert p.direction == "bearish"


def test_star_rejects_a_large_middle_candle():
    """The middle candle is the whole pattern -- a big one is just three candles.

    body/range must exceed max_star_body_pct (0.4) for the rejection to be
    testing what it claims. body 1.4 on range 3.0 = 0.47; everything else about
    the frame stays a valid morning star, so this isolates the one condition.
    """
    df = _morning_star()
    df.loc[1] = (99.8, 101.5, 98.5, 101.2)   # body 1.4 / range 3.0 = 0.47
    assert detect_star(df, 2) is None


def test_star_rejects_shallow_penetration():
    """Closing barely into the first body is not a reversal."""
    df = _morning_star()
    df.loc[2] = (100.0, 102.0, 99.8, 101.0)  # only ~1 of the 10 body
    assert detect_star(df, 2) is None


def test_star_needs_three_candles():
    assert detect_star(_morning_star().iloc[:2], 1) is None


# ------------------------------------------------- soldiers and crows


def _three_soldiers() -> pd.DataFrame:
    return _df([
        (100.0, 105.2, 99.8, 105.0),
        (103.0, 110.2, 102.8, 110.0),   # opens inside prev body, closes higher
        (108.0, 115.2, 107.8, 115.0),
    ])


def test_three_white_soldiers_detected():
    p = detect_three_soldiers(_three_soldiers(), 2)
    assert p is not None and p.name == "three_soldiers"
    assert p.direction == "bullish"


def test_three_black_crows_detected():
    df = _df([
        (115.0, 115.2, 109.8, 110.0),
        (112.0, 112.2, 104.8, 105.0),
        (107.0, 107.2, 99.8, 100.0),
    ])
    p = detect_three_soldiers(df, 2)
    assert p is not None and p.name == "three_crows"
    assert p.direction == "bearish"


def test_soldiers_reject_mixed_direction():
    df = _three_soldiers()
    df.loc[1] = (110.0, 110.5, 103.0, 104.0)   # middle candle turns bearish
    assert detect_three_soldiers(df, 2) is None


def test_soldiers_reject_gap_open_outside_previous_body():
    """A gap-open run is exhaustion more often than strength."""
    df = _three_soldiers()
    df.loc[2] = (112.0, 118.0, 111.8, 117.0)   # opens ABOVE prev body high
    assert detect_three_soldiers(df, 2) is None


def test_soldiers_reject_doji_bodies():
    df = _df([
        (100.0, 105.0, 99.0, 100.2),   # tiny body on a wide range
        (100.1, 106.0, 99.5, 100.3),
        (100.2, 107.0, 99.6, 100.4),
    ])
    assert detect_three_soldiers(df, 2) is None


# ------------------------------------------------------------ tweezers


def test_tweezer_top_detected():
    df = _df([
        (100.0, 110.0, 99.5, 109.0),   # bullish into 110
        (109.0, 110.0, 102.0, 103.0),  # bearish, same high
    ])
    p = detect_tweezer(df, 1)
    assert p is not None and p.name == "tweezer_top"
    assert p.direction == "bearish"


def test_tweezer_bottom_detected():
    df = _df([
        (110.0, 110.5, 100.0, 101.0),  # bearish into 100
        (101.0, 108.0, 100.0, 107.0),  # bullish, same low
    ])
    p = detect_tweezer(df, 1)
    assert p is not None and p.name == "tweezer_bottom"
    assert p.direction == "bullish"


def test_tweezer_rejects_same_direction_pair():
    df = _df([
        (100.0, 110.0, 99.5, 109.0),
        (109.0, 110.0, 108.0, 109.5),   # also bullish
    ])
    assert detect_tweezer(df, 1) is None


def test_tweezer_rejects_mismatched_extremes():
    df = _df([
        (100.0, 110.0, 99.5, 109.0),
        (109.0, 118.0, 102.0, 103.0),   # high nowhere near 110
    ])
    assert detect_tweezer(df, 1) is None


def test_tweezer_tolerance_is_fractional_not_absolute():
    """Same setting must work at 1.08 and at 60,000."""
    fx = _df([(1.0800, 1.0850, 1.0790, 1.0840),
              (1.0840, 1.08504, 1.0800, 1.0810)])
    btc = _df([(60000.0, 61000.0, 59900.0, 60900.0),
               (60900.0, 61000.4, 60100.0, 60200.0)])
    assert detect_tweezer(fx, 1) is not None, "failed on a 1.08-priced instrument"
    assert detect_tweezer(btc, 1) is not None, "failed on a 60,000-priced instrument"


# ------------------------------------------------------- integration


def test_new_detectors_are_reachable_from_detect_candles():
    names = {p.name for p in detect_candles(_morning_star(), lookback=3)}
    assert "morning_star" in names, f"detect_candles missed it: {names}"


def test_detectors_never_raise_on_short_or_flat_frames():
    flat = _df([(100.0, 100.0, 100.0, 100.0)] * 3)
    empty = _df([])
    for fn in (detect_star, detect_three_soldiers, detect_tweezer):
        for df in (flat, empty):
            i = max(0, len(df) - 1)
            assert fn(df, i) is None or isinstance(fn(df, i).strength, float)


def test_taxonomy_now_maps_star_pattern():
    from bot.knowledge import taxonomy as t
    sp = t.BY_KEY["star_pattern"]
    assert sp.maps_to == "bot.smc.candles", (
        "star_pattern must point at the module that now implements it"
    )


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
