#!/usr/bin/env python3
"""
SMC auto-trader for Hyperliquid. Venue (testnet or mainnet) is whatever
`hyperliquid.testnet` in config.yaml says — check the startup banner, it
states the live venue explicitly. Every candidate signal must clear the full
screen — SMC + Fibonacci + top-down + risk + sniper entry — before it can trade.

    python hypertrade.py BTC                  # dry-run: screen only, no orders
    python hypertrade.py XMR --risk 1 --lev 5 # dry-run with explicit risk/leverage
    python hypertrade.py BTC --loop           # keep scanning on the poll interval
    python hypertrade.py BTC --live           # place REAL orders on approval, on
                                              #   whichever venue config.yaml selects
                                              #   (needs a funded wallet)

Dry-run is the default and sends no orders. `--live` requires a wallet (from
HL_PRIVATE_KEY or wallet_testnet.json) funded on the configured venue.
"""

from __future__ import annotations

import argparse
import time

import yaml
from dotenv import load_dotenv

from bot.capital_guard import CapitalGuard
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
        if plan is None:
            print("  -> approved, but not sizable — fund the wallet (or amount < $10 min). No order.")
            continue
        print(f"  Plan: {plan.side.upper()} ${plan.usd} of {coin} at {plan.leverage}x")
        if dry_run:
            print("  -> DRY RUN — no order sent")
        else:
            allowed, reason = trader.guard_check(account_value)
            if not allowed:
                print(f"  -> BLOCKED by capital guard: {reason}")
            else:
                print("  -> SNIPING approved order:", trader.execute(plan))
    if not approved:
        print("\nNo setups cleared the full screen this pass.")


def main() -> None:
    load_dotenv()
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    hl = cfg.get("hyperliquid", {})

    ap = argparse.ArgumentParser(description="SMC-screened Hyperliquid auto-trader")
    ap.add_argument("coin", nargs="?", default="BTC")
    ap.add_argument("--watchlist", action="store_true", help="scan all configured majors + memecoins")
    ap.add_argument("--live", action="store_true",
                    help="place real orders on the venue set by hyperliquid.testnet in config.yaml (needs funded wallet)")
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
    guard_cfg = cfg.get("capital_guard", {})
    capital_guard = CapitalGuard(**{k: guard_cfg[k] for k in CapitalGuard.__dataclass_fields__ if k in guard_cfg})
    trader = HyperliquidTrader(client, strategy, screener, risk_pct=args.risk, leverage=args.lev,
                                capital_guard=capital_guard)

    watch = None
    if args.watchlist:
        watch = list(dict.fromkeys((hl.get("majors") or []) + (hl.get("memecoins") or [])))

    venue_label = "testnet" if hl.get("testnet", True) else "REAL MONEY — mainnet"
    mode = f"LIVE ({venue_label} orders)" if args.live else "DRY RUN (no orders)"
    target = f"watchlist ({len(watch)} coins)" if watch else args.coin
    start_balance = client.account().account_value if args.live else args.balance
    print("=" * 60)
    print(f"  SMC auto-trader — Hyperliquid {'testnet' if hl.get('testnet', True) else 'MAINNET'}")
    print(f"  Scan: {target}   TF: {args.interval}/{args.htf}   Risk: {args.risk}%   Lev: {args.lev}x")
    print(f"  Mode: {mode}   Sizing balance: ${start_balance:,.2f}")
    if args.live and start_balance == 0:
        print("  Wallet unfunded — screening only until you fund it via the faucet.")
    print("=" * 60)

    poll = cfg.get("poll_interval_sec", 30)
    try:
        while True:
            # A transient venue error (502, timeout, rate limit) must not kill a
            # long-running bot — log it and retry on the next pass.
            try:
                # Re-read the live balance/positions each pass so funding the
                # wallet mid-run automatically arms sniping.
                if args.live:
                    acct = client.account()
                    account_value, open_positions = acct.account_value, acct.positions
                else:
                    account_value, open_positions = args.balance, []

                if watch:
                    scan_and_report(trader, watch, args.interval, args.htf, account_value,
                                    dry_run=not args.live)
                elif args.live and open_positions:
                    print(f"[{args.coin}] position already open — skipping scan")
                else:
                    trader.run_once(args.coin, args.interval, args.htf, account_value,
                                    dry_run=not args.live)
            except Exception as e:
                print(f"[transient error] {type(e).__name__}: {str(e)[:100]} — retrying next pass")

            if not args.loop:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
