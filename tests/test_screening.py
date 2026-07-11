"""
Tests for the Fibonacci module and the composite TradeScreener — no network.

Verifies that a clean setup is APPROVED and that failing any single gate (SMC
confluence, top-down alignment, Fibonacci OTE, risk/reward, sniper entry) causes
a REJECT. Also checks the trader's risk-based sizing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.hyperliquid.trader import HyperliquidTrader
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.fibonacci import in_ote, ote_band, recent_leg, retracement_levels
from bot.smc.strategy import Signal, SignalType
from bot.smc.structure import Trend, detect_trend, find_swing_points


def frame(closes):
    ts = pd.date_range("2025-01-01", periods=len(closes), freq="15min")
    return pd.DataFrame({
        "timestamp": ts,
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1] * len(closes),
    })


# A full long setup: equal lows at 100 (sell-side liquidity), a sweep dip to 99
# that takes them out, a rally to 112 (the leg), then a pullback into the
# 0.618-0.786 pocket (~103).
LTF = frame([100, 101, 100, 101, 99,
             101, 103, 105, 107, 109, 111, 112,
             110, 108, 106, 104, 103, 103, 103, 103])
# Strictly rising, all-distinct -> no equal highs/lows, so no liquidity pool is
# ever swept. Used to prove the liquidity-sweep gate fails without a sweep.
LTF_NO_SWEEP = frame([100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                      110, 111, 112, 113, 114, 115, 116, 117, 118, 119])
BULL_HTF = frame([96, 100, 104, 101, 98, 103, 108, 105, 102, 107, 112])
BEAR_HTF = frame([120, 116, 112, 115, 118, 113, 108, 111, 114, 109, 104])

CFG = ScreenConfig(swing_lookback=2, sweep_bars=20)


# ---- Fibonacci ------------------------------------------------------------

def test_retracement_levels_up_leg():
    lv = retracement_levels(90, 110)  # span 20
    assert abs(lv[0.618] - (110 - 20 * 0.618)) < 1e-9
    assert abs(lv[0.5] - 100) < 1e-9


def test_ote_band_and_membership():
    lo, hi = ote_band(90, 110)  # 94.28 .. 97.64
    assert 94 < lo < 95 and 97 < hi < 98
    assert in_ote(96, 90, 110)
    assert not in_ote(105, 90, 110)


def test_recent_leg_long_picks_low_to_high():
    leg = recent_leg(find_swing_points(LTF, 2), "long")
    assert leg is not None
    assert abs(leg[0] - 99) < 1.0 and abs(leg[1] - 112) < 1.0


# ---- screener setup sanity ------------------------------------------------

def test_htf_trends_detect_as_expected():
    assert detect_trend(find_swing_points(BULL_HTF, 2)) == Trend.BULLISH
    assert detect_trend(find_swing_points(BEAR_HTF, 2)) == Trend.BEARISH


def _long_signal(entry, sl_pct=0.01, rr=2.5, conf=0.8):
    sl = entry * (1 - sl_pct)
    tp = entry + (entry - sl) * rr
    return Signal(SignalType.LONG, entry, sl, tp, "test", conf)


def _pocket_mid():
    leg = recent_leg(find_swing_points(LTF, 2), "long")
    lo, hi = ote_band(*leg)
    return (lo + hi) / 2


# ---- screener: approve + each failure mode --------------------------------

def test_clean_setup_approved():
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid()), LTF, BULL_HTF)
    assert r.approved, r.table()


def test_low_confidence_rejected():
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid(), conf=0.4), LTF, BULL_HTF)
    assert not r.approved
    assert not next(c for c in r.checks if c.name == "SMC confluence").passed


def test_htf_opposing_rejected():
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid()), LTF, BEAR_HTF)
    assert not r.approved
    assert not next(c for c in r.checks if c.name == "Top-down alignment").passed


def test_liquidity_sweep_required():
    # clean signal, but on data with no swept sell-side low -> sweep gate fails
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid()), LTF_NO_SWEEP, BULL_HTF)
    assert not r.approved
    assert not next(c for c in r.checks if c.name == "Liquidity sweep").passed


def test_entry_outside_ote_rejected():
    r = TradeScreener(CFG).screen(_long_signal(110.0), LTF, BULL_HTF)  # at the high, not the pocket
    assert not r.approved
    assert not next(c for c in r.checks if c.name == "Fibonacci OTE (final)").passed


def test_low_rr_rejected():
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid(), rr=1.0), LTF, BULL_HTF)
    assert not r.approved
    assert not next(c for c in r.checks if c.name == "Risk/reward").passed


def test_wide_stop_fails_sniper_only():
    # 3% stop (> 2% max) but RR still 2.5 and in the pocket -> only sniper fails
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid(), sl_pct=0.03), LTF, BULL_HTF)
    assert not r.approved
    failed = [c.name for c in r.checks if not c.passed]
    assert failed == ["Sniper entry"], failed


# ---- trader sizing --------------------------------------------------------

def test_plan_sizes_by_risk():
    t = HyperliquidTrader(client=None, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    sig = _long_signal(100.0, sl_pct=0.01)  # 1% stop
    plan = t._plan("BTC", sig, account_value=10000)
    # risk $100 / 1% stop = $10,000 notional (within 3x buying power)
    assert plan is not None and abs(plan.usd - 10000) < 1e-6
    assert plan.side == "long"


def test_plan_skips_when_below_min_order():
    t = HyperliquidTrader(client=None, strategy=None, screener=None, risk_pct=0.001, leverage=3)
    sig = _long_signal(100.0, sl_pct=0.05)  # tiny risk, wide stop -> notional < $10
    assert t._plan("BTC", sig, account_value=100) is None


def test_plan_capped_by_free_margin_not_total_equity():
    # Real scenario hit live: account_value=$5.39 but withdrawable=$0 because an
    # existing position has all margin locked. Must skip (return None), not size
    # against total equity and get exchange-rejected for insufficient margin.
    t = HyperliquidTrader(client=None, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    sig = _long_signal(1822.1, sl_pct=0.0013)  # mirrors the live ETH sniper entry
    assert t._plan("ETH", sig, account_value=5.39, withdrawable=0.0) is None


def test_plan_uses_free_margin_when_partially_available():
    t = HyperliquidTrader(client=None, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    sig = _long_signal(100.0, sl_pct=0.01)  # 1% stop
    # Plenty of equity, but only $10 free -> buying power capped at $10*3=$30,
    # well below the risk-driven $1000 notional this would otherwise want.
    plan = t._plan("BTC", sig, account_value=100000, withdrawable=10.0)
    assert plan is not None and abs(plan.usd - 30.0) < 1e-6


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
