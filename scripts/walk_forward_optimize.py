#!/usr/bin/env python3
"""
Walk-forward optimizer for the live SMC strategy's parameters — blueprint
Section 8. Fetches real historical BTC candles from Hyperliquid, runs
bot/backtest/optimizer.py's optimize -> select -> forward-validate cycle
once, and PRINTS a report. It never writes to config.yaml or anywhere
else — if the report says DEPLOY and you agree, you update config.yaml's
smc parameters yourself.

    python scripts/walk_forward_optimize.py                  # practical defaults (see below)
    python scripts/walk_forward_optimize.py --optimize-days 180 --test-days 30 --timeframe 15m

Performance note, found while building this: bot/backtest/engine.py's
BacktestEngine re-scans the whole window on every single bar (an existing
characteristic, not introduced here) — the blueprint's literal "180 days
optimize + 30 days test" at 15-minute candles took long enough in testing
that it isn't a practical default (thousands of bars, 9 candidates, each
bar re-running swing/order-block/liquidity/FVG detection). The defaults
below (1h candles, 14+5 days) run in well under a minute against real data
and still exercise the exact same walk-forward logic; pass --timeframe 15m
--optimize-days 180 --test-days 30 yourself if you want the literal spec and
are fine waiting — this script does not estimate or cap how long that takes.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from dotenv import load_dotenv

from bot.backtest.optimizer import format_walk_forward_report, run_walk_forward
from bot.hyperliquid.client import HyperliquidClient
from bot.wallet import DefiWallet

_SMC_PARAM_KEYS = (
    "swing_lookback",
    "order_block_lookback",
    "fvg_min_size_pct",
    "liquidity_tolerance_pct",
    "reward_risk_ratio",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coin", default="BTC", help="Hyperliquid coin to optimize against (default: BTC)")
    parser.add_argument("--timeframe", default="1h", help="candle interval (default: 1h — see performance note above)")
    parser.add_argument("--htf", default="4h", help="higher timeframe for trend context (default: 4h)")
    parser.add_argument("--optimize-days", type=float, default=14, help="optimize-window length in days (default: 14)")
    parser.add_argument("--test-days", type=float, default=5, help="forward test-window length in days (default: 5)")
    parser.add_argument("--epsilon", type=float, default=0.1, help="exploration rate, 0-1 (default: 0.1)")
    parser.add_argument("--seed", type=int, default=None, help="rng seed, for a reproducible epsilon-greedy draw")
    args = parser.parse_args()

    load_dotenv()
    with open(Path(__file__).resolve().parents[1] / "config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    current_params = {k: cfg[k] for k in _SMC_PARAM_KEYS if k in cfg}

    lookback_hours = int((args.optimize_days + args.test_days + 2) * 24)  # +2 days margin
    print(f"Fetching {lookback_hours}h of {args.timeframe} {args.coin} candles from Hyperliquid...")

    hl_cfg = cfg.get("hyperliquid", {})
    wallet = DefiWallet.from_env() or DefiWallet.load()
    client = HyperliquidClient.connect(
        private_key=wallet.private_key if wallet else "", testnet=hl_cfg.get("testnet", True)
    )
    df = client.candles(args.coin, interval=args.timeframe, lookback_hours=lookback_hours)
    print(f"Got {len(df)} bars ({df['timestamp'].min()} -> {df['timestamp'].max()})\n")

    rng = random.Random(args.seed) if args.seed is not None else None
    report = run_walk_forward(
        df,
        current_params,
        htf=args.htf,
        optimize_days=args.optimize_days,
        test_days=args.test_days,
        epsilon=args.epsilon,
        rng=rng,
    )

    if report is None:
        print(
            f"Not enough history for a {args.optimize_days}+{args.test_days} day walk-forward split "
            f"({len(df)} bars fetched) — try a shorter window or a coarser timeframe."
        )
        sys.exit(1)

    print(format_walk_forward_report(report))


if __name__ == "__main__":
    main()
