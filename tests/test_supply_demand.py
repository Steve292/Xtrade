"""
Tests for bot/smc/supply_demand.py and the strategy diagnostics — no network.

Run directly (`python tests/test_supply_demand.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc.strategy import SMCStrategy, SignalType
from bot.smc.structure import find_swing_points
from bot.smc.supply_demand import (
    detect_supply_demand_zones,
    nearest_zone,
    price_in_zone,
)


def _candles(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_demand_zone_from_swing_low_that_rallied():
    # A swing low at index ~5 followed by a strong rally -> a demand zone.
    rows = []
    for i in range(5):
        rows.append([100, 101, 99, 100])          # flat base
    rows.append([100, 100.5, 97, 98])             # swing low candle (i=5)
    for i in range(6):
        base = 98 + i * 2
        rows.append([base, base + 2.5, base - 0.5, base + 2])  # strong rally
    df = _candles(rows)
    swings = find_swing_points(df, lookback=2)
    zones = detect_supply_demand_zones(df, swings, impulse_pct=0.01, forward=3)
    demands = [z for z in zones if z.kind == "demand"]
    assert demands, "expected at least one demand zone from the rally origin"
    z = demands[0]
    assert z.bottom <= z.top and z.strength > 0


def test_supply_zone_from_swing_high_that_dropped():
    rows = []
    for i in range(5):
        rows.append([100, 101, 99, 100])
    rows.append([100, 103, 99.5, 102])            # swing high candle
    for i in range(6):
        base = 102 - i * 2
        rows.append([base, base + 0.5, base - 2.5, base - 2])  # strong drop
    df = _candles(rows)
    swings = find_swing_points(df, lookback=2)
    zones = detect_supply_demand_zones(df, swings, impulse_pct=0.01, forward=3)
    supplies = [z for z in zones if z.kind == "supply"]
    assert supplies, "expected at least one supply zone from the drop origin"


def test_mitigated_zone_is_dropped():
    # A demand zone that price later closes back below is mitigated -> excluded.
    rows = []
    for i in range(5):
        rows.append([100, 101, 99, 100])
    rows.append([100, 100.5, 97, 98])             # swing low (demand origin)
    for i in range(4):                             # rally away
        base = 98 + i * 2
        rows.append([base, base + 2.5, base - 0.5, base + 2])
    rows.append([104, 104, 90, 91])                # crash back through the zone
    rows.append([91, 92, 89, 90])
    df = _candles(rows)
    swings = find_swing_points(df, lookback=2)
    zones = detect_supply_demand_zones(df, swings, impulse_pct=0.01, forward=3)
    # Any demand zone around ~97-98 must be gone (price closed below its bottom).
    assert not any(z.kind == "demand" and z.bottom >= 96 for z in zones)


def test_price_in_zone_and_nearest():
    from bot.smc.supply_demand import SupplyDemandZone
    z = SupplyDemandZone(index=0, kind="demand", top=100.0, bottom=98.0, strength=0.02)
    assert price_in_zone(99.0, z) is True
    assert price_in_zone(101.0, z) is False
    assert nearest_zone(99.0, [z], "demand") is z
    assert nearest_zone(99.0, [z], "supply") is None
    # within tolerance band just above the top
    assert nearest_zone(100.3, [z], "demand", tolerance_pct=0.01) is z
    # far away -> none
    assert nearest_zone(120.0, [z], "demand") is None


def test_no_zones_on_empty_or_flat_market():
    flat = _candles([[100, 100.5, 99.5, 100] for _ in range(30)])
    swings = find_swing_points(flat, lookback=2)
    zones = detect_supply_demand_zones(flat, swings)
    assert zones == [] or all(z.strength < 0.01 for z in zones)


def test_diagnose_gives_reasons_for_no_setup():
    # A flat, featureless series should produce a "No setup:" diagnostic with
    # concrete clues rather than a generic message.
    strat = SMCStrategy()
    flat = _candles([[100, 100.2, 99.8, 100] for _ in range(60)])
    sig = strat.analyze(flat, flat)
    assert sig.type == SignalType.NONE
    assert sig.reason.startswith("No setup:")
    # At least one recognizable clue is present.
    assert any(k in sig.reason for k in
               ["ranging", "no liquidity sweep", "supply/demand", "equilibrium", "BOS/CHoCH"])


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
