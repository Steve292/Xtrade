#!/usr/bin/env python
"""Continuously evaluate candidate configurations against real MT5 history.

    python scripts/walk_forward.py --report          # evaluate, print, change nothing
    python scripts/walk_forward.py --promote         # also record a winner if one qualifies
    python scripts/walk_forward.py --target-win 90   # what would it take?

Runs after every ingest that grows the corpus, so new knowledge is retested
against real outcomes rather than assumed to help.

WHY THIS REFUSES TO CHASE A WIN-RATE TARGET
-------------------------------------------
Searching filter combinations until one hits a target ALWAYS succeeds. Slice
3,547 trades finely enough and some 12-trade cohort wins 90% of the time. That
is arithmetic, not edge, and promoting it would arm a live account on a
coincidence.

An exhaustive search over this history has already been run. The ceiling is
67.0% on 385 trades, and 70% is unreachable by any combination -- so a 90%
result appearing here would be evidence of a bug or of the sample having
shrunk, not of a discovery. The gate below is built to reject it either way.

PROMOTION GATE — all four, or it is not promoted
------------------------------------------------
  1. n >= MIN_TRADES (30). The floor bot/knowledge/verify.py already uses.
  2. beats the incumbent in BOTH chronological halves, not on the total.
  3. the 95% binomial lower bound on its win rate clears the unfiltered
     baseline -- an observed rate whose interval includes the baseline has
     not demonstrated anything.
  4. second-half win rate within DEGRADE_TOL of the first. A cohort that
     halves out of sample was fitted to the first half.

Everything is appended to walk_forward_ledger.json, INCLUDING rejections and
why. A search that only records its winners is how overfitting looks like
progress.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "walk_forward_ledger.json"

MIN_TRADES = 30
DEGRADE_TOL = 10.0     # percentage points of win rate


def _load(out: Path) -> pd.DataFrame:
    g = pd.read_json(out / "gate_split.json")
    r = pd.read_json(out / "replay.json")
    # Positional alignment, NOT a key merge: 159 positions share an
    # (open_dt, symbol) pair and joining on those fans 3,547 rows out to 3,953,
    # inflating every count.
    key = ["open_dt", "symbol", "net"]
    g = g.sort_values(key).reset_index(drop=True)
    r = r.sort_values(key).reset_index(drop=True)
    if len(g) != len(r):
        raise SystemExit(f"row mismatch {len(g)} vs {len(r)} — re-run replay")
    df = g.copy()
    df["bot_dir"] = r["bot_dir"].values
    df = df.sort_values("open_dt").reset_index(drop=True)
    df["dir_match"] = df.bot_dir == df.dir
    df["win"] = df.net > 0
    return df


def wilson_lower(wins: int, n: int) -> float:
    if n == 0:
        return 0.0
    z, p = 1.96, wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (c - m)


def pf(net: np.ndarray) -> float:
    gl = -net[net < 0].sum()
    return float(net[net > 0].sum() / gl) if gl > 0 else float("inf")


def candidates(df: pd.DataFrame) -> list[tuple[str, np.ndarray]]:
    atoms = [
        ("veto", df.veto_ok.to_numpy(bool)),
        ("gates", df["A+B_all"].to_numpy(bool)),
        ("dir_match", df.dir_match.to_numpy(bool)),
        ("metals", df.symbol.isin(["XAUUSDc", "XAGUSDc"]).to_numpy(bool)),
    ]
    out = []
    for k in range(1, len(atoms) + 1):
        for combo in itertools.combinations(atoms, k):
            mask = np.ones(len(df), dtype=bool)
            for _, m in combo:
                mask &= m
            out.append((" + ".join(a for a, _ in combo), mask))
    return out


def evaluate(df: pd.DataFrame, name: str, mask: np.ndarray, baseline: float) -> dict:
    n = int(mask.sum())
    sub = df[mask]
    if n == 0:
        return {"name": name, "n": 0, "ok": False, "reason": "empty"}
    mid = len(df) // 2
    a, b = df[mask & (df.index < mid)], df[mask & (df.index >= mid)]
    wr = 100 * sub.win.mean()
    lower = wilson_lower(int(sub.win.sum()), n)
    wa = 100 * a.win.mean() if len(a) else 0.0
    wb = 100 * b.win.mean() if len(b) else 0.0

    reasons = []
    if n < MIN_TRADES:
        reasons.append(f"n={n} below the {MIN_TRADES}-trade floor")
    if len(a) < 5 or len(b) < 5:
        reasons.append("too few trades in one half to split-test")
    elif not (wa > baseline and wb > baseline):
        reasons.append(f"does not beat baseline in both halves ({wa:.1f} / {wb:.1f})")
    if lower <= baseline:
        reasons.append(f"95% lower bound {lower:.1f}% does not clear baseline {baseline:.1f}%")
    if len(a) >= 5 and len(b) >= 5 and (wa - wb) > DEGRADE_TOL:
        reasons.append(f"degrades {wa - wb:.1f}pp out of sample")

    return {"name": name, "n": n, "win": round(wr, 2),
            "lower95": round(lower, 2), "pf": round(pf(sub.net.to_numpy(float)), 4),
            "first_half": round(wa, 2), "second_half": round(wb, 2),
            "ok": not reasons, "reason": "; ".join(reasons) or "passes all gates"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="var/mt5")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--target-win", type=float, default=90.0)
    a = ap.parse_args()

    out = Path(a.out)
    if not out.is_absolute():
        out = ROOT / out
    if not (out / "gate_split.json").exists():
        print(f"no replay data in {out} — run scripts/gate_split.py first",
              file=sys.stderr)
        return 2

    df = _load(out)
    baseline = 100 * df.win.mean()
    print(f"{len(df)} positions, unfiltered win rate {baseline:.2f}%\n")

    results = [evaluate(df, n, m, baseline) for n, m in candidates(df)]
    results = [r for r in results if r["n"] > 0]
    results.sort(key=lambda r: -r["win"])

    print(f"{'configuration':<34}{'n':>6}{'win%':>8}{'95%lo':>8}{'1st':>7}{'2nd':>7}  verdict")
    print("-" * 100)
    for r in results:
        mark = "PASS" if r["ok"] else "reject"
        print(f"{r['name']:<34}{r['n']:>6}{r['win']:>7.1f}%{r['lower95']:>7.1f}%"
              f"{r['first_half']:>6.1f}%{r['second_half']:>6.1f}%  {mark}: {r['reason'][:44]}")

    passing = [r for r in results if r["ok"]]
    best = results[0]
    hit_target = [r for r in passing if r["win"] >= a.target_win]

    print("\n" + "=" * 100)
    print(f"best win rate found      : {best['win']:.1f}% on {best['n']} trades ({best['name']})")
    print(f"passing the full gate    : {len(passing)}"
          + (f" -> {passing[0]['name']} at {passing[0]['win']:.1f}%" if passing else ""))
    print(f"reaching {a.target_win:.0f}% target      : "
          f"{'YES — ' + hit_target[0]['name'] if hit_target else 'NO'}")
    if not hit_target:
        print(f"  Nothing reached {a.target_win:.0f}% while clearing the gate. On this history the")
        print("  ceiling is ~67% and 70% is unreachable by any combination, so a passing")
        print("  result at 90% would indicate a bug or a collapsed sample, not a discovery.")

    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "positions": len(df), "baseline_win": round(baseline, 2),
        "target": a.target_win, "target_met": bool(hit_target),
        "best": best, "passing": passing, "all": results,
    }
    if a.promote or a.report:
        led = []
        if LEDGER.exists():
            try:
                led = json.loads(LEDGER.read_text())
            except ValueError:
                led = []
        led.append(entry)
        LEDGER.write_text(json.dumps(led[-50:], indent=2))
        print(f"\nappended to {LEDGER.name} "
              f"({len(passing)} passing, {len(results) - len(passing)} rejected — "
              f"rejections recorded too)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
