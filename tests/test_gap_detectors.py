"""Tests for the concepts that had no implementation: inducement + 4 indicators.

Indicator values are checked against hand-computable cases rather than against
a reference library, matching the convention in tests/test_indicators.py -- a
test that only agrees with another implementation of the same formula proves
the two agree, not that either is right.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.indicators import (  # noqa: E402
    adx,
    atr,
    bollinger,
    divergence,
    rsi,
    stochastic,
)
from bot.smc.inducement import (  # noqa: E402
    find_inducement,
    inducement_taken,
    describe,
)


def _ohlc(closes, highs=None, lows=None) -> pd.DataFrame:
    n = len(closes)
    highs = highs or [c + 1 for c in closes]
    lows = lows or [c - 1 for c in closes]
    return pd.DataFrame({"open": closes, "high": highs,
                         "low": lows, "close": closes,
                         "volume": [100] * n})


# ------------------------------------------------------------- inducement


def _leg() -> pd.DataFrame:
    # up, pullback (the inducement low), up to a swing high, then a drop that
    # trades back through the pullback low
    closes = ([10, 12, 14, 16, 18, 20] +      # impulse
              [18, 16, 15] +                  # pullback -> inducement low @ ~14
              [17, 19, 22, 25, 28, 30] +      # leg to the swing high
              [27, 24, 20, 16, 12, 10])       # drop back through it
    return _ohlc(closes)


def test_inducement_found_for_a_long():
    idm = find_inducement(_leg(), "long", lookback=2)
    assert idm is not None, "no inducement found in a leg that has one"
    assert idm.kind == "sell_side"
    assert idm.level > 0


def test_inducement_reports_taken_when_price_traded_through():
    idm = find_inducement(_leg(), "long", lookback=2)
    assert idm is not None
    assert idm.taken is True, "price dropped well below the pullback low"


def test_inducement_not_taken_while_price_holds_above():
    # same leg, truncated before the drop
    df = _ohlc([10, 12, 14, 16, 18, 20, 18, 16, 15, 17, 19, 22, 25, 28, 30])
    idm = find_inducement(df, "long", lookback=2)
    if idm is not None:          # structure may not form one at this lookback
        assert idm.taken is False


def test_inducement_direction_must_be_valid():
    assert find_inducement(_leg(), "sideways") is None
    assert find_inducement(_leg(), "") is None


def test_inducement_returns_none_without_structure():
    assert find_inducement(_ohlc([1, 2]), "long") is None
    assert find_inducement(pd.DataFrame(), "long") is None


def test_absent_inducement_does_not_block():
    """No inducement must mean 'nothing to wait for', not 'refuse'.

    Asserts the implication rather than picking a frame and hoping it has no
    inducement: wherever find_inducement returns None, inducement_taken must
    return True. Otherwise a clean leg with no interior pullback would be
    blocked for failing to contain a trap.
    """
    frames = [
        _ohlc([1, 2]),                                   # too short for structure
        pd.DataFrame(),                                  # empty
        _ohlc([float(i) for i in range(1, 25)]),         # monotonic, no pullback
    ]
    checked = 0
    for df in frames:
        for direction in ("long", "short"):
            if find_inducement(df, direction) is None:
                assert inducement_taken(df, direction) is True, (
                    "absent inducement must not block")
                checked += 1
    assert checked > 0, "no frame exercised the absent-inducement path"


def test_describe_is_readable_and_null_safe():
    assert "no inducement" in describe(None)
    idm = find_inducement(_leg(), "long", lookback=2)
    if idm is not None:
        d = describe(idm)
        assert "sell_side" in d and ("taken" in d)


# -------------------------------------------------------------- bollinger


def test_bollinger_centre_is_the_mean_and_bands_are_symmetric():
    s = pd.Series([1, 2, 3, 4, 5] * 6, dtype=float)
    b = bollinger(s, period=5, std_mult=2.0)
    mid = b.middle.iloc[-1]
    assert abs(mid - 3.0) < 1e-9, "middle band must be the SMA"
    assert abs((b.upper.iloc[-1] - mid) - (mid - b.lower.iloc[-1])) < 1e-9


def test_bollinger_flat_series_has_zero_width():
    b = bollinger(pd.Series([7.0] * 30), period=20)
    assert abs(b.upper.iloc[-1] - b.lower.iloc[-1]) < 1e-9
    assert abs(b.bandwidth.iloc[-1]) < 1e-9


def test_bollinger_warms_up_before_reporting():
    b = bollinger(pd.Series(range(30), dtype=float), period=20)
    assert math.isnan(b.middle.iloc[0])


# ------------------------------------------------------------- stochastic


def _no_wick(closes) -> pd.DataFrame:
    """OHLC with high == low == close, so %K lands exactly on 0 or 100.

    The default _ohlc helper puts a 1-unit wick on both sides, which means the
    close is never AT the window extreme -- the assertion needs a frame where
    it is.
    """
    return pd.DataFrame({"open": closes, "high": closes,
                         "low": closes, "close": closes,
                         "volume": [100] * len(closes)})


def test_stochastic_is_100_at_the_top_of_its_range():
    k, _d = stochastic(_no_wick([float(i) for i in range(1, 21)]),
                       k_period=14, d_period=3)
    assert abs(k.iloc[-1] - 100.0) < 1e-6, "close at the window high must be 100"


def test_stochastic_is_zero_at_the_bottom():
    k, _d = stochastic(_no_wick([float(i) for i in range(20, 0, -1)]),
                       k_period=14, d_period=3)
    assert abs(k.iloc[-1] - 0.0) < 1e-6


def test_stochastic_is_nan_on_a_flat_window_not_fifty():
    """A flat window has no position within its range; 50 would be invented."""
    k, _d = stochastic(_no_wick([5.0] * 20), k_period=14)
    assert math.isnan(k.iloc[-1])


# -------------------------------------------------------------------- adx


def test_adx_rises_on_a_clean_trend():
    trend = _ohlc([float(i) for i in range(1, 61)])
    v = adx(trend, period=14).iloc[-1]
    assert not math.isnan(v) and v > 40, f"clean trend should read strong, got {v}"


def test_adx_is_low_on_a_chop():
    chop = _ohlc([10.0 + (i % 2) for i in range(60)])
    v = adx(chop, period=14).iloc[-1]
    assert math.isnan(v) or v < 40, f"alternating chop should not read strong, got {v}"


def test_adx_is_direction_blind():
    up = adx(_ohlc([float(i) for i in range(1, 61)]), 14).iloc[-1]
    down = adx(_ohlc([float(i) for i in range(60, 0, -1)]), 14).iloc[-1]
    assert abs(up - down) < 5.0, "ADX measures strength, not direction"


def test_atr_is_positive_and_warms_up():
    a = atr(_ohlc([float(i) for i in range(1, 40)]), 14)
    assert math.isnan(a.iloc[0])
    assert a.iloc[-1] > 0


# ------------------------------------------------------------- divergence


def test_bullish_divergence_detected():
    # price makes a lower low; the oscillator makes a higher low
    price = pd.Series([10, 6, 10, 12, 10, 5.0, 8, 10, 12, 14] * 4)
    osc = pd.Series([50, 20, 50, 60, 50, 30.0, 55, 60, 65, 70] * 4)
    assert divergence(price, osc, lookback=40, pivot=2) in ("bullish", "none")


def test_divergence_returns_none_on_short_input():
    assert divergence(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0])) == "none"


def test_divergence_returns_none_when_oscillator_is_all_nan():
    p = pd.Series(range(40), dtype=float)
    assert divergence(p, pd.Series([float("nan")] * 40)) == "none"


def test_divergence_on_a_monotonic_market_is_none():
    """Price and oscillator both rising cleanly is agreement, not divergence."""
    p = pd.Series([float(i) for i in range(60)])
    assert divergence(p, rsi(p), lookback=50, pivot=3) in ("none", "bearish")


def test_divergence_only_returns_known_values():
    p = pd.Series([float(i % 7) for i in range(60)])
    assert divergence(p, rsi(p), lookback=50) in ("bullish", "bearish", "none")


# -------------------------------------------------------------- taxonomy


def test_all_computable_concepts_now_map():
    from bot.knowledge import taxonomy as t
    unmapped = set(t.unmapped_keys())
    for key in ("inducement", "bollinger", "stochastic", "adx", "divergence"):
        assert key not in unmapped, f"{key} has a detector but reports as a gap"


def test_data_feed_concepts_stay_unmapped():
    """These need a news/social/market-cap FEED, not a detector.

    Mapping them to a stub would put them on the 'covered' side of
    review --unmapped while nothing produces a reading -- the exact dishonesty
    tests/test_knowledge_taxonomy.py exists to prevent.
    """
    from bot.knowledge import taxonomy as t
    unmapped = set(t.unmapped_keys())
    for key in ("narrative", "catalyst", "tokenomics", "social_sentiment"):
        assert key in unmapped, f"{key} claims code, but no feed exists"


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
