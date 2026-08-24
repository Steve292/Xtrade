"""
Tests for bot/smc/volume_profile.py — POC, value area, and VWAP.
No network.

Run directly (`python tests/test_volume_profile.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from bot.smc.volume_profile import build_volume_profile, vwap, vwap_bias


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _bar(o, h, l, c, v=100.0):
    return [o, h, l, c, v]


# --- point of control -------------------------------------------------------


def test_poc_lands_where_the_volume_concentrated():
    """Most bars trade quietly around 100; a handful trade heavily near 120.
    The POC must follow the VOLUME, not the price range."""
    rows = [_bar(99, 101, 99, 100, v=1.0) for _ in range(40)]
    rows += [_bar(119, 121, 119, 120, v=500.0) for _ in range(5)]
    profile = build_volume_profile(_df(rows), bins=20)
    assert profile is not None
    assert 118 <= profile.poc <= 122, f"POC {profile.poc} did not follow the heavy volume"


def test_value_area_contains_the_poc():
    rows = [_bar(99, 101, 99, 100, v=10.0) for _ in range(50)]
    rows += [_bar(104, 106, 104, 105, v=3.0) for _ in range(20)]
    profile = build_volume_profile(_df(rows))
    assert profile.value_area_low <= profile.poc <= profile.value_area_high


def test_value_area_captures_at_least_the_requested_share():
    rng = np.random.default_rng(5)
    rows = []
    price = 100.0
    for _ in range(200):
        o = price
        c = price + rng.normal(0, 1.2)
        h = max(o, c) + abs(rng.normal(0, 0.6))
        l = min(o, c) - abs(rng.normal(0, 0.6))
        rows.append(_bar(o, h, l, c, v=abs(rng.normal(500, 120))))
        price = c
    profile = build_volume_profile(_df(rows), bins=24, value_area_pct=0.70)

    inside = sum(
        v for lo, v in zip(profile.bin_edges, profile.bin_volumes)
        if profile.value_area_low <= lo < profile.value_area_high
    )
    # Expansion stops on the first bin that reaches the target, so the share
    # meets or slightly exceeds 70% — it must never fall short.
    assert inside / profile.total_volume >= 0.70


def test_in_value_area_boundary_is_inclusive():
    rows = [_bar(99, 101, 99, 100) for _ in range(30)]
    profile = build_volume_profile(_df(rows))
    assert profile.in_value_area(profile.value_area_low)
    assert profile.in_value_area(profile.value_area_high)
    assert not profile.in_value_area(profile.value_area_high + 1)


def test_wide_bars_spread_volume_across_every_bin_they_span():
    """A single very wide bar must not dump all its volume into one bin —
    that would place volume where trading demonstrably did not happen."""
    profile = build_volume_profile(_df([_bar(100, 200, 100, 150, v=1000.0)]), bins=10)
    assert profile is not None
    touched = sum(1 for v in profile.bin_volumes if v > 0)
    assert touched > 1, "wide bar's volume collapsed into a single bin"
    assert abs(sum(profile.bin_volumes) - 1000.0) < 1e-6, "volume was not conserved"


# --- degenerate input returns None, never a fabricated number ---------------


def test_no_read_rather_than_a_meaningless_one():
    assert build_volume_profile(_df([])) is None
    assert build_volume_profile(_df([_bar(5, 5, 5, 5, v=10.0)] * 30)) is None, \
        "a flat range has no meaningful POC"
    assert build_volume_profile(_df([_bar(99, 101, 99, 100, v=0.0)] * 30)) is None, \
        "zero volume has no meaningful profile"


def test_missing_volume_column_yields_no_read():
    df = pd.DataFrame([[99, 101, 99, 100]] * 30, columns=["open", "high", "low", "close"])
    assert build_volume_profile(df) is None
    assert vwap(df) is None


# --- VWAP -------------------------------------------------------------------


def test_vwap_is_pulled_toward_the_heavily_traded_price():
    light = _df([_bar(99, 101, 99, 100, v=1.0)] * 10)
    heavy = _df([_bar(99, 101, 99, 100, v=1.0)] * 10 + [_bar(149, 151, 149, 150, v=900.0)])
    assert vwap(heavy) > vwap(light)


def test_vwap_of_a_constant_price_is_that_price():
    assert abs(vwap(_df([_bar(100, 100, 100, 100, v=7.0)] * 20)) - 100.0) < 1e-9


def test_vwap_respects_the_lookback_window():
    rows = [_bar(199, 201, 199, 200, v=100.0)] * 20 + [_bar(99, 101, 99, 100, v=100.0)] * 10
    assert abs(vwap(_df(rows), lookback=10) - 100.0) < 1e-6


def test_vwap_none_inputs_are_neutral_not_directional():
    assert vwap(_df([])) is None
    assert vwap_bias(100.0, None) == "neutral"
    assert vwap_bias(101.0, 100.0) == "bullish"
    assert vwap_bias(99.0, 100.0) == "bearish"
    assert vwap_bias(100.0, 100.0) == "neutral"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
