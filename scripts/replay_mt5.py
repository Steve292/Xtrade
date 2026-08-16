#!/usr/bin/env python
"""Replay real MT5 history through the strategy and measure the abstention veto.

Read-only against MT5: it calls history_deals_get and copy_rates_range and
nothing else. No order_send, no modify, no close. Everything after the export
runs offline against cached files, so a re-analysis never touches the terminal
the live account is connected to.

    python scripts/replay_mt5.py export   --out var/mt5
    python scripts/replay_mt5.py bars     --out var/mt5
    python scripts/replay_mt5.py replay   --out var/mt5
    python scripts/replay_mt5.py validate --out var/mt5   # out-of-sample split

`replay` answers "would the veto have kept this trade", using the trade's real
realized P&L. It is a FILTER study: exits stay whatever actually happened, so
no exit behaviour is invented. See bot/trade_review.py for what the numbers
did and did not support.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LTF, HTF = "15m", "1h"
LTF_BARS, HTF_BARS = 200, 100
TF_ATTR = {"15m": "TIMEFRAME_M15", "1h": "TIMEFRAME_H1"}


def _raw():
    from dotenv import load_dotenv
    from mt5linux import MetaTrader5

    load_dotenv()
    r = MetaTrader5(host=os.getenv("MT5_HOST", "127.0.0.1"),
                    port=int(os.getenv("MT5_PORT", "18812")))
    if not r.initialize():
        raise SystemExit(f"MT5 initialize() failed: {r.last_error()}")
    return r


def cmd_export(out: Path) -> int:
    raw = _raw()
    deals = raw.history_deals_get(dt.datetime(2000, 1, 1),
                                  dt.datetime.now() + dt.timedelta(days=1))
    fields = ("ticket", "time", "type", "entry", "magic", "position_id",
              "reason", "volume", "price", "commission", "swap", "profit",
              "symbol", "comment")
    rows = [{f: getattr(d, f, None) for f in fields} for d in (deals or [])]
    (out / "history.json").write_text(json.dumps({"deals": rows}, default=str))
    print(f"exported {len(rows)} deals -> {out/'history.json'}")
    return 0


def cmd_bars(out: Path) -> int:
    import pandas as pd

    hist = json.loads((out / "history.json").read_text())
    symbols = sorted({d["symbol"] for d in hist["deals"] if d.get("symbol")})
    raw = _raw()
    bars = out / "bars"
    bars.mkdir(exist_ok=True)
    # Pad the start: the strategy needs lookback before the first trade.
    start, end = dt.datetime(2025, 11, 1), dt.datetime.now() + dt.timedelta(days=1)
    for sym in symbols:
        for tf, attr in TF_ATTR.items():
            dest = bars / f"{sym}_{tf}.pkl"
            if dest.exists():
                continue
            rates = raw.copy_rates_range(sym, getattr(raw, attr), start, end)
            if rates is None or len(rates) == 0:
                print(f"  {sym} {tf}: no data")
                continue
            df = pd.DataFrame(rates)
            df["timestamp"] = pd.to_datetime(df["time"], unit="s")
            df = df.rename(columns={"tick_volume": "volume"})
            df = df[["timestamp", "open", "high", "low", "close", "volume"]]
            df.to_pickle(dest)
            print(f"  {sym} {tf}: {len(df)} bars")
    return 0


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


def cmd_replay(out: Path) -> int:
    import pandas as pd
    import yaml

    from bot.smc.strategy import SMCStrategy
    from bot.trade_review import ReviewStats, review, summarize

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
    hist = json.loads((out / "history.json").read_text())
    positions = _positions(hist)
    bars = {(f.stem.rsplit("_", 1)[0], f.stem.rsplit("_", 1)[1]): pd.read_pickle(f)
            for f in (out / "bars").glob("*.pkl")}

    stats, rows, skipped = ReviewStats(), [], 0
    for p in positions:
        ltf, htf = bars.get((p["symbol"], LTF)), bars.get((p["symbol"], HTF))
        if ltf is None or htf is None:
            skipped += 1
            continue
        t = pd.Timestamp(p["open_dt"])
        li, hi = ltf.timestamp.searchsorted(t, "right"), htf.timestamp.searchsorted(t, "right")
        if li < LTF_BARS or hi < 20:
            skipped += 1
            continue
        v = review(strategy,
                   ltf.iloc[li - LTF_BARS:li].reset_index(drop=True),
                   htf.iloc[max(0, hi - HTF_BARS):hi].reset_index(drop=True))
        stats.add(v, p["net"])
        rows.append({**{k: p[k] for k in ("symbol", "dir", "net")},
                     "open_dt": p["open_dt"].isoformat(), "kept": v.allowed})
    pd.DataFrame(rows).to_json(out / "replay.json", orient="records")
    print(summarize(stats))
    print(f"\nskipped (insufficient bars): {skipped}")
    return 0


def cmd_validate(out: Path) -> int:
    """Split chronologically and re-test. A rule that only works on one half
    was fitted to it."""
    import pandas as pd

    df = pd.read_json(out / "replay.json").sort_values("open_dt").reset_index(drop=True)
    mid = len(df) // 2

    def pf(d):
        gl = -d[d.net < 0].net.sum()
        return (d[d.net > 0].net.sum() / gl) if gl else float("inf")

    print(f"{'half':<14}{'policy':<18}{'trades':>7}{'net':>12}{'PF':>9}")
    print("-" * 60)
    holds = True
    for name, h in (("FIRST", df.iloc[:mid]), ("SECOND", df.iloc[mid:])):
        base, kept = h, h[h.kept]
        print(f"{name:<14}{'take everything':<18}{len(base):>7}{base.net.sum():>12,.0f}{pf(base):>9.3f}")
        if len(kept):
            print(f"{'':<14}{'veto applied':<18}{len(kept):>7}{kept.net.sum():>12,.0f}{pf(kept):>9.3f}")
            holds = holds and pf(kept) > pf(base)
    print("-" * 60)
    print("VERDICT:", "holds in both halves" if holds else "FAILS out of sample")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["export", "bars", "replay", "validate"])
    p.add_argument("--out", default="var/mt5", help="working directory for cached data")
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    return {"export": cmd_export, "bars": cmd_bars,
            "replay": cmd_replay, "validate": cmd_validate}[a.action](out)


if __name__ == "__main__":
    raise SystemExit(main())
