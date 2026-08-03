"""
Tests for CapitalGuard — the daily-loss/drawdown circuit breaker. No network.

Run directly (`python tests/test_capital_guard.py`) or under pytest.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.capital_guard import CapitalGuard, OpenRisk, trading_day


def _tmp_state_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".json")
    import os
    os.close(fd)
    p = Path(name)
    p.unlink()  # load() should behave fine against a path that doesn't exist yet
    return p


TODAY = date(2026, 7, 14)


def test_fresh_guard_starts_with_no_state():
    g = CapitalGuard()
    assert g.day_start_balance is None and g.peak_balance is None and not g.halted


def test_daily_loss_halts():
    g = CapitalGuard(max_daily_loss_pct=3.0)
    g.update(100.0, TODAY)
    g.update(96.0, TODAY)  # -4%, past the 3% limit
    assert g.halted and "Daily loss" in g.halt_reason


def test_drawdown_halts():
    g = CapitalGuard(max_drawdown_pct=10.0)
    g.update(100.0, TODAY)
    g.update(120.0, TODAY)  # new peak
    g.update(107.0, TODAY)  # -10.83% from peak, past the 10% limit
    assert g.halted and "drawdown" in g.halt_reason


def test_peak_rebases_every_clock_hour():
    # At explicit user request: peak/day-start now rebase hourly rather than
    # on the trading-day rollover — even a real, large drawdown gets forgiven
    # once the next hour bucket starts.
    g = CapitalGuard(max_drawdown_pct=10.0)
    t0 = 1_700_000_000.0  # exact hour boundary
    g.update(100.0, TODAY, now_ts=t0)
    g.update(150.0, TODAY, now_ts=t0 + 60)  # peak=150, same hour
    assert g.peak_balance == 150.0

    g.update(120.0, TODAY, now_ts=t0 + 3600)  # next clock hour — peak rebases to today's balance
    assert g.peak_balance == 120.0
    assert not g.halted  # would have been a -20% "drawdown" against the stale peak


def test_peak_still_ratchets_up_within_the_same_hour_after_rebase():
    g = CapitalGuard(max_drawdown_pct=10.0)
    t0 = 1_700_000_000.0
    g.update(100.0, TODAY, now_ts=t0)
    g.update(80.0, TODAY, now_ts=t0 + 3600)  # new hour: peak rebases to 80
    g.update(90.0, TODAY, now_ts=t0 + 3600 + 60)  # same hour, new intraday high
    assert g.peak_balance == 90.0


def test_peak_does_not_rebase_within_the_same_clock_hour():
    g = CapitalGuard(max_drawdown_pct=10.0)
    t0 = float(1_700_000_000 // 3600 * 3600)  # exact hour boundary, so +1800/+3000 can't cross it
    g.update(100.0, TODAY, now_ts=t0)
    g.update(120.0, TODAY, now_ts=t0 + 1800)  # new peak, same hour
    g.update(107.0, TODAY, now_ts=t0 + 3000)  # -10.83% from peak, still same hour
    assert g.halted and "drawdown" in g.halt_reason  # not forgiven mid-hour


def test_can_open_new_trade_respects_halt_and_caps():
    g = CapitalGuard(max_concurrent_trades=2, max_concurrent_open_risk_pct=3.0)
    g.update(100.0, TODAY)
    allowed, _ = g.can_open_new_trade([], 1.0)
    assert allowed
    allowed, reason = g.can_open_new_trade([OpenRisk(1, 1.0), OpenRisk(2, 1.0)], 1.0)
    assert not allowed and "concurrent trades" in reason
    allowed, reason = g.can_open_new_trade([OpenRisk(1, 2.5)], 1.0)
    assert not allowed and "open risk" in reason


def test_save_and_load_roundtrip():
    path = _tmp_state_path()
    try:
        g = CapitalGuard.load(path, max_daily_loss_pct=3.0)
        g.update(100.0, TODAY)
        g.update(95.0, TODAY)

        reloaded = CapitalGuard.load(path, max_daily_loss_pct=3.0)
        assert reloaded.day_start_balance == 100.0
        assert reloaded.peak_balance == 100.0
        assert reloaded.current_day == TODAY
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_file_starts_fresh():
    path = _tmp_state_path()  # never created
    g = CapitalGuard.load(path)
    assert g.day_start_balance is None and g.peak_balance is None


def test_config_thresholds_come_from_caller_not_saved_state():
    # A config change (e.g. tightening max_daily_loss_pct) must take effect
    # immediately, even though the saved file only carries balance/day state.
    path = _tmp_state_path()
    try:
        CapitalGuard.load(path, max_daily_loss_pct=3.0).update(100.0, TODAY)
        reloaded = CapitalGuard.load(path, max_daily_loss_pct=1.0)  # tightened
        assert reloaded.max_daily_loss_pct == 1.0
    finally:
        path.unlink(missing_ok=True)


def test_persisted_guard_catches_loss_across_a_restart():
    # The real bug: this bot restarts often (every deploy). Without
    # persistence, a fresh CapitalGuard() resets day_start_balance to
    # whatever balance it first sees — so a loss that happened before the
    # restart is invisible to it. load() must fix this.
    path = _tmp_state_path()
    try:
        before_restart = CapitalGuard.load(path, max_daily_loss_pct=3.0)
        before_restart.update(100.0, TODAY)  # day starts at $100

        # "restart" -> a brand new process, but loads the same saved state
        after_restart = CapitalGuard.load(path, max_daily_loss_pct=3.0)
        after_restart.update(90.0, TODAY)  # now down 10% from the TRUE day start
        assert after_restart.halted
        assert "Daily loss" in after_restart.halt_reason
    finally:
        path.unlink(missing_ok=True)


def test_unpersisted_guard_misses_the_same_loss_after_a_restart():
    # Contrast case, proving the fix matters: the plain constructor (the old
    # behavior — what hypertrade.py used before this fix) has no memory of
    # the pre-restart balance, so the identical 10% decline goes undetected.
    before_restart = CapitalGuard(max_daily_loss_pct=3.0)
    before_restart.update(100.0, TODAY)

    after_restart = CapitalGuard(max_daily_loss_pct=3.0)  # fresh instance, no load()
    after_restart.update(90.0, TODAY)  # its OWN day_start becomes 90 -> sees 0% change
    assert not after_restart.halted


def test_weekly_loss_halts_with_48h_cooldown():
    # Daily/DD thresholds loosened so only the weekly check can trip.
    g = CapitalGuard(max_daily_loss_pct=100.0, max_drawdown_pct=100.0, max_weekly_loss_pct=6.0)
    t0 = 1_700_000_000.0
    g.update(100.0, TODAY, now_ts=t0)
    g.update(93.0, TODAY, now_ts=t0 + 3600)  # -7%, past the 6% weekly limit
    assert g.halted and "Weekly loss" in g.halt_reason


def test_weekly_cooldown_stays_halted_even_after_a_full_recovery():
    g = CapitalGuard(max_daily_loss_pct=100.0, max_drawdown_pct=100.0, max_weekly_loss_pct=6.0)
    t0 = 1_700_000_000.0
    g.update(100.0, TODAY, now_ts=t0)
    g.update(93.0, TODAY, now_ts=t0 + 3600)  # trips halt, 48h cooldown starts
    almost_there = t0 + 3600 + 48 * 3600 - 1
    g.update(150.0, TODAY, now_ts=almost_there)  # big recovery, but cooldown hasn't expired yet
    assert g.halted and "Weekly loss" in g.halt_reason


def test_weekly_cooldown_clears_once_expired_and_balance_is_healthy():
    g = CapitalGuard(max_daily_loss_pct=100.0, max_drawdown_pct=100.0, max_weekly_loss_pct=6.0)
    t0 = 1_700_000_000.0
    g.update(100.0, TODAY, now_ts=t0)
    g.update(93.0, TODAY, now_ts=t0 + 3600)  # trips halt
    later = t0 + 3600 + 48 * 3600 + 1  # just past the cooldown
    g.update(100.0, TODAY, now_ts=later)  # recovered AND cooldown expired
    assert not g.halted


def test_vix_spike_halts():
    g = CapitalGuard(vix_halt_threshold=30.0)
    g.update(100.0, TODAY, vix=35.0)
    assert g.halted and "VIX" in g.halt_reason


def test_vix_below_threshold_does_not_halt():
    g = CapitalGuard(vix_halt_threshold=30.0)
    g.update(100.0, TODAY, vix=18.0)
    assert not g.halted


def test_correlated_btc_spx_crash_halts_with_24h_cooldown():
    g = CapitalGuard(correlated_crash_btc_pct=-5.0, correlated_crash_spx_pct=-3.0)
    g.update(100.0, TODAY, btc_24h_change_pct=-6.0, spx_24h_change_pct=-4.0)
    assert g.halted and "Correlated crash" in g.halt_reason


def test_correlated_crash_requires_both_legs_down():
    g = CapitalGuard(correlated_crash_btc_pct=-5.0, correlated_crash_spx_pct=-3.0)
    g.update(100.0, TODAY, btc_24h_change_pct=-6.0, spx_24h_change_pct=-1.0)  # SPX not down enough
    assert not g.halted


def test_liquidity_shock_wide_spread_halts():
    g = CapitalGuard(liquidity_shock_spread_pct=0.1)
    g.update(100.0, TODAY, spread_pct=0.15)
    assert g.halted and "Liquidity shock" in g.halt_reason


def test_liquidity_shock_volume_collapse_halts():
    g = CapitalGuard(liquidity_shock_volume_drop_pct=-50.0)
    g.update(100.0, TODAY, volume_change_24h_pct=-60.0)
    assert g.halted and "Liquidity shock" in g.halt_reason


def test_three_consecutive_losses_halve_size():
    g = CapitalGuard(consecutive_loss_limit=3)
    assert g.size_multiplier == 1.0
    g.record_trade_result(won=False)
    g.record_trade_result(won=False)
    assert g.size_multiplier == 1.0  # only 2 losses so far
    g.record_trade_result(won=False)
    assert g.size_multiplier == 0.5


def test_two_consecutive_wins_restore_size_after_halving():
    g = CapitalGuard(consecutive_loss_limit=3)
    for _ in range(3):
        g.record_trade_result(won=False)
    assert g.size_multiplier == 0.5
    g.record_trade_result(won=True)
    assert g.size_multiplier == 0.5  # only 1 win so far
    g.record_trade_result(won=True)
    assert g.size_multiplier == 1.0


def test_a_loss_between_wins_resets_restore_progress():
    g = CapitalGuard(consecutive_loss_limit=3)
    for _ in range(3):
        g.record_trade_result(won=False)
    g.record_trade_result(won=True)  # win streak = 1
    g.record_trade_result(won=False)  # breaks the streak; loss streak = 1
    g.record_trade_result(won=True)  # win streak = 1 again, not 2
    assert g.size_multiplier == 0.5  # still halved: never got 2 CONSECUTIVE wins


def test_profit_lock_triggers_at_first_milestone():
    g = CapitalGuard(profit_lock_trigger_pct=20.0, profit_lock_fraction=0.10)
    g.update(100.0, TODAY)  # sets baseline_balance = 100
    amount = g.check_profit_lock(121.0)  # +21% since baseline
    assert amount is not None and abs(amount - 12.1) < 1e-9  # 10% of 121


def test_profit_lock_does_not_refire_within_the_same_milestone():
    g = CapitalGuard(profit_lock_trigger_pct=20.0, profit_lock_fraction=0.10)
    g.update(100.0, TODAY)
    assert g.check_profit_lock(121.0) is not None
    assert g.check_profit_lock(122.0) is None  # still the same 20%-39% band


def test_profit_lock_fires_again_at_the_next_milestone():
    g = CapitalGuard(profit_lock_trigger_pct=20.0, profit_lock_fraction=0.10)
    g.update(100.0, TODAY)
    g.check_profit_lock(121.0)  # milestone 1 (>=20%)
    assert g.check_profit_lock(141.0) is not None  # +41% -> milestone 2 (>=40%)


def test_profit_lock_returns_none_when_not_configured():
    g = CapitalGuard()
    g.update(100.0, TODAY)
    assert g.check_profit_lock(200.0) is None


def test_profit_lock_returns_none_at_a_loss():
    g = CapitalGuard(profit_lock_trigger_pct=20.0)
    g.update(100.0, TODAY)
    assert g.check_profit_lock(80.0) is None


def test_section7_state_persists_across_a_restart():
    path = _tmp_state_path()
    try:
        g = CapitalGuard.load(
            path, max_weekly_loss_pct=6.0, consecutive_loss_limit=3, profit_lock_trigger_pct=20.0
        )
        g.update(100.0, TODAY)
        g.record_trade_result(won=False)
        g.record_trade_result(won=False)
        g.record_trade_result(won=False)  # halved
        g.check_profit_lock(125.0)  # locks a milestone

        reloaded = CapitalGuard.load(
            path, max_weekly_loss_pct=6.0, consecutive_loss_limit=3, profit_lock_trigger_pct=20.0
        )
        assert reloaded.size_multiplier == 0.5
        assert reloaded.baseline_balance == 100.0
        assert reloaded.profit_locks_triggered == 1
        assert reloaded.locked_amount > 0
    finally:
        path.unlink(missing_ok=True)


# ---- trading_day() — 5pm ET market-close rollover, not local midnight -----

def test_trading_day_before_5pm_et_is_same_calendar_date():
    assert trading_day(datetime(2026, 7, 28, 16, 59)) == date(2026, 7, 28)


def test_trading_day_rolls_over_exactly_at_5pm_et():
    assert trading_day(datetime(2026, 7, 28, 17, 0)) == date(2026, 7, 29)


def test_trading_day_after_5pm_et_is_next_calendar_date():
    assert trading_day(datetime(2026, 7, 28, 23, 0)) == date(2026, 7, 29)


def test_trading_day_early_morning_is_still_labeled_todays_session():
    # 2am on the 28th is within the session that opened the 27th at 5pm and
    # runs until the 28th at 5pm — still labeled the 28th (hasn't rolled yet).
    assert trading_day(datetime(2026, 7, 28, 2, 0)) == date(2026, 7, 28)


def test_trading_day_defaults_to_now_when_unspecified():
    # Just confirm it runs without error and returns a real date — the exact
    # value is time-dependent, not worth pinning down here.
    assert isinstance(trading_day(), date)


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
