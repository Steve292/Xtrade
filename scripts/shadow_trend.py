#!/usr/bin/env python3
"""
Live shadow of the 4h trend pullback. Reads candles, writes a ledger, nothing else.

    python scripts/shadow_trend.py --loop      # the launchd service
    python scripts/shadow_trend.py --report    # results vs the backtest
    python scripts/shadow_trend.py --verify    # rebuild from MT5, flag any mismatch

Processes every closed 4h bar since the last one seen, so an outage is caught up
from MT5's history rather than lost.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from dotenv import load_dotenv

from bot.mt5.client import MT5Client
from bot.shadow_trend import Params, State, add_indicators, replay, step, summarize

STATE_PATH = ROOT / "shadow_state.json"
SYMBOLS = ("BTCUSDc", "XAUUSDc")
POLL_SECONDS = 300
# What the stress test measured (2026-11-04); the bar live results are judged by.
BACKTEST = {"BTCUSDc": {"win_rate": 0.24, "per_trade_r": 0.41}, "XAUUSDc": {"win_rate": 0.27, "per_trade_r": 0.42}}


def _connect() -> MT5Client:
    return MT5Client.connect(
        host=os.getenv("MT5_HOST", "127.0.0.1"), port=os.getenv("MT5_PORT", "18812"),
        login=os.getenv("MT5_LOGIN", ""), password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""), terminal_path=os.getenv("MT5_TERMINAL_PATH", ""),
    )


def _market(client: MT5Client, sym: str) -> tuple:
    """(closed-bar frame with indicators, spread, swap in price units per day)."""
    h4 = client.copy_rates(sym, "4h", 2000).iloc[:-1]   # last row is still forming
    d1 = client.copy_rates(sym, "1d", 300)
    info = client._mt5.symbol_info(sym)
    bid, ask = client.tick(sym)
    swap = (info.swap_long * info.point, info.swap_short * info.point) if info.swap_mode == 1 else (0.0, 0.0)
    return add_indicators(h4, d1), ask - bid, swap


def _load() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save(data: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(STATE_PATH)


def run_once(client: MT5Client) -> None:
    data = _load()
    for sym in SYMBOLS:
        df, spread, swap = _market(client, sym)
        rec = data.get(sym)
        if rec is None:
            # Forward results only: start at the newest closed bar, nothing replayed.
            rec = {"started": str(df["timestamp"].iloc[-1]), "last_bar": str(df["timestamp"].iloc[-1]),
                   "pending": None, "position": None, "trades": []}
            print(f"[{sym}] shadow started at bar {rec['last_bar']} (spread {spread:.2f})", flush=True)
        st = State(pending=rec["pending"], position=rec["position"], trades=rec["trades"])
        new = df[df["timestamp"] > pd.Timestamp(rec["last_bar"])]
        for bar in new.itertuples(index=False):
            had_position = st.position
            trade = step(st, bar, spread, swap)
            if st.position and not had_position:
                pos = st.position
                print(f"[{sym}] {bar.timestamp} SHADOW {'LONG' if pos['side'] == 1 else 'SHORT'} "
                      f"entry {pos['entry']:.2f} stop {pos['stop']:.2f}", flush=True)
            if trade:
                print(f"[{sym}] {bar.timestamp} SHADOW EXIT {trade['reason']} {trade['exit']:.2f} "
                      f"R {trade['R']:+.2f}", flush=True)
            rec["last_bar"] = str(bar.timestamp)
        rec.update(pending=st.pending, position=st.position, trades=st.trades)
        data[sym] = rec
    data["last_pass"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save(data)


def report() -> None:
    data = _load()
    print(f"last pass: {data.get('last_pass', 'never')}")
    for sym in SYMBOLS:
        rec = data.get(sym)
        if not rec:
            print(f"{sym}: not started")
            continue
        s = summarize(rec["trades"])
        print(f"\n{sym}: since {rec['started']} | last bar {rec['last_bar']} | open {rec['position'] or 'none'}")
        print(f"  live: {s}")
        print(f"  backtest: {BACKTEST[sym]} (win rate is low by design; judge after 30+ trades)")


def verify(client: MT5Client) -> int:
    """Rebuild each ledger from fresh candles. Different entries, exits or R mean
    the tracker and its tested logic have drifted, and the results can't be trusted."""
    data = _load()
    bad = 0
    for sym in SYMBOLS:
        rec = data.get(sym)
        if not rec:
            continue
        df, spread, swap = _market(client, sym)
        start = int((df["timestamp"] <= pd.Timestamp(rec["started"])).sum())
        rebuilt = replay(df, spread, swap, start=start).trades
        keys = ("side", "entry_time", "exit_time", "reason")
        same = [tuple(t[k] for k in keys) for t in rec["trades"]] == [tuple(t[k] for k in keys) for t in rebuilt]
        print(f"{sym}: ledger {len(rec['trades'])} trades, rebuilt {len(rebuilt)} -> {'MATCH' if same else 'MISMATCH'}")
        bad += 0 if same else 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    if args.report:
        report()
        return 0
    if args.verify:
        return 1 if verify(_connect()) else 0
    while True:
        try:
            run_once(_connect())
        except Exception as e:
            print(f"[shadow] pass failed ({type(e).__name__}: {str(e)[:150]}) -- retrying next pass", flush=True)
        if not args.loop:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
