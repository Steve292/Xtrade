#!/usr/bin/env python3
"""
SMC auto-trader for Hyperliquid (testnet). Every candidate signal must clear the
full screen — SMC + Fibonacci + top-down + risk + sniper entry — before it can
trade.

    python hypertrade.py BTC                  # dry-run: screen only, no orders
    python hypertrade.py XMR --risk 1 --lev 5 # dry-run with explicit risk/leverage
    python hypertrade.py BTC --loop           # keep scanning on the poll interval
    python hypertrade.py BTC --live           # place REAL testnet orders on approval
                                              #   (needs a funded wallet)

Dry-run is the default and sends no orders. `--live` requires a wallet (from
HL_PRIVATE_KEY or wallet_testnet.json) funded via the testnet faucet.
"""

from __future__ import annotations

import argparse
import time

import yaml
from dotenv import load_dotenv

from bot.hyperliquid.client import HyperliquidClient
from bot.hyperliquid.trader import HyperliquidTrader
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.strategy import SMCStrategy, SignalType
from bot.wallet import DefiWallet


def scan_and_report(trader, coins, ltf, htf, account_value, dry_run):
    rows = trader.scan(coins, ltf, htf, account_value)
    print(f"{'COIN':<11}{'SIGNAL':<7}{'CONF':>5}  VERDICT")
    print("-" * 52)
    approved = []
    for coin, signal, result, plan, err in rows:
        if err:
            print(f"{coin:<11}{'-':<7}{'':>5}  error: {err[:32]}")
        elif signal.type == SignalType.NONE:
            print(f"{coin:<11}{'-':<7}{'':>5}  no setup")
        elif result.approved:
            print(f"{coin:<11}{signal.type.value.upper():<7}{signal.confidence:>4.0%}  APPROVED")
            approved.append((coin, signal, result, plan))
        else:
            failed = next((c.name for c in result.checks if not c.passed), "?")
            print(f"{coin:<11}{signal.type.value.upper():<7}{signal.confidence:>4.0%}  rejected — {failed}")

    for coin, signal, result, plan in approved:
        print(f"\n{coin} APPROVED — full screen:")
        print(result.table())
        print(f"  Plan: {plan.side.upper()} ${plan.usd} of {coin} at {plan.leverage}x")
        if dry_run:
            print("  -> DRY RUN — no order sent")
        else:
            print("  -> firing testnet order:", trader.execute(plan))
    if not approved:
        print("\nNo setups cleared the full screen this pass.")


def main() -> None:
    load_dotenv()
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    hl = cfg.get("hyperliquid", {})

    ap = argparse.ArgumentParser(description="SMC-screened Hyperliquid auto-trader (testnet)")
    ap.add_argument("coin", nargs="?", default="BTC")
    ap.add_argument("--watchlist", action="store_true", help="scan all configured majors + memecoins")
    ap.add_argument("--live", action="store_true", help="place real testnet orders (needs funded wallet)")
    ap.add_argument("--loop", action="store_true", help="scan continuously")
    ap.add_argument("--risk", type=float, default=cfg.get("risk_per_trade_pct", 1.0))
    ap.add_argument("--lev", type=int, default=hl.get("default_leverage", 3))
    ap.add_argument("--interval", default=cfg.get("timeframe", "15m"))
    ap.add_argument("--htf", default=cfg.get("higher_timeframe", "1h"))
    ap.add_argument("--balance", type=float, default=cfg.get("initial_balance", 10000.0),
                    help="account value to size against in dry-run")
    args = ap.parse_args()

    wallet = DefiWallet.from_env() or DefiWallet.load()
    if args.live and wallet is None:
        raise SystemExit("--live needs a wallet. Run: python hyperwallet.py create")

    client = HyperliquidClient.connect(
        private_key=wallet.private_key if wallet else "",
        testnet=hl.get("testnet", True),
    )

    strategy = SMCStrategy(
        swing_lookback=cfg.get("swing_lookback", 5),
        order_block_lookback=cfg.get("order_block_lookback", 20),
        fvg_min_size_pct=cfg.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=cfg.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=cfg.get("reward_risk_ratio", 2.0),
    )
    screener = TradeScreener(ScreenConfig.from_dict(cfg.get("screening", {})))
    trader = HyperliquidTrader(client, strategy, screener, risk_pct=args.risk, leverage=args.lev)

    # In live mode size against the real funded account; in dry-run use --balance.
    account_value = client.account().account_value if args.live else args.balance

    watch = None
    if args.watchlist:
        watch = list(dict.fromkeys((hl.get("majors") or []) + (hl.get("memecoins") or [])))

    mode = "LIVE (testnet orders)" if args.live else "DRY RUN (no orders)"
    target = f"watchlist ({len(watch)} coins)" if watch else args.coin
    print("=" * 60)
    print(f"  SMC auto-trader — Hyperliquid {'testnet' if hl.get('testnet', True) else 'MAINNET'}")
    print(f"  Scan: {target}   TF: {args.interval}/{args.htf}   Risk: {args.risk}%   Lev: {args.lev}x")
    print(f"  Mode: {mode}   Sizing balance: ${account_value:,.2f}")
    print("=" * 60)

    poll = cfg.get("poll_interval_sec", 30)
    try:
        while True:
            if watch:
                scan_and_report(trader, watch, args.interval, args.htf, account_value,
                                dry_run=not args.live)
            elif args.live and client.account().positions:
                print(f"[{args.coin}] position already open — skipping scan")
            else:
                trader.run_once(args.coin, args.interval, args.htf, account_value, dry_run=not args.live)
            if not args.loop:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
