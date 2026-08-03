"""
Tests for the Fibonacci module and the composite TradeScreener — no network.

Verifies that a clean setup is APPROVED and that failing any single gate (SMC
confluence, top-down alignment, Fibonacci OTE, risk/reward, sniper entry) causes
a REJECT. Also checks the trader's risk-based sizing.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.capital_guard import CapitalGuard
from bot.hyperliquid.client import Position
from bot.hyperliquid.trader import HyperliquidTrader, TradePlan
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


def frame_ohlc(rows):
    """Explicit [open, high, low, close] candles — needed when a test relies on
    candle bodies (e.g. a large demand candle forming a supply/demand zone),
    which the close-only `frame()` helper can't express (it has zero-body bars)."""
    ts = pd.date_range("2025-01-01", periods=len(rows), freq="15min")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({
        "timestamp": ts, "open": list(o), "high": list(h),
        "low": list(l), "close": list(c), "volume": [1] * len(rows),
    })


# A genuinely clean 7-gate long: equal lows at 100 (sell-side liquidity), a big
# bullish demand candle at index 4 that sweeps to 98 and closes at 104 (forming
# a demand zone [98, 104]), a rally to 112 (the leg), then a pullback to ~102 —
# which sits inside BOTH the demand zone and the 0.618-0.786 OTE pocket.
LTF_SD = frame_ohlc([
    [100.5, 101, 100, 100.5], [100.5, 101, 100, 100.5],
    [100.5, 101, 100, 100.5], [100.5, 101, 100, 100.5],
    [100, 104.5, 98, 104],                                   # 4: demand candle + sweep
    [104, 106.5, 103.5, 106], [106, 108.5, 105.5, 108],
    [108, 110.5, 107.5, 110], [110, 112.5, 109.5, 112],      # rally (impulse) to 112
    [112, 112, 110, 110.5], [110.5, 111, 108, 108.5],
    [108.5, 109, 106, 106.5], [106.5, 107, 104, 104.5],
    [104.5, 105, 102, 102.5],                                # pullback into the pocket
    [102.5, 103, 101.5, 102], [102, 102.5, 101.5, 102],
    [102, 102.5, 101.5, 102], [102, 102.5, 101.5, 102],
    [102, 102.5, 101.5, 102], [102, 102.5, 101.5, 102],
])


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


def _pocket_mid_of(f):
    leg = recent_leg(find_swing_points(f, 2), "long")
    lo, hi = ote_band(*leg)
    return (lo + hi) / 2


# ---- screener: approve + each failure mode --------------------------------

def test_clean_setup_approved():
    # Uses LTF_SD, where the entry sits in both a demand zone and the OTE pocket,
    # so all seven gates (including Supply/Demand) can pass.
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid_of(LTF_SD)), LTF_SD, BULL_HTF)
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
    # 30% stop (> 25% max) but RR still 2.5, in the pocket, and at a demand
    # zone (LTF_SD) -> only sniper fails.
    r = TradeScreener(CFG).screen(_long_signal(_pocket_mid_of(LTF_SD), sl_pct=0.30), LTF_SD, BULL_HTF)
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


# ---- trader execute() / check_exits() -------------------------------------

class StubHLClient:
    """Stub venue for HyperliquidTrader.execute()/check_exits() — no network."""

    def __init__(self, fill_size=None, fill_price=None, reject=False, bracket_error=False):
        self.calls = []
        self.fill_size = fill_size
        self.fill_price = fill_price
        self.reject = reject
        self.bracket_error = bracket_error
        self.resting = {}  # coin -> count of resting trigger orders, for enforce_brackets()

    def long(self, coin, usd, leverage=None):
        self.calls.append(("long", coin, usd, leverage))
        return self._order_resp()

    def short(self, coin, usd, leverage=None):
        self.calls.append(("short", coin, usd, leverage))
        return self._order_resp()

    def _order_resp(self):
        if self.reject:
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"error": "Insufficient margin to place order. asset=1"}]}}}
        return {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"filled": {"totalSz": str(self.fill_size), "avgPx": str(self.fill_price), "oid": 1}}]}}}

    def attach_bracket(self, coin, is_buy, size, stop_loss, take_profit):
        self.calls.append(("attach_bracket", coin, is_buy, size, stop_loss, take_profit))
        if self.bracket_error:
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"error": "bad trigger"}, {"error": "bad trigger"}]}}}
        return {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"resting": {"oid": 2}}, {"resting": {"oid": 3}}]}}}

    def open_trigger_orders(self, coin):
        self.calls.append(("open_trigger_orders", coin))
        val = self.resting.get(coin, 0)
        return val if isinstance(val, list) else [{}] * val  # int -> N placeholder legs

    def modify_trigger_order(self, coin, oid, is_buy, size, trigger_px, tpsl):
        self.calls.append(("modify_trigger_order", coin, oid, is_buy, size, trigger_px, tpsl))
        if self.bracket_error:
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"error": "bad modify"}]}}}
        return {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"resting": {"oid": oid}}]}}}


def _eth_plan():
    return TradePlan(coin="ETH", side="long", usd=16.35, leverage=3, entry=1822.1,
                      stop_loss=1819.73, take_profit=1826.84, risk_pct=1.0)


def test_execute_attaches_bracket_on_fill():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())
    assert [c[0] for c in client.calls] == ["long", "attach_bracket"]
    _, coin, is_buy, size, sl, tp = client.calls[1]
    assert coin == "ETH" and is_buy is True and size == 0.0089
    assert sl == 1819.73 and tp == 1826.84
    assert t._tracked["ETH"]["entry"] == 1822.1


def test_execute_skips_bracket_on_rejected_order():
    client = StubHLClient(reject=True)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())
    assert [c[0] for c in client.calls] == ["long"]  # no attach_bracket attempted
    assert "ETH" not in t._tracked


def test_execute_does_not_track_on_bracket_rejection():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1, bracket_error=True)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())
    assert "ETH" not in t._tracked  # bracket rejected -> don't claim it's protected


def test_check_exits_reports_and_drops_closed_positions():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())
    assert "ETH" in t._tracked

    closed = t.check_exits(open_positions=[])  # ETH no longer open
    assert len(closed) == 1 and closed[0]["coin"] == "ETH"
    assert "ETH" not in t._tracked


def test_check_exits_leaves_still_open_positions_alone():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())

    still_open = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1,
                            unrealized_pnl=0.0, leverage=3.0)]
    closed = t.check_exits(open_positions=still_open)
    assert closed == []
    assert "ETH" in t._tracked


def test_enforce_brackets_skips_already_protected_position():
    client = StubHLClient()
    client.resting = {"ETH": 2}  # both legs already resting
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1, unrealized_pnl=0.0, leverage=3.0)]
    enforced = t.enforce_brackets(pos, account_value=1000.0)
    assert enforced == []
    assert not any(c[0] == "attach_bracket" for c in client.calls)


def test_enforce_brackets_uses_tracked_plan_when_available():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())  # populates _tracked["ETH"] with the plan's real SL/TP

    client.resting = {}  # bracket never actually made it to the venue
    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1, unrealized_pnl=0.0, leverage=3.0)]
    enforced = t.enforce_brackets(pos, account_value=1000.0)
    assert len(enforced) == 1 and enforced[0]["coin"] == "ETH"
    _, coin, is_buy, size, sl, tp = client.calls[-1]
    assert coin == "ETH" and sl == 1819.73 and tp == 1826.84  # the plan's own values, not recomputed


def test_enforce_brackets_falls_back_to_risk_based_stop_when_untracked():
    client = StubHLClient()
    screener = TradeScreener(ScreenConfig(min_rr=2.0))
    t = HyperliquidTrader(client=client, strategy=None, screener=screener, risk_pct=1.0, leverage=3)
    # ETH was never opened by this trader instance (e.g. bot restarted) -> untracked
    pos = [Position(coin="ETH", side="long", size=10.0, entry=100.0, unrealized_pnl=0.0, leverage=3.0)]
    enforced = t.enforce_brackets(pos, account_value=1000.0)
    assert len(enforced) == 1
    # dollar risk bounded to 1% of $1000 = $10, spread over size 10 -> $1/unit
    assert abs(enforced[0]["stop_loss"] - 99.0) < 1e-9
    assert abs(enforced[0]["take_profit"] - 102.0) < 1e-9  # 2R reward
    assert "ETH" in t._tracked  # now tracked going forward


def test_enforce_brackets_fallback_bounds_dollar_risk_regardless_of_size():
    # Regression for a real bug: POPCAT lost tracking after a restart and got
    # re-protected with a flat max_stop_pct (2%) applied to its already-large
    # size (sized against a much tighter ~0.2-0.5% original sniper stop) —
    # the eventual stop-out cost ~6% of account equity in one trade instead
    # of the intended ~1%. The fix must bound dollar risk to risk_pct of
    # equity NO MATTER how large the position's size already is.
    client = StubHLClient()
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    pos = [Position(coin="POPCAT", side="long", size=340.0, entry=0.045207, unrealized_pnl=0.0, leverage=3.0)]
    account_value = 5.39

    enforced = t.enforce_brackets(pos, account_value=account_value)
    stop_loss = enforced[0]["stop_loss"]
    dollar_risk = (pos[0].entry - stop_loss) * pos[0].size
    assert dollar_risk <= account_value * 0.01 + 1e-9  # never more than the configured 1% risk
    # a flat 2% stop on this size would have risked ~$0.31 — 6x the intended ~$0.05
    naive_2pct_risk = pos[0].entry * 0.02 * pos[0].size
    assert dollar_risk < naive_2pct_risk / 3


def test_enforce_brackets_backfills_tracking_for_untracked_but_protected_position():
    # Simulates POPCAT's real situation: a previous process attached a bracket
    # and then restarted (or never had oid-tracking at all), so this fresh
    # trader has no _tracked entry even though the position is protected.
    client = StubHLClient()
    client.resting = {"POPCAT": [
        {"triggerPx": "0.044303", "oid": 501},  # below entry -> SL for a long
        {"triggerPx": "0.047015", "oid": 502},  # above entry -> TP for a long
    ]}
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    pos = [Position(coin="POPCAT", side="long", size=340.0, entry=0.045207, unrealized_pnl=0.0, leverage=3.0)]

    enforced = t.enforce_brackets(pos, account_value=5.39)
    assert enforced == []  # nothing needed attaching
    assert not any(c[0] == "attach_bracket" for c in client.calls)
    tracked = t._tracked["POPCAT"]
    assert tracked["sl_oid"] == 501 and tracked["tp_oid"] == 502
    assert abs(tracked["stop_loss"] - 0.044303) < 1e-9
    assert abs(tracked["take_profit"] - 0.047015) < 1e-9

    # and now ratchet_stops can actually act on it going forward
    risk_per_unit = 0.045207 - 0.044303
    pos_1r = [Position(coin="POPCAT", side="long", size=340.0, entry=0.045207,
                        unrealized_pnl=risk_per_unit * 340.0, leverage=3.0)]
    ratcheted = t.ratchet_stops(pos_1r)
    assert len(ratcheted) == 1 and ratcheted[0]["milestone"] == 1
    call = next(c for c in client.calls if c[0] == "modify_trigger_order")
    assert call[2] == 501  # moved the SL leg specifically, by its real oid


def test_enforce_brackets_warns_but_does_not_crash_on_failure():
    client = StubHLClient(bracket_error=True)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1, unrealized_pnl=0.0, leverage=3.0)]
    enforced = t.enforce_brackets(pos, account_value=1000.0)
    assert enforced == []
    assert "ETH" not in t._tracked


# ---- trader ratchet_stops() ------------------------------------------------
# _eth_plan(): entry=1822.1, stop_loss=1819.73 -> risk_per_unit=2.37, size=0.0089

def test_ratchet_stops_noop_below_first_milestone():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())

    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1,
                     unrealized_pnl=1.0 * 0.0089, leverage=3.0)]  # ~0.42R, below the first milestone
    assert t.ratchet_stops(pos) == []
    assert not any(c[0] == "modify_trigger_order" for c in client.calls)


def test_ratchet_stops_locks_in_quarter_r_at_first_milestone():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())

    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1,
                     unrealized_pnl=2.37 * 0.0089, leverage=3.0)]  # exactly 1R
    ratcheted = t.ratchet_stops(pos)
    assert len(ratcheted) == 1 and ratcheted[0]["milestone"] == 1
    expected_sl = 1822.1 + 0.25 * 2.37
    assert abs(ratcheted[0]["new_stop_loss"] - expected_sl) < 1e-9
    assert t._tracked["ETH"]["milestones_locked"] == 1
    call = next(c for c in client.calls if c[0] == "modify_trigger_order")
    assert call[1] == "ETH" and call[2] == 2  # sl_oid, from attach_bracket's stubbed response
    assert call[3] is False  # is_buy=False to close a long


def test_ratchet_stops_jumps_multiple_milestones_in_one_pass():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())

    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1,
                     unrealized_pnl=3 * 2.37 * 0.0089, leverage=3.0)]  # 3R in a single 30s poll gap
    ratcheted = t.ratchet_stops(pos)
    assert ratcheted[0]["milestone"] == 3
    assert t._tracked["ETH"]["milestones_locked"] == 3
    expected_sl = 1822.1 + 0.75 * 2.37  # 3 milestones * 0.25R each
    assert abs(ratcheted[0]["new_stop_loss"] - expected_sl) < 1e-9


def test_ratchet_stops_same_milestone_is_a_noop_on_next_pass():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())
    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1, unrealized_pnl=2.37 * 0.0089, leverage=3.0)]
    t.ratchet_stops(pos)
    calls_before = len(client.calls)

    assert t.ratchet_stops(pos) == []  # still 1R, no new milestone
    assert len(client.calls) == calls_before  # no additional modify_trigger_order call


def test_ratchet_stops_never_loosens_if_price_pulls_back():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())
    pos_2r = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1,
                        unrealized_pnl=2 * 2.37 * 0.0089, leverage=3.0)]
    t.ratchet_stops(pos_2r)
    locked_sl = t._tracked["ETH"]["stop_loss"]

    pos_1r = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1,
                        unrealized_pnl=1 * 2.37 * 0.0089, leverage=3.0)]
    assert t.ratchet_stops(pos_1r) == []  # pulled back to 1R
    assert t._tracked["ETH"]["stop_loss"] == locked_sl  # still locked at the 2R level, not loosened


def test_ratchet_stops_symmetric_for_short():
    client = StubHLClient(fill_size=0.1, fill_price=100.0)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    short_plan = TradePlan(coin="BTC", side="short", usd=10.0, leverage=3, entry=100.0,
                            stop_loss=102.0, take_profit=94.0, risk_pct=1.0)
    t.execute(short_plan)  # risk_per_unit = 2.0

    pos = [Position(coin="BTC", side="short", size=0.1, entry=100.0,
                     unrealized_pnl=2.0 * 0.1, leverage=3.0)]  # exactly 1R for a short
    ratcheted = t.ratchet_stops(pos)
    assert len(ratcheted) == 1 and ratcheted[0]["milestone"] == 1
    expected_sl = 100.0 - 0.25 * 2.0  # stop tightens DOWNWARD for a short
    assert abs(ratcheted[0]["new_stop_loss"] - expected_sl) < 1e-9
    call = next(c for c in client.calls if c[0] == "modify_trigger_order")
    assert call[3] is True  # is_buy=True to close a short


def test_ratchet_stops_skips_positions_without_sl_oid():
    client = StubHLClient()
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t._tracked["ETH"] = {"side": "long", "entry": 1822.1, "initial_stop_loss": 1819.73,
                          "stop_loss": 1819.73, "take_profit": 1826.84, "milestones_locked": 0}
    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1, unrealized_pnl=2.37 * 0.0089, leverage=3.0)]
    assert t.ratchet_stops(pos) == []


def test_ratchet_stops_warns_but_does_not_crash_on_modify_failure():
    client = StubHLClient(fill_size=0.0089, fill_price=1822.1)
    t = HyperliquidTrader(client=client, strategy=None, screener=None, risk_pct=1.0, leverage=3)
    t.execute(_eth_plan())
    client.bracket_error = True  # now make modify_trigger_order fail too
    pos = [Position(coin="ETH", side="long", size=0.0089, entry=1822.1, unrealized_pnl=2.37 * 0.0089, leverage=3.0)]
    ratcheted = t.ratchet_stops(pos)
    assert ratcheted == []
    assert t._tracked["ETH"]["milestones_locked"] == 0  # unchanged


# ---- guard_check() / combined ledger ---------------------------------------

def test_guard_check_blocked_by_halted_combined_guard():
    # A halted combined_guard must block a trade even though this venue's own
    # capital_guard (and its own account_value) would otherwise be fine —
    # that's the entire point of a cross-venue circuit breaker.
    combined = CapitalGuard(max_daily_loss_pct=3.0)
    combined.update(100.0, date(2026, 7, 20))
    combined.update(90.0, date(2026, 7, 20))  # -10%, past the 3% limit -> halted
    assert combined.halted

    t = HyperliquidTrader(client=StubHLClient(), strategy=None, screener=None,
                           risk_pct=1.0, leverage=3, combined_guard=combined)
    allowed, reason = t.guard_check(account_value=100000.0)  # this venue looks fine
    assert not allowed
    assert "combined ledger" in reason


def test_guard_check_passes_through_when_combined_guard_not_halted():
    combined = CapitalGuard(max_daily_loss_pct=3.0)
    combined.update(100.0, date(2026, 7, 20))  # no loss yet -> not halted

    t = HyperliquidTrader(client=StubHLClient(), strategy=None, screener=None,
                           risk_pct=1.0, leverage=3, combined_guard=combined)
    allowed, reason = t.guard_check(account_value=100000.0)  # no per-venue guard configured
    assert allowed and reason is None


def test_guard_check_confidence_floor_blocks_and_allows():
    # Runtime authorization floor: a raised floor blocks a lower-confidence
    # signal even though everything else is fine. Monkeypatch the live_state
    # reader so the test never touches the real live_state.json.
    from bot.hyperliquid import trader as trader_mod
    orig = trader_mod.live_state.get_min_confidence
    trader_mod.live_state.get_min_confidence = lambda *a, **k: 0.85
    try:
        t = HyperliquidTrader(client=StubHLClient(), strategy=None, screener=None,
                               risk_pct=1.0, leverage=3)
        blocked, reason = t.guard_check(account_value=100000.0, confidence=0.70)
        assert not blocked and "authorization floor" in reason
        ok, _ = t.guard_check(account_value=100000.0, confidence=0.90)
        assert ok  # at/above the floor, no per-venue guard configured
        # The exact boundary: confidence == floor must ALSO pass ("85% and
        # above must fire" means >=, not a strict > that would silently
        # reject exactly-85% signals).
        at_floor, _ = t.guard_check(account_value=100000.0, confidence=0.85)
        assert at_floor
        just_below, reason_below = t.guard_check(account_value=100000.0, confidence=0.849999)
        assert not just_below and "authorization floor" in reason_below
    finally:
        trader_mod.live_state.get_min_confidence = orig


def test_guard_check_without_confidence_skips_floor():
    # Callers that don't pass confidence (e.g. non-signal contexts) must not be
    # blocked by the floor — the check only applies when confidence is supplied.
    from bot.hyperliquid import trader as trader_mod
    orig = trader_mod.live_state.get_min_confidence
    trader_mod.live_state.get_min_confidence = lambda *a, **k: 0.99
    try:
        t = HyperliquidTrader(client=StubHLClient(), strategy=None, screener=None,
                               risk_pct=1.0, leverage=3)
        ok, reason = t.guard_check(account_value=100000.0)  # no confidence arg
        assert ok and reason is None
    finally:
        trader_mod.live_state.get_min_confidence = orig


# ---- auto-fire vs. queue split -------------------------------------------

class _StubUnified:
    def __init__(self, final_pct, direction="BULLISH", agreement=4):
        self.final_pct = final_pct
        self.smart_money_direction = direction
        self.smart_money_agreement_count = agreement


def _stub_plan():
    return TradePlan(coin="BTC", side="long", usd=250.0, leverage=3, entry=100.0,
                     stop_loss=80.0, take_profit=140.0, risk_pct=1.0)


def _queue_decision(final_pct, threshold, queue_path):
    """Run queue_if_below_auto_fire with both live_state and the queue file
    redirected, so nothing touches the real live_state.json / queue."""
    from bot.hyperliquid import trader as trader_mod
    orig_get = trader_mod.live_state.get_auto_fire_pct
    orig_add = trader_mod.pending_trades.add
    calls = []
    trader_mod.live_state.get_auto_fire_pct = lambda *a, **k: threshold
    trader_mod.pending_trades.add = lambda **kw: calls.append(kw) or kw
    try:
        result = HyperliquidTrader.queue_if_below_auto_fire(
            "BTC", _long_signal(100.0), _stub_plan(), _StubUnified(final_pct)
        )
        return result, calls
    finally:
        trader_mod.live_state.get_auto_fire_pct = orig_get
        trader_mod.pending_trades.add = orig_add


def test_at_or_above_auto_fire_threshold_fires_and_does_not_queue():
    result, calls = _queue_decision(final_pct=90.0, threshold=90.0, queue_path=None)
    assert result is None      # None == "caller should fire now"
    assert calls == []         # nothing queued


def test_below_auto_fire_threshold_queues_instead_of_firing():
    result, calls = _queue_decision(final_pct=74.0, threshold=90.0, queue_path=None)
    assert result == 90.0      # returns the threshold it fell short of
    assert len(calls) == 1
    assert calls[0]["venue"] == "hl"
    assert calls[0]["symbol"] == "BTC"
    assert calls[0]["side"] == "long"
    assert calls[0]["final_pct"] == 74.0


def test_queued_entry_carries_the_plans_prices_not_the_raw_signals():
    _, calls = _queue_decision(final_pct=50.0, threshold=90.0, queue_path=None)
    assert calls[0]["stop_loss"] == 80.0
    assert calls[0]["take_profit"] == 140.0
    assert calls[0]["size"] == 250.0  # USD notional on this venue


def test_threshold_of_zero_fires_everything():
    result, calls = _queue_decision(final_pct=1.0, threshold=0.0, queue_path=None)
    assert result is None and calls == []


def test_threshold_of_100_queues_all_but_a_perfect_score():
    result, calls = _queue_decision(final_pct=99.9, threshold=100.0, queue_path=None)
    assert result == 100.0 and len(calls) == 1


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
