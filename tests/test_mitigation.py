"""
Tests for bot/smc/mitigation.py — origin zones of structure-breaking moves.
No network.

Run directly (`python tests/test_mitigation.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from bot.smc.mitigation import (
    MitigationBlock,
    detect_mitigation_blocks,
    nearest_unmitigated,
    price_in_mitigation_block,
)
from bot.smc.structure import detect_structure_breaks, find_swing_points


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _bar(o, h, l, c, v=100.0):
    return [o, h, l, c, v]


def _walk(seed: int, n: int = 180) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        c = price + rng.normal(0, 1.5)
        h = max(o, c) + abs(rng.normal(0, 0.9))
        l = min(o, c) - abs(rng.normal(0, 0.9))
        rows.append(_bar(o, h, l, c))
        price = c
    return _df(rows)


# --- anchoring to structure breaks ------------------------------------------


def test_no_structure_breaks_means_no_blocks():
    """The defining property: this detector is anchored on BOS/CHoCH, not on
    move size. A frame with no structure events yields nothing regardless of
    how the price moved."""
    df = _walk(1)
    assert detect_mitigation_blocks(df, events=[]) == []


def test_each_block_maps_to_a_real_structure_event():
    df = _walk(2)
    swings = find_swing_points(df)
    events = detect_structure_breaks(df, swings)
    blocks = detect_mitigation_blocks(df, swings, events)
    event_indices = {e.index for e in events}
    for b in blocks:
        assert b.event_index in event_indices
        assert b.event_kind in ("bos", "choch")


def test_origin_candle_precedes_or_equals_the_break():
    for seed in range(6):
        df = _walk(seed)
        for b in detect_mitigation_blocks(df):
            assert b.index <= b.event_index


def test_origin_stays_within_the_distance_bound():
    for seed in range(6):
        df = _walk(seed)
        for b in detect_mitigation_blocks(df, max_origin_distance=5):
            assert b.event_index - b.index <= 5


def test_origin_candle_opposes_the_break_direction():
    """A bullish break's origin must be a DOWN candle (and vice versa) — that
    is the candle whose unfilled orders a retrace comes back to mitigate."""
    for seed in range(6):
        df = _walk(seed)
        opens, closes = df["open"].values, df["close"].values
        for b in detect_mitigation_blocks(df):
            is_down = closes[b.index] < opens[b.index]
            assert is_down == (b.direction == "bullish")


# --- precomputed inputs are honoured ----------------------------------------


def test_passing_precomputed_swings_and_events_matches_deriving_them():
    df = _walk(4)
    swings = find_swing_points(df)
    events = detect_structure_breaks(df, swings)
    a = detect_mitigation_blocks(df, swings, events)
    b = detect_mitigation_blocks(df)
    assert [(x.index, x.direction, x.event_index) for x in a] == \
           [(x.index, x.direction, x.event_index) for x in b]


# --- mitigation state -------------------------------------------------------


def test_unmitigated_lookup_skips_blocks_price_already_traded_back_through():
    zone = dict(top=105.0, bottom=100.0, event_index=5, event_kind="bos")
    filled = MitigationBlock(index=1, direction="bullish", mitigated=True, **zone)
    open_ = MitigationBlock(index=1, direction="bullish", mitigated=False, **zone)

    assert nearest_unmitigated(102.0, [filled], "bullish") is None
    assert nearest_unmitigated(102.0, [open_], "bullish") is open_


def test_unmitigated_lookup_respects_direction_and_containment():
    b = MitigationBlock(index=1, direction="bullish", top=105.0, bottom=100.0,
                        event_index=5, event_kind="bos", mitigated=False)
    assert nearest_unmitigated(102.0, [b], "bearish") is None
    assert nearest_unmitigated(99.0, [b], "bullish") is None


def test_price_in_block_is_inclusive_of_both_edges():
    b = MitigationBlock(index=0, direction="bullish", top=105.0, bottom=100.0,
                        event_index=1, event_kind="bos", mitigated=False)
    assert price_in_mitigation_block(100.0, b) and price_in_mitigation_block(105.0, b)
    assert not price_in_mitigation_block(99.99, b)


# --- degenerate input -------------------------------------------------------


def test_empty_frame_yields_no_blocks():
    assert detect_mitigation_blocks(_df([])) == []


def test_flat_frame_does_not_raise():
    assert detect_mitigation_blocks(_df([_bar(100, 100, 100, 100)] * 60)) == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
