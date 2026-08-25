#!/usr/bin/env python3
"""
Offline A/B harness for the three confluence flags, over historical bars.

Why this exists rather than reusing backtest.py: bot/backtest/engine.py only
ever drives SMCStrategy and calc_position_size. It never calls TradeScreener,
evaluate_unified, or the sizing chain — so it structurally cannot measure the
number that matters most here, the AUTO-FIRE COUNT, i.e. how many setups clear
live_state's auto_fire_pct and would execute with no human in the loop.
Reporting a backtest's win rate as evidence about auto-fire behaviour would be
measuring the wrong thing.

This replays historical candles through the SAME chain bot/runner.py uses:

    SMCStrategy -> TradeScreener -> evaluate_unified(+knowledge) -> sizing

It never connects to a broker and never places anything. Smart-money inputs
are held at a fixed NEUTRAL so the only thing varying between arms is the flag
under test.

Usage:
    python scripts/validate_confluence.py --bars 3000 --symbol BTC/USDT
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from bot import knowledge as knowledge_mod
from bot.position_sizing import (
    SizingFactors,
    apply_risk_ceiling,
    atr,
    confidence_multiplier,
    final_risk_pct,
    regime_alloc_weight,
    risk_pct_for_fixed_usd,
    volatility_adjust,
)
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.strategy import SMCStrategy, SignalType
from bot.unified_screen import evaluate_unified

# Held fixed across every arm: the variable under test is the flag, not the
# market-wide smart-money read.
SM_DIRECTION, SM_BULL, SM_BEAR = "NEUTRAL", 0, 0  # defaults; overridable via --sm-agreement
WARMUP = 200
HTF_BARS = 100


def live_state_auto_fire() -> float:
    from bot import live_state
    return live_state.get_auto_fire_pct()


def fetch(symbol: str, timeframe: str, bars: int):
    import pandas as pd
    from bot.exchange import Exchange

    ex = Exchange(exchange_id="binance", mode="paper")
    out, since = [], None
    while len(out) < bars:
        batch = ex.client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        out = batch + out if since else batch
        since = batch[0][0] - ex.client.parse_timeframe(timeframe) * 1000
        if len(batch) < 1000:
            break
    df = pd.DataFrame(out[-bars:], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def collect_candidates(df, config, *, extended, step=4):
    """Every bar that produces a signal, collected ONCE.

    Arms must be scored against an IDENTICAL candidate set. The earlier
    version re-walked history inside each arm and de-duplicated on first
    occurrence, so a looser gate approved earlier bars and every arm sampled a
    different set of setups. The tell was htf_sweep "either" -- strictly
    looser than "both" -- reporting six FEWER auto-fires, which is impossible
    for a gate that only ever admits more. That was the harness comparing
    different trades, not the flag doing anything.
    """
    smc_cfg = config.get("smc", {})
    strategy = SMCStrategy(
        swing_lookback=config.get("swing_lookback", 5),
        order_block_lookback=config.get("order_block_lookback", 20),
        fvg_min_size_pct=config.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=config.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=config.get("reward_risk_ratio", 2.0),
        stop_loss_pct=config.get("stop_loss_pct"),
        extended_detectors=extended,
        extended_max_adjust=smc_cfg.get("extended_max_adjust", 0.10),
    )
    out = []
    for i in range(WARMUP, len(df) - 1, step):
        window = df.iloc[max(0, i - WARMUP):i]
        htf = df.iloc[max(0, i - HTF_BARS * 4):i:4]
        if len(window) < 50 or len(htf) < 20:
            continue
        sig = strategy.analyze(window, htf)
        if sig.type is not SignalType.NONE:
            out.append((i, sig, window, htf))
    return out


def run_arm(candidates, config, *, knowledge_on, sizing_on, index, balance, staged_usd,
            sm=(SM_DIRECTION, SM_BULL, SM_BEAR), screen_overrides=None):
    screen_cfg = dict(config.get("screening", {}))
    screen_cfg.update(screen_overrides or {})
    screener = TradeScreener(ScreenConfig.from_dict(screen_cfg))
    # Read the live runtime value rather than hardcoding it -- a stale constant
    # here silently measures a threshold the bot is not actually using.
    from bot import live_state
    auto_fire_pct = live_state.get_auto_fire_pct()
    sizing_cfg = config.get("position_sizing", {})
    max_multiple = float(sizing_cfg.get("max_multiple", 1.5))
    max_risk_usd = float(sizing_cfg.get("max_risk_usd", 5.0))

    stats = {
        "signals": 0, "screened": 0, "approved": 0, "auto_fire": 0, "queued": 0,
        "final_pct_sum": 0.0, "risk_usd_sum": 0.0, "risk_usd_max": 0.0, "ceiling_hits": 0,
    }

    for i, signal, window, htf in candidates:
        stats["signals"] += 1

        screen_result = screener.screen(signal, window, htf)
        if screen_result.approved:
            stats["screened"] += 1

        kr = (
            knowledge_mod.score_signal(signal.detectors, index)
            if knowledge_on and index.available else None
        )
        unified = evaluate_unified(
                signal, screen_result, sm[0], sm[1], sm[2],
            knowledge_result=kr,
            knowledge_max_adjust_pct=config.get("knowledge", {}).get("max_adjust_pct", 5.0),
        )
        if not unified.approved:
            continue

        stats["approved"] += 1
        stats["final_pct_sum"] += unified.final_pct
        if unified.final_pct >= auto_fire_pct:
            stats["auto_fire"] += 1
        else:
            stats["queued"] += 1

        risk_pct = risk_pct_for_fixed_usd(staged_usd, balance)
        if sizing_on and risk_pct > 0:
            vol = 1.0
            if len(window) > 100:
                a20, a100 = atr(window, 20).dropna(), atr(window, 100).dropna()
                if len(a20) and len(a100):
                    vol = volatility_adjust(float(a20.iloc[-1]), float(a100.mean()))
            adapted = final_risk_pct(SizingFactors(
                base_risk_pct=risk_pct,
                regime_alloc_weight=regime_alloc_weight("NEUTRAL"),
                hotness_multiplier=1.0,
                volatility_adjust=vol,
                confidence_multiplier=confidence_multiplier(signal.confidence),
            ))
            ceiling = min(staged_usd * max_multiple, max_risk_usd)
            capped = apply_risk_ceiling(adapted, balance, ceiling)
            if capped < adapted - 1e-9:
                stats["ceiling_hits"] += 1
            risk_pct = capped

        usd = risk_pct / 100 * balance
        stats["risk_usd_sum"] += usd
        stats["risk_usd_max"] = max(stats["risk_usd_max"], usd)

    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--step", type=int, default=4, help="bars between candidate samples")
    ap.add_argument("--balance", type=float, default=6.12, help="live combined balance")
    ap.add_argument("--staged-usd", type=float, default=3.0)
    ap.add_argument("--cache", default="", help="CSV to cache/reuse fetched bars")
    ap.add_argument("--sm-agreement", type=int, default=0,
                    help="smart-money modules agreeing with the signal (0-9). "
                         "Drives how high final_pct can reach, and therefore "
                         "whether auto-fire is reachable at all.")
    args = ap.parse_args()

    config = yaml.safe_load(open(args.config))
    import os
    import pandas as pd
    if args.cache and os.path.exists(args.cache):
        df = pd.read_csv(args.cache, parse_dates=["timestamp"])
        print(f"Reusing cached bars from {args.cache}")
    else:
        print(f"Fetching {args.bars} bars of {args.symbol} {args.timeframe}...")
        df = fetch(args.symbol, args.timeframe, args.bars)
        if args.cache:
            df.to_csv(args.cache, index=False)
    print(f"  {len(df)} bars, {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}\n")

    kcfg = config.get("knowledge", {})
    index = knowledge_mod.build_index(
        kcfg.get("corpus_path", knowledge_mod.DEFAULT_CORPUS_PATH),
        kcfg.get("cache_path", knowledge_mod.DEFAULT_CACHE_PATH),
    )
    print(f"Knowledge corpus: {'available' if index.available else 'NOT FOUND'}"
          + (f" ({index.document_count} docs, {len(index.weights)} modules)" if index.available else "")
          + f"\nBalance ${args.balance:.2f}, staged risk ${args.staged_usd:.2f}/trade, "
            f"auto-fire threshold {live_state_auto_fire():.0f}%\n")

    live_flags = dict(
        extended=config.get("smc", {}).get("extended_detectors", False),
        knowledge_on=config.get("knowledge", {}).get("enabled", False),
        sizing_on=config.get("position_sizing", {}).get("enabled", False),
    )
    # Liquidity-quality flags, measured one at a time against the live config
    # so each one's effect on approvals is isolated.
    arms = [
        ("live config (as running)", live_flags, {}),
        ("+ merge_pools",           live_flags, {"merge_pools": True}),
        ("+ require_reclaim",       live_flags, {"require_reclaim": True}),
        ("+ htf_sweep (both)",      live_flags, {"htf_sweep": True, "htf_sweep_require_both": True}),
        ("+ htf_sweep (either)",    live_flags, {"htf_sweep": True, "htf_sweep_require_both": False}),
        ("all quality flags on",    live_flags, {"merge_pools": True, "require_reclaim": True,
                                                 "htf_sweep": True, "htf_sweep_require_both": True}),
    ]

    n_agree = max(0, min(9, args.sm_agreement))
    sm = (("BULLISH" if n_agree else "NEUTRAL"), n_agree, 0)
    print(f"Smart money held at {sm[0]} with {n_agree}/9 modules agreeing "
          f"(final_pct ceiling ~{(100 + n_agree / 9 * 100) / 2:.0f}%)\n")

    hdr = (f"{'arm':<26} {'sig':>5} {'appr':>5} {'AUTO':>5} {'queue':>6} "
           f"{'avg%':>7} {'avg$':>7} {'max$':>7} {'cap':>4}")
    print(hdr); print("-" * len(hdr))
    # Collect ONCE, under the live extended-detector setting, then score that
    # identical set under every arm. This is what makes the arms comparable.
    candidates = collect_candidates(
        df, config, extended=live_flags["extended"], step=args.step)
    print(f"Candidate setups collected once: {len(candidates)} "
          f"(every arm is scored against this same set)\n")

    base = None
    for name, kw, overrides in arms:
        s = run_arm(candidates, config, index=index, balance=args.balance,
                    staged_usd=args.staged_usd, sm=sm,
                    screen_overrides=overrides,
                    knowledge_on=kw["knowledge_on"], sizing_on=kw["sizing_on"])
        n = max(s["approved"], 1)
        row = (f"{name:<26} {s['signals']:>5} {s['approved']:>5} {s['auto_fire']:>5} "
               f"{s['queued']:>6} {s['final_pct_sum']/n:>7.2f} "
               f"{s['risk_usd_sum']/n:>7.2f} {s['risk_usd_max']:>7.2f} {s['ceiling_hits']:>4}")
        if base is None:
            base = s
        else:
            d_appr = s["approved"] - base["approved"]
            d_fire = s["auto_fire"] - base["auto_fire"]
            marks = []
            if d_appr: marks.append(f"approved {d_appr:+d}")
            if d_fire: marks.append(f"auto-fire {d_fire:+d}")
            if marks: row += "   " + ", ".join(marks)
        print(row)

    print("\nAUTO = setups that would fire with NO human review. That column is "
          "the one to read before enabling anything.")


if __name__ == "__main__":
    main()
