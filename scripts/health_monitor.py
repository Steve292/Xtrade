#!/usr/bin/env python3
"""
Alerts when the bot stops working. Health is judged from logs and shadow state;
MT5 is only probed (read-only) before a watchdog restart. No way to touch a trade.

    python scripts/health_monitor.py          # the launchd service
    python scripts/health_monitor.py --test   # send one test notification

Also the MT5 watchdog: if MT5 stays unreadable for 10 minutes and a direct
probe confirms it, it restarts the terminal and bridge (at most 3 times in 6h).
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


MT5_PROBLEMS = ("live bot can't read MT5", "live bot is crashing")
RESTART_AFTER_SECONDS = 10 * 60
RESTART_SPACING_SECONDS = 30 * 60
MAX_RESTARTS = 3                 # per window -- then stop and leave it to a human
RESTART_WINDOW_SECONDS = 6 * 60 * 60
PROBE = r"""
import sys
from mt5linux import MetaTrader5
m = MetaTrader5(host="127.0.0.1", port=18812)
sys.exit(0 if m.initialize() and m.terminal_info() and m.terminal_info().connected else 1)
"""


class Watchdog:
    """Decides when a frozen MT5 needs a restart. The terminal can hang while
    its bridge process stays up -- launchd sees nothing wrong, and the bot sat
    on 'No IPC connection' for 13 hours (2026-11-10). Waits RESTART_AFTER, the
    caller confirms with a direct probe, and repeated failures stop after
    MAX_RESTARTS rather than restarting forever."""

    def __init__(self):
        self.since = None
        self.restarts = []
        self.gave_up = False

    def due(self, problems: list, now: float) -> bool:
        if not any(p in MT5_PROBLEMS for p in problems):
            self.since = None
            self.gave_up = False
            return False
        if self.since is None:
            self.since = now
        self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW_SECONDS]
        if now - self.since < RESTART_AFTER_SECONDS or self.gave_up:
            return False
        if self.restarts and now - self.restarts[-1] < RESTART_SPACING_SECONDS:
            return False
        if len(self.restarts) >= MAX_RESTARTS:
            self.gave_up = True
            return False
        return True

    def record(self, now: float) -> None:
        self.restarts.append(now)


def _python() -> str:
    return str(ROOT / "venv" / "bin" / "python")


def probe_mt5(timeout: float = 60) -> bool:
    try:
        return subprocess.run([_python(), "-I", "-c", PROBE], timeout=timeout,
                              capture_output=True).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def restart_mt5() -> bool:
    """Stop the frozen terminal, restart the bridge, let the next connection
    relaunch the terminal, then restart the live loop so it reconnects (it
    does not on its own). Server-side stop-losses and take-profits are not
    affected by the terminal closing."""
    uid = str(subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip())
    subprocess.run(["pkill", "-TERM", "-f", "terminal64.exe"], check=False)
    time.sleep(8)
    subprocess.run(["pkill", "-KILL", "-f", "terminal64.exe"], check=False)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.smc.mt5bridge"], check=False)
    ok = False
    for _ in range(6):
        time.sleep(15)
        if probe_mt5():
            ok = True
            break
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.smc.mt5autotrader"], check=False)
    return ok


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
    watchdog = Watchdog()
    print("[monitor] started", flush=True)
    while True:
        try:
            tail, age = _log_state()
            problems = find_problems(tail, age, _shadow_age())
            message = alerter.update(problems, time.time())
            if message:
                print(f"[monitor] {datetime.now():%m-%d %H:%M} {message}", flush=True)
                notify(message)
            if watchdog.due(problems, time.time()):
                if probe_mt5():
                    print("[watchdog] MT5 answers a direct probe -- not restarting", flush=True)
                    watchdog.since = time.time()
                else:
                    watchdog.record(time.time())
                    ok = restart_mt5()
                    note = ("Watchdog restarted MT5 -- connected again." if ok else
                            "Watchdog restarted MT5 but it is still not answering.")
                    print(f"[watchdog] {datetime.now():%m-%d %H:%M} {note}", flush=True)
                    notify(note)
            elif watchdog.gave_up and watchdog.since is not None and not getattr(watchdog, "told", False):
                watchdog.told = True
                notify(f"Watchdog stopped after {MAX_RESTARTS} MT5 restarts in 6h. Needs a manual look.")
            if not watchdog.gave_up:
                watchdog.told = False
        except Exception as e:
            print(f"[monitor] check failed ({type(e).__name__}: {str(e)[:120]})", flush=True)
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
