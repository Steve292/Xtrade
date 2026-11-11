"""Health monitor: what counts as a problem, and when it alerts."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from health_monitor import Alerter, Watchdog, find_problems

OK = ["[XAUUSDc] [2026-11-05 11:15:00] No signal", "[BTCUSDc] [2026-11-05 11:15:00] SIGNAL LONG rejected"]


def test_healthy_run_has_no_problems():
    assert find_problems(OK, 30, 100) == []


def test_open_position_with_no_signal_lines_is_not_a_problem():
    assert find_problems(["  [fixed risk] combined balance $4.80 -> $0.80/trade"], 30, 100) == []


def test_stale_log_mt5_errors_and_crashes_are_caught():
    assert "stopped" in find_problems(OK, 3600, 100)[0]
    assert find_problems(OK, None, 100)
    assert "can't read MT5" in find_problems(["[BTCUSDc] Error: copy_rates returned no data"], 30, 100)[0]
    assert "crashing" in find_problems(["Traceback (most recent call last):"], 30, 100)[0]


def test_stale_shadow_is_caught_and_a_missing_ledger_is_not():
    assert "shadow" in find_problems(OK, 30, 3600)[0]
    assert find_problems(OK, 30, None) == []


def test_a_brief_blip_never_alerts():
    a = Alerter()
    assert a.update(["x"], 0) is None and a.update(["x"], 600) is None
    assert a.update([], 700) is None


def test_alerts_after_15_minutes_repeats_hourly_then_recovers():
    a = Alerter()
    a.update(["x"], 0)
    assert "Down 15 min" in a.update(["x"], 900)
    assert a.update(["x"], 1500) is None
    assert "Down 75 min" in a.update(["x"], 4500)
    assert "Recovered" in a.update([], 4600)
    assert a.update([], 4700) is None


MT5_DOWN = ["live bot can't read MT5"]


def test_watchdog_waits_10_minutes_and_ignores_other_problems():
    w = Watchdog()
    assert not w.due(MT5_DOWN, 0) and not w.due(MT5_DOWN, 500)
    assert w.due(MT5_DOWN, 600)
    assert not Watchdog().due(["shadow tracker has stopped completing passes"], 10_000)


def test_watchdog_spaces_restarts_and_gives_up_after_three():
    w = Watchdog()
    w.due(MT5_DOWN, 0)
    t = 600
    for _ in range(3):
        assert w.due(MT5_DOWN, t)
        w.record(t)
        assert not w.due(MT5_DOWN, t + 60)       # 30 min spacing
        t += 1800
    assert not w.due(MT5_DOWN, t) and w.gave_up


def test_watchdog_resets_once_mt5_recovers():
    w = Watchdog()
    w.due(MT5_DOWN, 0); w.record(600)
    assert not w.due([], 700)
    assert not w.due(MT5_DOWN, 800)              # a fresh 10-minute wait
