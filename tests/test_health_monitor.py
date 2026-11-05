"""Health monitor: what counts as a problem, and when it alerts."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from health_monitor import Alerter, find_problems

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
