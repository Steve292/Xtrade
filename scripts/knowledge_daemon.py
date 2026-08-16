#!/usr/bin/env python
"""Continuous knowledge ingestion with adaptive backoff.

    python scripts/knowledge_daemon.py                 # run forever
    python scripts/knowledge_daemon.py --once          # a single cycle
    python scripts/knowledge_daemon.py --status        # read the state file

WHY THIS IS NOT A SLEEP LOOP
----------------------------
YouTube rate-limits this IP. Three consecutive manual runs went 96 videos
ingested -> 13 -> 0, each ending in the pipeline's own circuit breaker
("10 consecutive failures ... stopping rather than hammering the source").
A fixed-interval loop would keep walking into that and make it worse -- the
failure mode of a naive ingester is getting the address blocked outright.

So the interval responds to what actually happened:

    ingested new material   -> BASE interval (6h)
    nothing new, no abort   -> BASE x 2, the corpus is simply current
    rate-limited (ABORTED)  -> exponential backoff, 1h -> 2 -> 4 -> 8 -> 12h cap

Backoff resets the moment a cycle ingests anything. State lives in
knowledge_daemon_state.json next to the corpus, so a restart does not reset
the backoff and start hammering again from zero.

The ingest itself is unchanged and still incremental: re-running only fetches
what is new, human channel confirmations still gate what may be touched, and
nothing here writes trading config.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "knowledge_daemon_state.json"
LOG = ROOT / "logs" / "knowledge-daemon.log"

BASE_HOURS = 6.0
BACKOFF_START_HOURS = 1.0
BACKOFF_MAX_HOURS = 12.0
MIN_INTERVAL_HOURS = 0.5   # hard floor; nothing may schedule tighter than this


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"{_now()}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"backoff_hours": 0.0, "cycles": 0, "last_run": None,
                "last_result": None, "total_ingested": 0}


def save_state(s: dict) -> None:
    try:
        STATE.write_text(json.dumps(s, indent=2))
    except OSError as exc:
        log(f"WARN could not write state: {exc}")


def run_cycle(python: str) -> dict:
    """One ingest pass. Returns a parsed summary; never raises."""
    cmd = [python, str(ROOT / "scripts" / "knowledge_ingest.py"), "ingest"]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=60 * 90)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return {"ingested": 0, "aborted": True, "reason": "timeout after 90m"}
    except Exception as exc:                      # noqa: BLE001 - must not die
        return {"ingested": 0, "aborted": True, "reason": f"{type(exc).__name__}: {exc}"}

    aborted = "ABORTED" in out
    ingested = 0
    m = re.search(r"ingested=(\d+)", out)
    if m:
        ingested = int(m.group(1))
    considered = 0
    m2 = re.search(r"considered=(\d+)", out)
    if m2:
        considered = int(m2.group(1))
    cands = None
    m3 = re.search(r"candidates: (\d+) above", out)
    if m3:
        cands = int(m3.group(1))
    return {"ingested": ingested, "considered": considered, "aborted": aborted,
            "candidates": cands, "reason": "rate-limited" if aborted else "ok"}


def sync_weights(python: str) -> dict:
    """Push a grown corpus into the derived weights, gated on tests.

    This is the ONLY thing that carries ingested knowledge across into code
    that runs. Transcripts alone change nothing -- the corpus is gitignored and
    nothing on the execute path reads it. So the crossing is automated here,
    and sync_weights.py refuses to commit unless the weights actually moved and
    the tests still pass afterwards. It never pushes and never merges.
    """
    cmd = [python, str(ROOT / "scripts" / "sync_weights.py"),
           "--apply", "--commit"]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=60 * 30)
        out = (p.stdout or "") + (p.stderr or "")
    except Exception as exc:                      # noqa: BLE001
        return {"changed": False, "note": f"{type(exc).__name__}: {exc}"}

    if "Nothing to do" in out:
        return {"changed": False, "note": "no drift"}
    if "TESTS FAILED" in out:
        return {"changed": False, "note": "TESTS FAILED — weights reverted"}
    if "committed to the current branch" in out:
        moved = re.findall(r"^\s{2}(\w+)\s+[\d.-]+\s*->\s*([\d.]+)", out, re.M)
        return {"changed": True,
                "note": "committed: " + ", ".join(f"{k}->{v}" for k, v in moved[:6])}
    return {"changed": False, "note": out.strip().splitlines()[-1][:120] if out.strip() else "no output"}


def walk_forward(python: str) -> dict:
    """Re-test candidate configurations against real MT5 history.

    Runs after new knowledge lands so a grown corpus is MEASURED against real
    outcomes rather than assumed to help. Reports only -- promotion still
    requires clearing the four gates in walk_forward.py, and every rejection is
    written to the ledger alongside the passes. A search that records only its
    winners is how overfitting comes to look like progress.
    """
    script = ROOT / "scripts" / "walk_forward.py"
    if not (ROOT / "var" / "mt5" / "gate_split.json").exists():
        return {"ran": False, "note": "no replay data yet"}
    try:
        p = subprocess.run([python, str(script), "--report"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60 * 20)
        out = (p.stdout or "") + (p.stderr or "")
    except Exception as exc:                      # noqa: BLE001
        return {"ran": False, "note": f"{type(exc).__name__}: {exc}"}

    best = re.search(r"best win rate found\s*:\s*([\d.]+)% on (\d+)", out)
    target = re.search(r"reaching [\d.]+% target\s*:\s*(\S+)", out)
    passing = re.search(r"passing the full gate\s*:\s*(\d+)", out)
    return {
        "ran": True,
        "best_win": float(best.group(1)) if best else None,
        "best_n": int(best.group(2)) if best else None,
        "passing": int(passing.group(1)) if passing else 0,
        "target_met": bool(target and target.group(1).startswith("YES")),
    }


def next_interval(res: dict, state: dict) -> float:
    """Hours until the next cycle, and update the backoff in `state`."""
    if res["aborted"]:
        cur = state.get("backoff_hours", 0.0) or 0.0
        nxt = BACKOFF_START_HOURS if cur <= 0 else min(cur * 2, BACKOFF_MAX_HOURS)
        state["backoff_hours"] = nxt
        return max(nxt, MIN_INTERVAL_HOURS)
    # a clean cycle clears the penalty
    state["backoff_hours"] = 0.0
    if res["ingested"] > 0:
        return BASE_HOURS
    return BASE_HOURS * 2      # nothing new; the corpus is current


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--python", default=str(Path(sys.executable)),
                    help="interpreter used to run the ingest")
    a = ap.parse_args()

    if a.status:
        s = load_state()
        print(json.dumps(s, indent=2))
        return 0

    log(f"daemon start (base={BASE_HOURS}h, backoff cap={BACKOFF_MAX_HOURS}h)")
    while True:
        state = load_state()
        state["cycles"] = state.get("cycles", 0) + 1
        state["last_run"] = _now()

        res = run_cycle(a.python)
        state["last_result"] = res
        state["total_ingested"] = state.get("total_ingested", 0) + res["ingested"]

        # Only when the corpus actually grew. Re-deriving against an unchanged
        # corpus produces identical weights and would just add noise.
        if res["ingested"] > 0:
            sync = sync_weights(a.python)
            state["last_weight_sync"] = sync
            log(f"  weight sync: {sync['note']}")
            if sync["changed"]:
                state["weight_updates"] = state.get("weight_updates", 0) + 1

            wf = walk_forward(a.python)
            state["last_walk_forward"] = wf
            if wf["ran"]:
                log(f"  walk-forward: best {wf['best_win']}% on {wf['best_n']} "
                    f"trades, {wf['passing']} config(s) passed the gate, "
                    f"90% target {'MET' if wf['target_met'] else 'not met'}")
            else:
                log(f"  walk-forward: skipped ({wf['note']})")

        wait = next_interval(res, state)
        save_state(state)

        log(f"cycle {state['cycles']}: ingested={res['ingested']} "
            f"considered={res.get('considered', 0)} "
            f"candidates={res.get('candidates')} "
            f"{'RATE-LIMITED' if res['aborted'] else 'ok'} "
            f"-> next in {wait:.1f}h "
            f"(cumulative {state['total_ingested']}, "
            f"weight updates {state.get('weight_updates', 0)})")

        if a.once:
            return 0
        time.sleep(wait * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
