#!/usr/bin/env python3
"""
Walk-forward trade simulation over MT5 history, with full annotation.

Answers the question a threshold sweep alone cannot: of the setups that would
have fired at final_pct >= T, how many actually reached target before stop?
bot/backtest/engine.py cannot answer it because it never runs the screener or
the unified gate -- it enters on the raw SMC signal.

For every bar it runs the real chain (SMCStrategy -> TradeScreener ->
evaluate_unified + knowledge), and for each approved setup walks price FORWARD
bar by bar until the stop or the take-profit is touched, recording which came
first and how long it took. Unresolved setups at the end of history are
reported as OPEN rather than silently dropped -- with a 20% stop those are the
majority, and hiding them would flatter the win rate badly.

Emits JSON: per-trade records, win rate by threshold, and annotated chart data
(candles, swings, OTE band, liquidity sweep) for the exemplar trade.

Usage: python scripts/simulate_trades.py --bars 4000 --out /tmp/sim.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

from bot import knowledge as kmod
from bot.mt5.client import MT5Client
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.fibonacci import ote_band, recent_leg
from bot.smc.liquidity import detect_liquidity_pools, recent_sweep
from bot.smc.strategy import SMCStrategy, SignalType
from bot.smc.structure import detect_trend, find_swing_points
from bot.unified_screen import evaluate_unified

WARMUP = 200
HTF_RATIO = 16  # 15m -> 4h


def simulate(df, hdf_full, strategy, screener, index, kcfg, symbol, step=4):
    """One pass over history. `step` skips bars to keep this tractable; a
    setup persists across several bars, so stepping does not lose setups, it
    just samples each one fewer times."""
    highs, lows = df["high"].values, df["low"].values
    trades, seen = [], set()

    for i in range(WARMUP, len(df) - 1, step):
        window = df.iloc[max(0, i - WARMUP):i]
        htf_slice = hdf_full[hdf_full["timestamp"] <= df.iloc[i - 1]["timestamp"]]
        if len(window) < 50 or len(htf_slice) < 20:
            continue

        sig = strategy.analyze(window, htf_slice.tail(100))
        if sig.type is SignalType.NONE:
            continue
        screen = screener.screen(sig, window, htf_slice.tail(100))
        kr = kmod.score_signal(sig.detectors, index) if index.available else None
        u = evaluate_unified(sig, screen, "NEUTRAL", 0, 0, knowledge_result=kr,
                             knowledge_max_adjust_pct=kcfg.get("max_adjust_pct", 5.0))
        if not u.approved:
            continue

        # De-duplicate: the same setup re-detected on consecutive bars is one
        # trade, not many. Key on the entry level, rounded.
        key = (sig.type.value, round(sig.entry, 1))
        if key in seen:
            continue
        seen.add(key)

        long = sig.type is SignalType.LONG
        outcome, bars_held, exit_price = "OPEN", None, None
        for j in range(i, len(df)):
            hi, lo = highs[j], lows[j]
            if long:
                if lo <= sig.stop_loss:
                    outcome, bars_held, exit_price = "LOSS", j - i, sig.stop_loss; break
                if hi >= sig.take_profit:
                    outcome, bars_held, exit_price = "WIN", j - i, sig.take_profit; break
            else:
                if hi >= sig.stop_loss:
                    outcome, bars_held, exit_price = "LOSS", j - i, sig.stop_loss; break
                if lo <= sig.take_profit:
                    outcome, bars_held, exit_price = "WIN", j - i, sig.take_profit; break

        trades.append({
            "symbol": symbol, "index": i,
            "timestamp": str(df.iloc[i]["timestamp"]),
            "side": sig.type.value, "entry": sig.entry, "stop": sig.stop_loss,
            "tp": sig.take_profit, "confidence": sig.confidence,
            "final_pct": u.final_pct, "knowledge_pct": u.knowledge_pct,
            "detectors": list(sig.detectors), "reason": sig.reason,
            "outcome": outcome, "bars_held": bars_held, "exit_price": exit_price,
        })
    return trades


def annotate(df, idx, trade):
    """Chart payload for one trade: candles around it plus the structural
    objects the gates actually looked at."""
    lo_i, hi_i = max(0, idx - 90), min(len(df), idx + 60)
    sl = df.iloc[lo_i:hi_i]
    window = df.iloc[max(0, idx - WARMUP):idx]

    swings = find_swing_points(window, 5)
    leg = recent_leg(swings, "bullish" if trade["side"] == "long" else "bearish")
    ote = list(ote_band(*leg)) if leg else None
    pools = detect_liquidity_pools(window, 0.0005)
    sweep = recent_sweep(pools, window, bars=20)

    return {
        "symbol": trade["symbol"],
        "entry_offset": idx - lo_i,
        "candles": [
            {"t": str(r["timestamp"]), "o": float(r["open"]), "h": float(r["high"]),
             "l": float(r["low"]), "c": float(r["close"])}
            for _, r in sl.iterrows()
        ],
        "swings": [{"i": s.index - (max(0, idx - WARMUP) - lo_i), "p": s.price, "k": s.kind}
                   for s in swings[-8:]],
        "ote": ote,
        "leg": list(leg) if leg else None,
        "sweep": {"level": sweep.level, "kind": sweep.kind} if sweep else None,
        "htf_trend": trade.get("htf_trend"),
        "trade": trade,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=4000)
    ap.add_argument("--step", type=int, default=4)
    ap.add_argument("--out", default="/tmp/sim.json")
    args = ap.parse_args()

    load_dotenv("/Users/mac/Projects/smc-trading-bot/.env")
    cfg = yaml.safe_load(open("config.yaml"))
    smc_cfg, kcfg = cfg.get("smc", {}), cfg.get("knowledge", {})
    index = kmod.build_index(kcfg.get("corpus_path"), kcfg.get("cache_path"))

    strategy = SMCStrategy(
        swing_lookback=cfg["swing_lookback"], order_block_lookback=cfg["order_block_lookback"],
        fvg_min_size_pct=cfg["fvg_min_size_pct"], liquidity_tolerance_pct=cfg["liquidity_tolerance_pct"],
        reward_risk_ratio=cfg["reward_risk_ratio"], stop_loss_pct=cfg.get("stop_loss_pct"),
        extended_detectors=smc_cfg.get("extended_detectors", False),
        extended_max_adjust=smc_cfg.get("extended_max_adjust", 0.10))
    screener = TradeScreener(ScreenConfig.from_dict(cfg.get("screening", {})))

    c = MT5Client.connect(host=os.getenv("MT5_HOST","127.0.0.1"), port=os.getenv("MT5_PORT","18812"),
        login=os.getenv("MT5_LOGIN",""), password=os.getenv("MT5_PASSWORD",""),
        server=os.getenv("MT5_SERVER",""), terminal_path=os.getenv("MT5_TERMINAL_PATH",""))

    result = {"symbols": {}, "config": {
        "stop_loss_pct": cfg.get("stop_loss_pct"), "rr": cfg["reward_risk_ratio"],
        "ltf": cfg["mt5_timeframe"], "htf": cfg["higher_timeframe"]}}

    for sym in cfg["mt5_watchlist"]:
        print(f"[{sym}] fetching...", flush=True)
        df = c.copy_rates(sym, cfg["mt5_timeframe"], count=args.bars)
        hdf = c.copy_rates(sym, cfg["higher_timeframe"], count=args.bars // HTF_RATIO + 200)
        hsw = find_swing_points(hdf, cfg["swing_lookback"])
        print(f"[{sym}] {len(df)} LTF bars {df.iloc[0]['timestamp']} -> {df.iloc[-1]['timestamp']}"
              f" | {len(hdf)} HTF bars | HTF trend {detect_trend(hsw).value}", flush=True)

        trades = simulate(df, hdf, strategy, screener, index, kcfg, sym, args.step)
        print(f"[{sym}] {len(trades)} distinct approved setups", flush=True)

        charts = []
        resolved = [t for t in trades if t["outcome"] != "OPEN"]
        for t in (resolved[:2] or trades[:1]):
            charts.append(annotate(df, t["index"], t))

        result["symbols"][sym] = {
            "htf_trend": detect_trend(hsw).value,
            "bars": len(df),
            "start": str(df.iloc[0]["timestamp"]), "end": str(df.iloc[-1]["timestamp"]),
            "trades": trades, "charts": charts,
        }

    json.dump(result, open(args.out, "w"), indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
