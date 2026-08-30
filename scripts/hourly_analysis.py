#!/usr/bin/env python3
"""
Run one full SMC scan of the live watchlist and write a narrated report.

Reuses exactly the strategy/screener/knowledge chain bot/runner.py trades on
-- same config, same gates, same auto_fire_pct -- so this is a description of
what the live loop is actually seeing, not a second opinion. Read-only: it
never places or modifies an order.

Run once by hand, or on a schedule (see deploy/com.smc.hourlyanalysis.plist,
which runs this every hour via launchd's StartInterval).

Output: appends to logs/hourly_analysis.log (the human-readable report) and
overwrites hourly_analysis_latest.json (one snapshot, for anything that wants
to read the last report programmatically -- a future dashboard widget, etc.).
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

from bot import knowledge as knowledge_mod, live_state
from bot.hourly_report import SymbolSnapshot, build_report
from bot.mt5.client import MT5Client
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.fibonacci import ote_band, recent_leg
from bot.smc.liquidity import detect_liquidity_pools, recent_sweep
from bot.smc.strategy import SMCStrategy, SignalType
from bot.smc.structure import detect_structure_breaks, detect_trend, find_swing_points
from bot.unified_screen import evaluate_unified

REPORT_LOG = "logs/hourly_analysis.log"
LATEST_JSON = "hourly_analysis_latest.json"


def _last_event_str(events, df, swing_lookback) -> str | None:
    if not events:
        return None
    e = events[-1]
    ago = len(df) - 1 - e.index
    return f"{e.kind.upper()} {e.direction}, {ago} bars ago"


def _snapshot_for(sym: str, cfg: dict, mt5: MT5Client, strategy: SMCStrategy,
                  screener: TradeScreener, index) -> SymbolSnapshot:
    ltf, htf = cfg["mt5_timeframe"], cfg["higher_timeframe"]
    df = mt5.copy_rates(sym, ltf, count=200)
    hdf = mt5.copy_rates(sym, htf, count=100)
    price = float(df.iloc[-1]["close"])
    bar_time = str(df.iloc[-1]["timestamp"])

    swing_lb = cfg["swing_lookback"]
    lswings = find_swing_points(df, swing_lb)
    hswings = find_swing_points(hdf, swing_lb)
    ltf_trend = detect_trend(lswings)
    htf_trend = detect_trend(hswings)
    levents = detect_structure_breaks(df, lswings)
    hevents = detect_structure_breaks(hdf, hswings)

    pools = detect_liquidity_pools(df, cfg["liquidity_tolerance_pct"])
    sweep = recent_sweep(pools, df, bars=cfg["screening"].get("sweep_bars", 20))
    sweep_str = (f"{sweep.kind} @ {sweep.level:,.2f} "
                f"({len(df) - 1 - sweep.index} bars ago)") if sweep else None

    sig = strategy.analyze(df, hdf)
    ote = None
    ote_dir = None
    leg_dir = "bullish" if sig.type != SignalType.SHORT else "bearish"
    leg = recent_leg(lswings, leg_dir)
    if leg:
        ote = ote_band(*leg)
        ote_dir = leg_dir

    snap = SymbolSnapshot(
        symbol=sym, price=price, bar_time=bar_time,
        ltf_label=ltf, htf_label=htf,
        ltf_trend=ltf_trend.value, htf_trend=htf_trend.value,
        ltf_last_event=_last_event_str(levents, df, swing_lb),
        htf_last_event=_last_event_str(hevents, hdf, swing_lb),
        sweep=sweep_str, ote_band=ote, ote_direction=ote_dir,
        signal_type=sig.type.value, signal_reason=sig.reason,
        auto_fire_pct=live_state.get_auto_fire_pct(),
    )

    if sig.type is not SignalType.NONE:
        sr = screener.screen(sig, df, hdf)
        kr = knowledge_mod.score_signal(sig.detectors, index) if index.available else None
        unified = evaluate_unified(
            sig, sr, "NEUTRAL", 0, 0, knowledge_result=kr,
            knowledge_max_adjust_pct=cfg.get("knowledge", {}).get("max_adjust_pct", 5.0),
            normalise_by_direction=cfg.get("normalise_smart_money_by_direction", False),
        )
        snap.confidence = sig.confidence
        snap.entry, snap.stop, snap.take_profit = sig.entry, sig.stop_loss, sig.take_profit
        snap.gate_checks = [(c.name, c.passed, c.detail) for c in sr.checks]
        snap.final_pct = unified.final_pct
        snap.knowledge_pct = kr.knowledge_pct if kr else None
        snap.approved = unified.approved

    return snap


def main() -> None:
    load_dotenv(".env")
    cfg = yaml.safe_load(open("config.yaml"))

    mt5 = MT5Client.connect(
        host=os.getenv("MT5_HOST", "127.0.0.1"), port=os.getenv("MT5_PORT", "18812"),
        login=os.getenv("MT5_LOGIN", ""), password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""), terminal_path=os.getenv("MT5_TERMINAL_PATH", ""),
    )
    strategy = SMCStrategy(
        swing_lookback=cfg["swing_lookback"], order_block_lookback=cfg["order_block_lookback"],
        fvg_min_size_pct=cfg["fvg_min_size_pct"], liquidity_tolerance_pct=cfg["liquidity_tolerance_pct"],
        reward_risk_ratio=cfg["reward_risk_ratio"], stop_loss_pct=cfg.get("stop_loss_pct"),
        extended_detectors=cfg.get("smc", {}).get("extended_detectors", False),
        extended_max_adjust=cfg.get("smc", {}).get("extended_max_adjust", 0.10),
    )
    screener = TradeScreener(ScreenConfig.from_dict(cfg.get("screening", {})))
    kcfg = cfg.get("knowledge", {})
    index = (knowledge_mod.build_index(kcfg.get("corpus_path"), kcfg.get("cache_path"))
            if kcfg.get("enabled") else knowledge_mod.KnowledgeIndex())

    snapshots = [_snapshot_for(sym, cfg, mt5, strategy, screener, index)
                for sym in cfg["mt5_watchlist"]]

    generated_at = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    report = build_report(snapshots, generated_at)

    with open(REPORT_LOG, "a") as f:
        f.write(report)

    with open(LATEST_JSON, "w") as f:
        json.dump({
            "generated_at": generated_at,
            "symbols": [
                {"symbol": s.symbol, "price": s.price, "signal": s.signal_type,
                 "final_pct": s.final_pct, "approved": s.approved,
                 "auto_fire_pct": s.auto_fire_pct}
                for s in snapshots
            ],
        }, f, indent=1)

    print(report)


if __name__ == "__main__":
    main()
