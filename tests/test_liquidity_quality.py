"""
Tests for the liquidity-pool quality flags: merging, reclaim, and unswept
visibility (bot/smc/liquidity.py), plus the HTF sweep gate in
bot/screening.py.

All three default OFF; the first test pins that. No network.

Run directly (`python tests/test_liquidity_quality.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.screening import ScreenConfig, TradeScreener
from bot.smc.liquidity import detect_liquidity_pools, recent_sweep
from bot.smc.strategy import Signal, SignalType
from bot.smc.structure import Trend


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _bar(o, h, l, c, v=100.0):
    return [o, h, l, c, v]


# --- defaults ---------------------------------------------------------------


def test_all_quality_flags_default_off():
    cfg = ScreenConfig()
    assert cfg.htf_sweep is False
    assert cfg.merge_pools is False
    assert cfg.require_reclaim is False


# --- merging duplicate levels -----------------------------------------------


def _four_equal_lows() -> pd.DataFrame:
    """One level touched four times, then broken. Unmerged this emits a pool
    per PAIR of touches (6 pairs); merged it is one level with 4 touches."""
    rows = [_bar(101, 102, 100, 101) for _ in range(4)]
    rows += [_bar(101, 103, 100.0, 102)]
    rows += [_bar(102, 103, 95, 96)]           # break below
    rows += [_bar(96, 97, 95, 96) for _ in range(4)]
    return _df(rows)


def test_merging_collapses_repeated_touches_of_one_level():
    df = _four_equal_lows()
    unmerged = detect_liquidity_pools(df, 0.005, merge=False)
    merged = detect_liquidity_pools(df, 0.005, merge=True)
    assert len(merged) < len(unmerged), (
        f"merging did not reduce {len(unmerged)} pools"
    )
    assert any(p.touches > 2 for p in merged), "touch count was not accumulated"


def test_merged_pool_keeps_the_most_recent_index():
    """Recency checks key on the index, so a merged level must carry the
    LATEST touch, not the first."""
    df = _four_equal_lows()
    for p in detect_liquidity_pools(df, 0.005, merge=True):
        same = [q for q in detect_liquidity_pools(df, 0.005) if q.kind == p.kind]
        if same:
            assert p.index >= min(q.index for q in same)


# --- sweep vs break ---------------------------------------------------------


def test_a_level_price_closed_through_is_swept_but_not_reclaimed():
    """Price breaks below and stays. That is a break, not a stop-hunt."""
    rows = [_bar(101, 102, 100, 101) for _ in range(3)]
    rows += [_bar(101, 101, 100, 100.5)]
    rows += [_bar(100, 101, 94, 95)]                       # closes below
    rows += [_bar(95, 96, 94, 95) for _ in range(4)]       # stays below
    pools = detect_liquidity_pools(_df(rows), 0.005)
    sells = [p for p in pools if p.kind == "sell_side"]
    assert sells, "expected a sell-side pool"
    assert all(p.swept for p in sells)
    assert not any(p.reclaimed for p in sells), \
        "a clean break must not read as reclaimed"


def test_a_wick_through_that_closes_back_is_reclaimed():
    """Price spikes below the level and closes back above — the stop-hunt
    this gate is supposed to be looking for."""
    rows = [_bar(101, 102, 100, 101) for _ in range(3)]
    rows += [_bar(101, 101, 100, 100.5)]
    rows += [_bar(101, 102, 94, 101.5)]                    # wick down, close back up
    rows += [_bar(101, 103, 100.5, 102) for _ in range(4)]
    pools = detect_liquidity_pools(_df(rows), 0.005)
    sells = [p for p in pools if p.kind == "sell_side"]
    assert sells and any(p.reclaimed for p in sells), \
        "wick-through-and-close-back was not detected as a reclaim"


def test_require_reclaim_filters_out_clean_breaks():
    rows = [_bar(101, 102, 100, 101) for _ in range(3)]
    rows += [_bar(101, 101, 100, 100.5)]
    rows += [_bar(100, 101, 94, 95)]
    rows += [_bar(95, 96, 94, 95) for _ in range(4)]
    df = _df(rows)
    loose = detect_liquidity_pools(df, 0.005, require_reclaim=False)
    strict = detect_liquidity_pools(df, 0.005, require_reclaim=True)
    assert len(strict) < len(loose), "require_reclaim did not filter anything"
    assert all(p.reclaimed for p in strict)


# --- unswept visibility -----------------------------------------------------


def test_unswept_levels_are_hidden_by_default_and_visible_on_request():
    """The original only ever returned SWEPT pools, so resting liquidity —
    levels price may still run to — was invisible to every caller."""
    rows = [_bar(101, 102, 100, 101) for _ in range(6)]
    rows += [_bar(101, 102, 100.5, 101.5) for _ in range(6)]
    df = _df(rows)
    default = detect_liquidity_pools(df, 0.005)
    assert all(p.swept for p in default), "default view must be swept-only"
    withal = detect_liquidity_pools(df, 0.005, include_unswept=True)
    assert len(withal) >= len(default)


# --- HTF sweep gate ---------------------------------------------------------


def _sig(side=SignalType.LONG) -> Signal:
    return Signal(type=side, entry=100.0, stop_loss=80.0, take_profit=140.0,
                  reason="", confidence=0.9)


def _sweep_frame(n=60) -> pd.DataFrame:
    rows = [_bar(101, 102, 100, 101) for _ in range(4)]
    rows += [_bar(101, 102, 94, 101.5)]
    rows += [_bar(101, 103, 100.5, 102) for _ in range(n)]
    return _df(rows)


def test_htf_sweep_off_reports_ltf_only():
    scr = TradeScreener(ScreenConfig(htf_sweep=False))
    res = scr.screen(_sig(), _sweep_frame(), _sweep_frame())
    check = next(c for c in res.checks if c.name == "Liquidity sweep")
    assert "HTF" not in check.detail, "HTF leaked into the detail with the flag off"


def test_htf_sweep_on_reports_both_timeframes():
    scr = TradeScreener(ScreenConfig(htf_sweep=True))
    res = scr.screen(_sig(), _sweep_frame(), _sweep_frame())
    check = next(c for c in res.checks if c.name == "Liquidity sweep")
    assert "LTF" in check.detail and "HTF" in check.detail


def test_requiring_both_is_never_looser_than_ltf_alone():
    """The strict mode must only ever REMOVE passes, never add them."""
    ltf, htf = _sweep_frame(), _sweep_frame()
    base = TradeScreener(ScreenConfig(htf_sweep=False)).screen(_sig(), ltf, htf)
    both = TradeScreener(ScreenConfig(htf_sweep=True, htf_sweep_require_both=True)).screen(_sig(), ltf, htf)
    b = next(c for c in base.checks if c.name == "Liquidity sweep").passed
    t = next(c for c in both.checks if c.name == "Liquidity sweep").passed
    assert not (t and not b), "require_both approved a sweep the LTF-only gate rejected"


def test_missing_htf_frame_falls_back_to_ltf_without_raising():
    scr = TradeScreener(ScreenConfig(htf_sweep=True))
    res = scr.screen(_sig(), _sweep_frame(), _df([]))
    assert any(c.name == "Liquidity sweep" for c in res.checks)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- market-structure shift gate --------------------------------------------
# SMCStrategy already SCORES a recent BOS/CHoCH as confluence, but scoring is
# not gating: without this a setup can be approved on zones and a sweep alone,
# with structure never having shifted in its favour.


def _flat(n=80) -> pd.DataFrame:
    """No swing structure at all, so no BOS or CHoCH can be detected."""
    return _df([_bar(100, 101, 99, 100) for _ in range(n)])


def _shifted_up() -> pd.DataFrame:
    """A frame that genuinely contains a recent bullish CHoCH.

    Built from a random walk rather than hand-drawn candles: the first attempt
    hand-crafted a "decisive break" that find_swing_points never registered as
    a swing at all, so the test was asserting against a frame with no
    detectable structure. Searching a walk for a frame the detector actually
    flags tests the gate instead of the fixture.
    """
    import numpy as np

    from bot.smc.structure import detect_structure_breaks, find_swing_points

    for seed in range(200):
        rng = np.random.default_rng(seed)
        price, rows = 100.0, []
        for _ in range(200):
            o = price
            c = price + rng.normal(0, 1.4)
            h = max(o, c) + abs(rng.normal(0, 0.8))
            l = min(o, c) - abs(rng.normal(0, 0.8))
            rows.append(_bar(o, h, l, c))
            price = c
        df = _df(rows)
        events = detect_structure_breaks(df, find_swing_points(df, 5))
        if any(e.direction == "bullish" and e.index >= len(df) - 20 for e in events):
            return df
    raise AssertionError("no frame with a recent bullish break found")


def test_structure_gate_absent_when_flag_off():
    res = TradeScreener(ScreenConfig(require_structure_shift=False)).screen(
        _sig(), _flat(), _flat())
    assert not any(c.name == "Market structure shift" for c in res.checks)


def test_structure_gate_rejects_a_frame_with_no_shift():
    res = TradeScreener(ScreenConfig(require_structure_shift=True)).screen(
        _sig(), _flat(), _flat())
    check = next(c for c in res.checks if c.name == "Market structure shift")
    assert not check.passed
    assert not res.approved


def test_structure_gate_passes_when_a_break_aligns_with_the_signal():
    df = _shifted_up()
    res = TradeScreener(ScreenConfig(require_structure_shift=True)).screen(
        _sig(SignalType.LONG), df, df)
    check = next(c for c in res.checks if c.name == "Market structure shift")
    assert check.passed, f"expected a bullish shift, got: {check.detail}"


def test_structure_gate_rejects_a_short_on_a_bullish_shift():
    """Direction must be respected — an upward break does not authorise a
    short. This also pins the long/short -> bullish/bearish translation; a
    silent vocabulary mismatch would make the gate reject everything."""
    df = _shifted_up()
    res = TradeScreener(ScreenConfig(require_structure_shift=True)).screen(
        _sig(SignalType.SHORT), df, df)
    check = next(c for c in res.checks if c.name == "Market structure shift")
    assert not check.passed


def test_structure_gate_respects_the_recency_window():
    """A break that happened long ago must not authorise a trade now."""
    df = _df(_shifted_up().values.tolist() + [_bar(100, 101, 99, 100)] * 60)
    fresh = TradeScreener(ScreenConfig(require_structure_shift=True, structure_shift_bars=200))
    stale = TradeScreener(ScreenConfig(require_structure_shift=True, structure_shift_bars=5))
    f = next(c for c in fresh.screen(_sig(), df, df).checks if c.name == "Market structure shift")
    s = next(c for c in stale.screen(_sig(), df, df).checks if c.name == "Market structure shift")
    assert not s.passed, "a 5-bar window should not see a break 60 bars back"
    if f.passed:
        assert "ago" in f.detail
