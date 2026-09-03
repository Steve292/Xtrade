"""
Tests for bot/smart_money.py — Section 4's smart money signal modules.
No network: cvd_signal/liquidation_heatmap_signal take a plain DataFrame,
everything else takes plain values.

Run directly (`python tests/test_smart_money.py`) or under pytest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smart_money import (
    aggregate_smart_money,
    cvd_signal,
    divergence_signal,
    gex_signal,
    liquidation_heatmap_signal,
    narrative_decay_signal,
    session_signal,
    smc_fib_signal,
    stablecoin_flow_signal,
)


def _candles(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# --- cvd_signal --------------------------------------------------------


def test_cvd_signal_buy_when_up_volume_dominates():
    rows = [
        {"open": 100, "close": 105, "high": 106, "low": 99, "volume": 100},
        {"open": 105, "close": 110, "high": 111, "low": 104, "volume": 100},
        {"open": 110, "close": 108, "high": 111, "low": 107, "volume": 10},
    ]
    result = cvd_signal(_candles(rows), lookback=3)
    assert result["signal"] == "BUY"
    assert result["strength"] > 0.1


def test_cvd_signal_sell_when_down_volume_dominates():
    rows = [
        {"open": 105, "close": 100, "high": 106, "low": 99, "volume": 100},
        {"open": 110, "close": 105, "high": 111, "low": 104, "volume": 100},
        {"open": 108, "close": 110, "high": 111, "low": 107, "volume": 10},
    ]
    result = cvd_signal(_candles(rows), lookback=3)
    assert result["signal"] == "SELL"


def test_cvd_signal_neutral_when_balanced():
    rows = [
        {"open": 100, "close": 101, "high": 102, "low": 99, "volume": 50},
        {"open": 101, "close": 100, "high": 102, "low": 99, "volume": 50},
    ]
    result = cvd_signal(_candles(rows), lookback=2)
    assert result["signal"] == "NEUTRAL"


def test_cvd_signal_handles_zero_volume():
    rows = [{"open": 100, "close": 105, "high": 106, "low": 99, "volume": 0}]
    result = cvd_signal(_candles(rows), lookback=1)
    assert result == {"signal": "NEUTRAL", "strength": 0.0}


# --- gex_signal --------------------------------------------------------


def test_gex_signal_caution_near_heaviest_strike():
    oi = {100000.0: 5.0, 101000.0: 500.0, 105000.0: 3.0}
    result = gex_signal(spot_price=101200.0, oi_by_strike=oi, caution_pct=3.0)
    assert result["signal"] == "CAUTION"
    assert result["flip_zone"] == 101000.0


def test_gex_signal_neutral_when_far_from_heaviest_strike():
    oi = {100000.0: 5.0, 101000.0: 500.0}
    result = gex_signal(spot_price=150000.0, oi_by_strike=oi, caution_pct=3.0)
    assert result["signal"] == "NEUTRAL"


def test_gex_signal_never_emits_a_direction():
    # Regression guard for the module's core honesty claim: whatever the OI
    # shape, this must never return BUY or SELL.
    for spot, oi in [
        (100.0, {100.0: 1.0}),
        (100.0, None),
        (0.0, {100.0: 1.0}),
        (100.0, {99.0: 1.0, 101.0: 1.0}),
    ]:
        assert gex_signal(spot, oi)["signal"] in ("NEUTRAL", "CAUTION")


def test_gex_signal_handles_missing_data():
    assert gex_signal(100.0, None)["signal"] == "NEUTRAL"
    assert gex_signal(0.0, {100.0: 1.0})["signal"] == "NEUTRAL"


# --- stablecoin_flow_signal -----------------------------------------------


def test_stablecoin_flow_buy_below_threshold():
    result = stablecoin_flow_signal(ssr=2.5)
    assert result == {"signal": "BUY", "ssr": 2.5}


def test_stablecoin_flow_sell_above_threshold():
    result = stablecoin_flow_signal(ssr=4.0)
    assert result == {"signal": "SELL", "ssr": 4.0}


def test_stablecoin_flow_neutral_when_missing():
    assert stablecoin_flow_signal(ssr=None) == {"signal": "NEUTRAL", "ssr": None}


# --- liquidation_heatmap_signal -------------------------------------------


def _sweep_candles() -> pd.DataFrame:
    base = {"open": 100.0, "close": 100.0, "high": 101.0, "low": 99.0, "volume": 10.0}
    rows = [dict(base) for _ in range(5)]  # bars 0-4: quiet baseline
    rows.append({"open": 100, "close": 100, "high": 105.0, "low": 99, "volume": 10})  # bar 5: touch 1
    rows.append(dict(base))  # bar 6
    rows.append({"open": 100, "close": 100, "high": 105.0002, "low": 99, "volume": 10})  # bar 7: touch 2 (pool forms)
    rows.append({"open": 100, "close": 104, "high": 106.0, "low": 99, "volume": 10})  # bar 8: sweeps above 105
    return _candles(rows)


def test_liquidation_heatmap_buy_on_recent_sweep():
    result = liquidation_heatmap_signal(_sweep_candles(), bars=5)
    assert result["signal"] == "BUY"
    assert result["kind"] == "buy_side"
    assert result["level"] is not None


def test_liquidation_heatmap_neutral_with_no_sweep():
    flat = _candles([{"open": 100.0, "close": 100.0, "high": 100.5, "low": 99.5, "volume": 10.0} for _ in range(20)])
    result = liquidation_heatmap_signal(flat, bars=5)
    assert result == {"signal": "NEUTRAL", "level": None, "kind": None}


# --- narrative_decay_signal ------------------------------------------------


def test_narrative_decay_is_always_unavailable():
    assert narrative_decay_signal() == {"signal": "NEUTRAL", "available": False}


# --- divergence_signal -----------------------------------------------------


def test_divergence_buy_when_decoupled():
    result = divergence_signal(btc_24h_change_pct=3.0, spx_24h_change_pct=-2.0)
    assert result["signal"] == "BUY"


def test_divergence_hedge_when_correlated():
    result = divergence_signal(btc_24h_change_pct=2.0, spx_24h_change_pct=1.5)
    assert result["signal"] == "HEDGE"


def test_divergence_neutral_when_missing_data():
    assert divergence_signal(None, -2.0)["signal"] == "NEUTRAL"


def test_divergence_requires_more_than_a_trivial_gap():
    # Opposite signs but both essentially flat -> not meaningfully diverging.
    result = divergence_signal(btc_24h_change_pct=0.05, spx_24h_change_pct=-0.05)
    assert result["signal"] == "HEDGE"


# --- smc_fib_signal ----------------------------------------------------


def test_smc_fib_signal_maps_long_short_none():
    assert smc_fib_signal("long") == {"signal": "BUY"}
    assert smc_fib_signal("short") == {"signal": "SELL"}
    assert smc_fib_signal("none") == {"signal": "NEUTRAL"}


# --- session_signal ----------------------------------------------------


def test_session_signal_overlap_window():
    now = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)  # 14:00 UTC
    result = session_signal(now)
    assert result == {"signal": "EXECUTE", "session": "LONDON_NY_OVERLAP"}


def test_session_signal_asia_only():
    now = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    assert session_signal(now) == {"signal": "EXECUTE", "session": "ASIA"}


def test_session_signal_dead_zone():
    now = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)
    assert session_signal(now) == {"signal": "HOLD", "session": "DEAD_ZONE"}


def test_session_signal_london_only():
    now = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    assert session_signal(now) == {"signal": "EXECUTE", "session": "LONDON"}


def test_session_signal_new_york_only():
    now = datetime(2026, 1, 5, 18, 0, tzinfo=timezone.utc)
    assert session_signal(now) == {"signal": "EXECUTE", "session": "NEW_YORK"}


# --- aggregate_smart_money -------------------------------------------------


def test_aggregate_bullish_when_more_buys():
    modules = {
        "cvd": {"signal": "BUY"},
        "flow": {"signal": "BUY"},
        "smc_fib": {"signal": "BUY"},
        "heatmap": {"signal": "BUY"},
        "session": {"signal": "EXECUTE"},
        "gex": {"signal": "NEUTRAL"},
    }
    result = aggregate_smart_money(modules)
    assert result.direction == "BULLISH"
    assert result.bullish_count == 4
    assert result.multiplier == 2.0


def test_aggregate_bearish_capped_at_three_modules():
    # Bearish can only ever draw on CVD/flow/SMC+Fib (heatmap and divergence
    # are BUY-or-NEUTRAL-only by design) -- 3 agree is the ceiling.
    modules = {
        "cvd": {"signal": "SELL"},
        "flow": {"signal": "SELL"},
        "smc_fib": {"signal": "SELL"},
    }
    result = aggregate_smart_money(modules)
    assert result.direction == "BEARISH"
    assert result.bearish_count == 3
    assert result.multiplier == 1.0


def test_aggregate_two_agree_gives_half_size():
    modules = {"cvd": {"signal": "BUY"}, "flow": {"signal": "BUY"}, "smc_fib": {"signal": "NEUTRAL"}}
    result = aggregate_smart_money(modules)
    assert result.multiplier == 0.5


def test_aggregate_less_than_two_holds_cash():
    modules = {"cvd": {"signal": "BUY"}, "flow": {"signal": "NEUTRAL"}}
    result = aggregate_smart_money(modules)
    assert result.multiplier == 0.0


def test_aggregate_tie_is_neutral():
    modules = {"cvd": {"signal": "BUY"}, "flow": {"signal": "SELL"}}
    result = aggregate_smart_money(modules)
    assert result.direction == "NEUTRAL"
    assert result.multiplier == 0.0


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
