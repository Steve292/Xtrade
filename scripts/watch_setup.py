#!/usr/bin/env python3
"""
Tail the live trader's log and emit only the events worth acting on.

Written for Monitor-style consumption: one line of stdout = one notification,
so this is aggressively deduplicated. The raw log writes a block per symbol
every 30s poll; forwarding that verbatim would be thousands of notifications a
day and would bury the two lines that actually matter.

Emitted always (these are the decision points):
  QUEUED   a setup cleared every gate but landed below auto_fire_pct, so it is
           sitting in the Approve/Cancel queue waiting on a human
  FIRED    an order was actually placed

Emitted on change only (state transitions, not steady state):
  GATE     the gate blocking a symbol changed — this is how you watch a setup
           walk toward qualifying without a notification every 30 seconds
  HALT     the capital guard started or stopped blocking entries

Emitted at most once per COOLDOWN seconds (recurring faults, not spam):
  FAULT    bridge failure, crash-restart, or a balance fetch that failed

Usage:  python scripts/watch_setup.py [logfile]
"""

from __future__ import annotations

import os
import re
import sys
import time

LOG = sys.argv[1] if len(sys.argv) > 1 else "logs/mt5-autotrader.log"
COOLDOWN = 600  # seconds between repeats of the same fault class

RE_QUEUED = re.compile(r"\[([A-Z0-9]+c?)\].*SIGNAL (\w+).*final (\d+)%.*QUEUED")
RE_FIRED = re.compile(r"\[([A-Z0-9]+c?)\].*(LIVE ORDER FIRED|PAPER FILL)")
RE_GATE = re.compile(r"\[([A-Z0-9]+c?)\].*SIGNAL (\w+) conf (\d+)% final (\d+)% rejected at screen: (.+?)\s*$")
RE_NOSIG = re.compile(r"\[([A-Z0-9]+c?)\].*No signal")
RE_HALT = re.compile(r"\[([A-Z0-9]+c?)\] blocked by combined ledger: (.+?)\s*$")
RE_SIZING = re.compile(r"\[sizing\] (\S+) .*capped ([\d.]+)% \(\$([\d.]+)")

FAULTS = (
    ("bridge", re.compile(r"MT5 initialize\(\) failed")),
    ("crash", re.compile(r"^Traceback")),
    ("balance", re.compile(r"balance fetch failed")),
    ("hl", re.compile(r"Hyperliquid unavailable|unreachable this pass")),
)


def emit(kind: str, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {kind:<6} {msg}", flush=True)


def follow(path: str):
    """Yield lines appended to `path`, surviving the log being rotated or the
    file being replaced by a crash-restart."""
    while not os.path.exists(path):
        time.sleep(2)
    f = open(path, "r", errors="replace")
    f.seek(0, os.SEEK_END)
    inode = os.fstat(f.fileno()).st_ino
    while True:
        line = f.readline()
        if line:
            yield line
            continue
        time.sleep(1)
        try:
            if os.stat(path).st_ino != inode:
                f.close()
                f = open(path, "r", errors="replace")
                inode = os.fstat(f.fileno()).st_ino
        except OSError:
            pass


def main() -> None:
    last_gate: dict = {}
    last_halt: dict = {}
    last_fault: dict = {}
    emit("WATCH", f"following {LOG} — reporting QUEUED / FIRED / gate changes / faults")

    for line in follow(LOG):
        m = RE_QUEUED.search(line)
        if m:
            emit("QUEUED", f"{m.group(1)} {m.group(2)} at final {m.group(3)}% "
                           f"— awaiting Approve/Cancel on the control panel")
            continue

        m = RE_FIRED.search(line)
        if m:
            emit("FIRED", f"{m.group(1)} — {m.group(2)}")
            continue

        m = RE_SIZING.search(line)
        if m:
            emit("SIZE", f"{m.group(1)} risking ${m.group(3)} ({m.group(2)}%)")
            continue

        m = RE_HALT.search(line)
        if m:
            sym, reason = m.group(1), m.group(2)
            if last_halt.get(sym) != reason:
                last_halt[sym] = reason
                emit("HALT", f"{sym} blocked — {reason}")
            continue

        m = RE_GATE.search(line)
        if m:
            sym, side, conf, final, gate = m.groups()
            key = (side, gate)
            if last_gate.get(sym) != key:
                last_gate[sym] = key
                emit("GATE", f"{sym} {side} conf {conf}% final {final}% — now blocked by: {gate}")
            continue

        m = RE_NOSIG.search(line)
        if m:
            sym = m.group(1)
            if last_gate.get(sym) is not None:
                last_gate[sym] = None
                emit("GATE", f"{sym} — setup gone, no signal")
            continue

        for name, pattern in FAULTS:
            if pattern.search(line):
                now = time.time()
                if now - last_fault.get(name, 0) > COOLDOWN:
                    last_fault[name] = now
                    emit("FAULT", f"{name}: {line.strip()[:110]}")
                break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
