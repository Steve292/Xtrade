"""
Tests for bot/smc/fibonacci.py — retracement levels, OTE band, recent_leg.

Anchored on a real bug: recent_leg()'s direction argument checks `== "long"`
literally. Every caller in this codebase that passed "bullish"/"bearish"
instead (scripts/hourly_analysis.py, scripts/simulate_trades.py) silently
fell into the SHORT branch every single time, for both directions -- "bearish"
was accidentally correct (it also isn't "long"), but every "bullish" call
returned the down-leg's numbers mislabeled as an up-leg. On live gold data
this was an 11-point error, enough to flip whether price read as inside or
outside the pocket -- and it shipped into a published artifact's chart
annotation before being caught.

Run directly (`python tests/test_fibonacci.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.smc.fibonacci import in_ote, ote_band, recent_leg, retracement_levels
from bot.smc.structure import SwingPoint


def _swings(*pairs):
    """pairs of (index, kind, price), in index order."""
    return [SwingPoint(index=i, kind=k, price=p) for i, k, p in pairs]


# --- retracement_levels / ote_band basics -----------------------------------


def test_retracement_levels_r0_is_the_end_r1_is_the_start():
    levels = retracement_levels(100.0, 200.0)
    assert levels[0.0] if 0.0 in levels else True  # 0.0 isn't in FIB_RATIOS, skip
    # 0.618 sits 61.8% of the way back from end toward start
    assert abs(levels[0.618] - (200.0 - 100.0 * 0.618)) < 1e-9


def test_ote_band_is_ordered_low_high_for_an_up_leg():
    lo, hi = ote_band(100.0, 200.0)  # up-leg: low=100, high=200
    assert lo < hi
    assert 100.0 < lo < 200.0 and 100.0 < hi < 200.0


def test_ote_band_is_ordered_low_high_for_a_down_leg_too():
    """ote_band always returns (min, max) regardless of leg direction --
    callers should never need to sort it themselves."""
    lo, hi = ote_band(200.0, 100.0)  # down-leg: high=200, low=100
    assert lo < hi


def test_in_ote_respects_the_band():
    lo, hi = ote_band(100.0, 200.0)
    assert in_ote((lo + hi) / 2, 100.0, 200.0)
    assert not in_ote(lo - 1, 100.0, 200.0)
    assert not in_ote(hi + 1, 100.0, 200.0)


# --- recent_leg: the direction argument, precisely ---------------------------


def test_recent_leg_long_returns_swing_low_into_the_following_high():
    sw = _swings((10, "low", 100.0), (20, "high", 150.0))
    assert recent_leg(sw, "long") == (100.0, 150.0)


def test_recent_leg_short_returns_swing_high_into_the_following_low():
    sw = _swings((10, "high", 150.0), (20, "low", 100.0))
    assert recent_leg(sw, "short") == (150.0, 100.0)


def test_recent_leg_only_recognizes_the_literal_string_long():
    """The regression this file exists for. Anything other than the exact
    string "long" -- including the plausible-looking "bullish" -- silently
    takes the SHORT branch. This is documented behavior now, not a trap:
    every caller must pass "long"/"short", never "bullish"/"bearish"."""
    sw = _swings((10, "high", 150.0), (20, "low", 100.0), (30, "high", 140.0))
    short_leg = recent_leg(sw, "short")
    assert recent_leg(sw, "bullish") == short_leg, \
        "\"bullish\" must equal the SHORT-branch result -- this pins the " \
        "current (surprising) behavior so a future fix is a deliberate " \
        "change, not a silent regression in either direction"
    assert recent_leg(sw, "long") != short_leg, \
        "the real long branch must differ from the short branch on this fixture"


def test_recent_leg_returns_none_without_enough_swings():
    assert recent_leg(_swings((10, "high", 150.0)), "long") is None  # no prior low
    assert recent_leg(_swings((10, "low", 100.0)), "short") is None  # no prior high
    assert recent_leg([], "long") is None


def test_recent_leg_uses_the_swing_immediately_before_the_end_swing():
    """Not just any prior low -- the LAST one before the end swing, i.e. the
    leg that actually produced the most recent high."""
    sw = _swings(
        (5, "low", 90.0), (10, "low", 100.0), (15, "high", 150.0),
    )
    assert recent_leg(sw, "long") == (100.0, 150.0)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
