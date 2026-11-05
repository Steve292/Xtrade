#!/usr/bin/env python3
"""
Alerts when the bot stops working. Reads logs and shadow state only: no MT5
connection (that bridge is the fragile part) and no way to touch a trade.

    python scripts/health_monitor.py          # the launchd service
    python scripts/health_monitor.py --test   # send one test notification
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_LOG = ROOT / "logs" / "mt5-autotrader.log"
SHADOW_STATE = ROOT / "shadow_state.json"
CHECK_SECONDS = 60
LOG_STALE_SECONDS = 15 * 60      # the loop writes every ~30s
SHADOW_STALE_SECONDS = 20 * 60   # it passes every 5 min
ALERT_AFTER_SECONDS = 15 * 60
REPEAT_SECONDS = 60 * 60


def find_problems(tail: list, log_age: float | None, shadow_age: float | None) -> list:
    """What's wrong right now. Not a timestamp check on signal lines: the loop
    legitimately prints none while a position is open."""
    problems = []
    if log_age is None or log_age > LOG_STALE_SECONDS:
        problems.append("live bot has stopped writing its log")
    if any("Traceback (most recent call last)" in line for line in tail[-40:]):
        problems.append("live bot is crashing")
    btc = [line for line in tail if line.startswith("[BTCUSDc]")]
    if btc and btc[-1].startswith("[BTCUSDc] Error"):
        problems.append("live bot can't read MT5")
    if shadow_age is not None and shadow_age > SHADOW_STALE_SECONDS:
        problems.append("shadow tracker has stopped completing passes")
    return problems


class Alerter:
    """Waits out brief blips, then alerts, repeats, and reports recovery."""

    def __init__(self):
        self.since = None
        self.last_alert = None

    def update(self, problems: list, now: float) -> str | None:
        if not problems:
            was_alerted = self.last_alert is not None
            self.since = self.last_alert = None
            return "Recovered: everything is running again." if was_alerted else None
        if self.since is None:
            self.since = now
        if now - self.since < ALERT_AFTER_SECONDS:
            return None
        if self.last_alert is None or now - self.last_alert >= REPEAT_SECONDS:
            self.last_alert = now
            return f"Down {int((now - self.since) / 60)} min: " + "; ".join(problems)
        return None


def notify(message: str) -> None:
    text = message.replace("\\", "").replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "TraderX" sound name "Basso"'],
                   check=False, timeout=20)


def _log_state() -> tuple:
    if not LIVE_LOG.exists():
        return [], None
    age = time.time() - LIVE_LOG.stat().st_mtime
    with LIVE_LOG.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 16000))
        return f.read().decode(errors="ignore").splitlines(), age


def _shadow_age() -> float | None:
    try:
        last = json.loads(SHADOW_STATE.read_text())["last_pass"]
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    except (OSError, ValueError, KeyError):
        return None


def main() -> int:
    if "--test" in sys.argv:
        notify("Health monitor is on. This is a test alert.")
        return 0
    alerter = Alerter()
    print("[monitor] started", flush=True)
    while True:
        try:
            tail, age = _log_state()
            message = alerter.update(find_problems(tail, age, _shadow_age()), time.time())
            if message:
                print(f"[monitor] {datetime.now():%m-%d %H:%M} {message}", flush=True)
                notify(message)
        except Exception as e:
            print(f"[monitor] check failed ({type(e).__name__}: {str(e)[:120]})", flush=True)
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
