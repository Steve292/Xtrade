"""
Tests for SMCStrategy's stop_loss_pct override — the fixed stop-loss
distance from entry that REPLACES the order-block/FVG-boundary invalidation
stop, at explicit user request (paired with bot/screening.py's raised
max_stop_pct so the Sniper-entry gate can still pass it). No network.

Calls the private _check_long/_check_short directly with hand-built SMC
component objects (OrderBlock, LiquidityPool, SupplyDemandZone, etc.) rather
than driving the full raw-OHLCV detection pipeline (find_swing_points,
detect_order_blocks, ...) — those are already covered by their own test
files; this isolates the one thing that actually changed: how `stop` is
computed once a confluence + entry_zone already exist.

Run directly (`python tests/test_smc_strategy.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.smc.liquidity import LiquidityPool
from bot.smc.order_blocks import OrderBlock
from bot.smc.strategy import SignalType, SMCStrategy
from bot.smc.structure import StructureEvent, Trend
from bot.smc.supply_demand import SupplyDemandZone

# A fully-confluent long: HTF+LTF bullish, a recent bullish BOS, a swept
# sell-side pool, price in the discount half of the range, inside both a
# demand zone and a bullish order block (entry_zone) spanning [90, 100].
LONG_ARGS = dict(
    price=95.0,
    trend=Trend.BULLISH,
    htf_trend=Trend.BULLISH,
    events=[StructureEvent(index=0, kind="bos", direction="bullish", level=99.0)],
    zone=(90.0, 100.0, 110.0),  # (range_low, equilibrium, range_high)
    obs=[OrderBlock(index=0, direction="bullish", top=100.0, bottom=90.0)],
    fvgs=[],
    sweep=LiquidityPool(index=0, kind="sell_side", level=94.0, swept=True),
    sd_zones=[SupplyDemandZone(index=0, kind="demand", top=100.0, bottom=90.0, strength=0.02)],
    df=None,  # unused by _check_long/_check_short
)

# Mirror image: a fully-confluent short, entry_zone spanning [100, 110].
SHORT_ARGS = dict(
    price=105.0,
    trend=Trend.BEARISH,
    htf_trend=Trend.BEARISH,
    events=[StructureEvent(index=0, kind="bos", direction="bearish", level=101.0)],
    zone=(90.0, 100.0, 110.0),
    obs=[OrderBlock(index=0, direction="bearish", top=110.0, bottom=100.0)],
    fvgs=[],
    sweep=LiquidityPool(index=0, kind="buy_side", level=106.0, swept=True),
    sd_zones=[SupplyDemandZone(index=0, kind="supply", top=110.0, bottom=100.0, strength=0.02)],
    df=None,
)


def test_long_confluence_is_high_enough_to_confirm_the_fixture_is_valid():
    # Sanity check on the fixture itself, independent of stop_loss_pct.
    signal = SMCStrategy()._check_long(**LONG_ARGS)
    assert signal.type == SignalType.LONG
    assert signal.confidence >= 0.55


def test_short_confluence_is_high_enough_to_confirm_the_fixture_is_valid():
    signal = SMCStrategy()._check_short(**SHORT_ARGS)
    assert signal.type == SignalType.SHORT
    assert signal.confidence >= 0.55


def test_stop_loss_pct_none_keeps_structural_stop_for_long():
    # Regression guard: omitting stop_loss_pct must reproduce the original
    # order-block-boundary stop (entry_zone.bottom * 0.999), unchanged.
    signal = SMCStrategy()._check_long(**LONG_ARGS)
    assert abs(signal.stop_loss - 90.0 * 0.999) < 1e-9


def test_stop_loss_pct_none_keeps_structural_stop_for_short():
    signal = SMCStrategy()._check_short(**SHORT_ARGS)
    assert abs(signal.stop_loss - 110.0 * 1.001) < 1e-9


def test_stop_loss_pct_overrides_long_stop_to_fixed_pct():
    signal = SMCStrategy(stop_loss_pct=0.20)._check_long(**LONG_ARGS)
    assert signal.type == SignalType.LONG
    assert abs(signal.stop_loss - 95.0 * 0.80) < 1e-9


def test_stop_loss_pct_overrides_short_stop_to_fixed_pct():
    signal = SMCStrategy(stop_loss_pct=0.20)._check_short(**SHORT_ARGS)
    assert signal.type == SignalType.SHORT
    assert abs(signal.stop_loss - 105.0 * 1.20) < 1e-9


def test_stop_loss_pct_take_profit_still_scales_by_reward_risk_ratio():
    strat = SMCStrategy(stop_loss_pct=0.20, reward_risk_ratio=2.0)
    signal = strat._check_long(**LONG_ARGS)
    risk = signal.entry - signal.stop_loss
    assert abs(signal.take_profit - (signal.entry + risk * 2.0)) < 1e-9


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


# --- extended detectors -----------------------------------------------------
# bot/smc/{breaker,mitigation,candles,volume_profile}.py refine confidence
# AFTER the confluence gate has already decided a setup exists. The two
# properties below are the safety contract for that.

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def _walk(seed: int, n: int = 220):
    rng = np.random.default_rng(seed)
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        c = price + rng.normal(0, 1.2)
        h = max(o, c) + abs(rng.normal(0, 0.7))
        l = min(o, c) - abs(rng.normal(0, 0.7))
        rows.append([o, h, l, c, abs(rng.normal(500, 150))])
        price = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_extended_detectors_are_off_by_default():
    assert SMCStrategy().extended_detectors is False


def test_extended_detectors_never_change_entry_stop_or_direction():
    """They refine CONFIDENCE only. If they could move the entry or the stop
    they would be changing the trade, not scoring it."""
    for seed in range(40):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        on = SMCStrategy(extended_detectors=True).analyze(df, htf)
        assert off.type == on.type
        assert off.entry == on.entry
        assert off.stop_loss == on.stop_loss
        assert off.take_profit == on.take_profit


def test_extended_detectors_cannot_manufacture_a_signal():
    """A setup the confluence gate rejected must stay rejected. The refinement
    happens after the gate precisely so a new detector can never push a
    sub-threshold setup over the entry bar."""
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        on = SMCStrategy(extended_detectors=True).analyze(df, htf)
        if off.type is SignalType.NONE:
            assert on.type is SignalType.NONE


def test_confidence_adjustment_respects_its_bound():
    for cap in (0.0, 0.05, 0.10, 0.25):
        for seed in range(30):
            df, htf = _walk(seed), _walk(seed + 9000, 120)
            off = SMCStrategy().analyze(df, htf)
            on = SMCStrategy(extended_detectors=True, extended_max_adjust=cap).analyze(df, htf)
            if off.type is SignalType.NONE:
                continue
            assert abs(on.confidence - off.confidence) <= cap + 1e-9


def test_confidence_stays_within_zero_to_one():
    for seed in range(40):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        on = SMCStrategy(extended_detectors=True, extended_max_adjust=0.5).analyze(df, htf)
        assert 0.0 <= on.confidence <= 1.0


def test_adjustment_moves_in_both_directions_across_a_sample():
    """A refinement that only ever raises confidence is a bias, not a signal —
    and raising it is the direction that increases unattended auto-fire."""
    ups = downs = 0
    for seed in range(120):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        off = SMCStrategy().analyze(df, htf)
        if off.type is SignalType.NONE:
            continue
        on = SMCStrategy(extended_detectors=True).analyze(df, htf)
        delta = on.confidence - off.confidence
        if delta > 0:
            ups += 1
        elif delta < 0:
            downs += 1
    assert ups > 0 and downs > 0, f"adjustment was one-directional (up={ups}, down={downs})"


# --- detector reporting -----------------------------------------------------


def test_signals_report_the_detectors_that_produced_them():
    """Signal.detectors is the join key bot/knowledge.py scores against — it
    must carry the same dotted module names the corpus's `maps_to` uses."""
    found = False
    for seed in range(60):
        df, htf = _walk(seed), _walk(seed + 9000, 120)
        sig = SMCStrategy().analyze(df, htf)
        if sig.type is SignalType.NONE:
            continue
        found = True
        assert sig.detectors, "a real setup reported no detectors"
        assert all(d.startswith("bot.") for d in sig.detectors)
        assert len(set(sig.detectors)) == len(sig.detectors), "duplicate detectors"
    assert found, "sample produced no signals — detector reporting went untested"


def test_no_signal_reports_no_detectors():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert SMCStrategy().analyze(empty).detectors == ()
