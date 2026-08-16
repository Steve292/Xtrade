#!/usr/bin/env python
"""Split the optional screen gates by corpus breadth and race the halves.

    python scripts/gate_split.py --out var/mt5

The question this answers is the one the knowledge pipeline poses but has never
been able to settle on its own. Every candidate it produces is ranked by how
many independent educators teach a concept -- breadth. docs/KNOWLEDGE.md is
explicit that breadth measures "how widely something is taught, never whether
it works". So: does breadth predict edge, or not?

The split is not chosen by hand. Each optional gate maps to a concept whose
weight was derived from the corpus (see bot/smc/consensus.py and
scripts/derive_weights.py), and the gates are cut at the median weight:

    GROUP A -- broadly taught      GROUP B -- narrowly taught
      target_at_level    0.77       mitigation        0.40
      candle_confirm     0.64       value_area_edge   0.30
      premium_discount   0.46       vwap_side         0.30
                                    breaker           0.15
                                    wyckoff           0.13

Every real position is then screened under four configurations -- baseline
(the shipped state, all optional gates off), A only, B only, and both -- using
each trade's real realized P&L. Exits are untouched, so this measures gate
selectivity, not exit behaviour.

A gate group that removes losers raises PF. One that merely removes trades
lowers coverage without moving PF, and one that removes winners lowers both.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LTF_BARS, HTF_BARS = 200, 100

GROUP_A = {  # broadly taught: weight >= 0.46
    "require_target_at_level": 0.77,
    "require_candle_confirmation": 0.64,
    "require_premium_discount": 0.46,
}
GROUP_B = {  # narrowly taught: weight < 0.46
    "require_mitigation": 0.40,
    "require_value_area_edge": 0.30,
    "require_vwap_side": 0.30,
    "require_breaker": 0.15,
    "require_wyckoff": 0.13,
}


def _positions(hist: dict) -> list[dict]:
    opens, closes = {}, {}
    for d in hist["deals"]:
        if not d.get("symbol"):
            continue
        pid = d.get("position_id")
        if int(d["entry"]) == 0:
            opens.setdefault(pid, d)
        elif int(d["entry"]) == 1:
            closes.setdefault(pid, []).append(d)
    out = []
    for pid, o in opens.items():
        cs = closes.get(pid)
        if not cs:
            continue
        out.append({
            "symbol": o["symbol"],
            "dir": "long" if int(o["type"]) == 0 else "short",
            "open_dt": dt.datetime.fromtimestamp(int(o["time"])),
            "net": sum(float(c["profit"]) + float(c.get("commission") or 0)
                       + float(c.get("swap") or 0) for c in cs),
        })
    out.sort(key=lambda p: p["open_dt"])
    return out


def _stats(rows: list[dict], key: str) -> dict:
    kept = [r for r in rows if r[key]]
    if not kept:
        return {"n": 0, "win": 0.0, "net": 0.0, "pf": 0.0, "cov": 0.0}
    wins = [r["net"] for r in kept if r["net"] > 0]
    loss = -sum(r["net"] for r in kept if r["net"] < 0)
    return {
        "n": len(kept),
        "win": 100.0 * len(wins) / len(kept),
        "net": sum(r["net"] for r in kept),
        "pf": (sum(wins) / loss) if loss else float("inf"),
        "cov": 100.0 * len(kept) / len(rows),
    }


def main() -> int:
    import pandas as pd
    import yaml

    from bot.screening import ScreenConfig, TradeScreener
    from bot.smc.strategy import SMCStrategy
    from bot.trade_review import review

    p = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="var/mt5")
    a = p.parse_args()
    out = Path(a.out)

    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config.yaml").read_text())
    strategy = SMCStrategy(
        swing_lookback=cfg.get("swing_lookback", 5),
        order_block_lookback=cfg.get("order_block_lookback", 20),
        fvg_min_size_pct=cfg.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=cfg.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=cfg.get("reward_risk_ratio", 2.0),
        stop_loss_pct=cfg.get("stop_loss_pct"),
        stop_atr_mult=cfg.get("stop_atr_mult"),
    )
    # The baseline must force every optional gate OFF rather than inherit
    # config.yaml. Once the gates are armed in config (they now are), inheriting
    # it makes all four variants identical and the comparison silently reports
    # nothing -- which is exactly the kind of "looks like it ran" failure the
    # rest of this file exists to avoid.
    off = {k: False for k in (*GROUP_A, *GROUP_B)}
    base = {**(cfg.get("screening", {}) or {}), **off}
    variants = {
        "baseline": ScreenConfig.from_dict(base),
        "A_broad": ScreenConfig.from_dict({**base, **{k: True for k in GROUP_A}}),
        "B_narrow": ScreenConfig.from_dict({**base, **{k: True for k in GROUP_B}}),
        "A+B_all": ScreenConfig.from_dict(
            {**base, **{k: True for k in GROUP_A}, **{k: True for k in GROUP_B}}),
    }
    screeners = {k: TradeScreener(v) for k, v in variants.items()}

    hist = json.loads((out / "history.json").read_text())
    positions = _positions(hist)
    bars = {(f.stem.rsplit("_", 1)[0], f.stem.rsplit("_", 1)[1]): pd.read_pickle(f)
            for f in (out / "bars").glob("*.pkl")}

    rows, skipped = [], 0
    for i, pos in enumerate(positions):
        ltf, htf = bars.get((pos["symbol"], "15m")), bars.get((pos["symbol"], "1h"))
        if ltf is None or htf is None:
            skipped += 1
            continue
        t = pd.Timestamp(pos["open_dt"])
        li = ltf.timestamp.searchsorted(t, "right")
        hi = htf.timestamp.searchsorted(t, "right")
        if li < LTF_BARS or hi < 20:
            skipped += 1
            continue
        lw = ltf.iloc[li - LTF_BARS:li].reset_index(drop=True)
        hw = htf.iloc[max(0, hi - HTF_BARS):hi].reset_index(drop=True)

        v = review(strategy, lw, hw)
        row = {"symbol": pos["symbol"], "dir": pos["dir"], "net": pos["net"],
               "open_dt": pos["open_dt"].isoformat(), "veto_ok": v.allowed}
        for name, scr in screeners.items():
            if not v.allowed:
                row[name] = False
                continue
            try:
                r = scr.screen(v.signal, lw, hw)
                row[name] = bool(r.approved) and r.direction == pos["dir"]
            except Exception:
                row[name] = False
        rows.append(row)
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(positions)}", flush=True)

    pd.DataFrame(rows).to_json(out / "gate_split.json", orient="records")

    all_net = sum(r["net"] for r in rows)
    all_win = 100.0 * sum(1 for r in rows if r["net"] > 0) / len(rows)
    gl = -sum(r["net"] for r in rows if r["net"] < 0)
    all_pf = (sum(r["net"] for r in rows if r["net"] > 0) / gl) if gl else 0.0

    print("\n" + "=" * 78)
    print(f"GATE SPLIT BY CORPUS BREADTH — {len(rows)} real positions")
    print("=" * 78)
    print(f"{'configuration':<24}{'kept':>7}{'cov%':>8}{'win%':>8}{'net':>13}{'PF':>9}")
    print("-" * 78)
    print(f"{'MT5 actual (no gates)':<24}{len(rows):>7}{100.0:>7.1f}%"
          f"{all_win:>7.1f}%{all_net:>13,.0f}{all_pf:>9.3f}")
    print(f"{'veto only':<24}", end="")
    s = _stats(rows, "veto_ok")
    print(f"{s['n']:>7}{s['cov']:>7.1f}%{s['win']:>7.1f}%{s['net']:>13,.0f}{s['pf']:>9.3f}")
    for name in ("baseline", "A_broad", "B_narrow", "A+B_all"):
        s = _stats(rows, name)
        label = {"baseline": "veto + screen (shipped)",
                 "A_broad": "  + GROUP A (broad)",
                 "B_narrow": "  + GROUP B (narrow)",
                 "A+B_all": "  + BOTH groups"}[name]
        print(f"{label:<24}{s['n']:>7}{s['cov']:>7.1f}%{s['win']:>7.1f}%"
              f"{s['net']:>13,.0f}{s['pf']:>9.3f}")
    print("-" * 78)
    print(f"skipped (insufficient bars): {skipped}")

    a_s, b_s = _stats(rows, "A_broad"), _stats(rows, "B_narrow")
    print("\nDOES BREADTH PREDICT EDGE?")
    if a_s["n"] and b_s["n"]:
        print(f"  broad gates  PF {a_s['pf']:.3f} on {a_s['n']} trades")
        print(f"  narrow gates PF {b_s['pf']:.3f} on {b_s['n']} trades")
        print("  ->", "breadth predicted edge" if a_s["pf"] > b_s["pf"]
              else "breadth did NOT predict edge -- the narrow half did better")
    else:
        print("  one group admitted no trades; see coverage above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
