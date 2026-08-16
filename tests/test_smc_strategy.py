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
