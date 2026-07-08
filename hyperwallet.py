#!/usr/bin/env python3
"""
DeFi wallet + Hyperliquid perps CLI (TESTNET by default).

    python hyperwallet.py create                 # make a deposit wallet
    python hyperwallet.py address                # show address + faucet link
    python hyperwallet.py markets                # majors: live prices + max leverage
    python hyperwallet.py markets --group memes  # memecoins
    python hyperwallet.py markets --group all    # everything listed
    python hyperwallet.py balance                # account value + withdrawable
    python hyperwallet.py positions              # open positions
    python hyperwallet.py long  BTC 50 --lev 5   # long $50 of BTC at 5x
    python hyperwallet.py short XMR 25           # short $25 of Monero
    python hyperwallet.py close SOL              # close the SOL position

Long/short work on any listed perp — BTC, ETH, SOL, HYPE, PAXG (gold), XMR, and
200+ memecoins. Trading commands need a wallet funded from the testnet faucet.
"""

from __future__ import annotations

import argparse
import sys

import yaml
from dotenv import load_dotenv

from bot.hyperliquid.client import HyperliquidClient
from bot.wallet import DefiWallet


def load_hl_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("hyperliquid", {})


def resolve_wallet() -> DefiWallet | None:
    return DefiWallet.from_env() or DefiWallet.load()


def client_for(hl: dict, need_wallet: bool) -> HyperliquidClient:
    testnet = hl.get("testnet", True)
    wallet = resolve_wallet()
    if need_wallet and wallet is None:
        sys.exit("No wallet found. Run:  python hyperwallet.py create")
    return HyperliquidClient.connect(
        private_key=wallet.private_key if wallet else "",
        testnet=testnet,
    )


def cmd_create(args, hl):
    w = DefiWallet.create(testnet=hl.get("testnet", True))
    path = w.save()
    print("New TESTNET deposit wallet created.\n")
    print(f"  Address:     {w.address}")
    print(f"  Private key: {w.private_key}")
    print(f"  Saved to:    {path}  (gitignored)")
    print(f"\nFund it (free testnet USDC): {w.faucet_url}")
    print("Paste the address above into the faucet, then check:  python hyperwallet.py balance")
    print("\n⚠  TESTNET only — never fund this key with real money or reuse it on mainnet.")


def cmd_address(args, hl):
    w = resolve_wallet()
    if w is None:
        sys.exit("No wallet found. Run:  python hyperwallet.py create")
    print(f"Address: {w.address}")
    print(f"Faucet:  {w.faucet_url}")


def cmd_markets(args, hl):
    client = client_for(hl, need_wallet=False)
    if args.group == "majors":
        names = hl.get("majors")
    elif args.group == "memes":
        names = hl.get("memecoins")
    else:
        names = None  # all
    markets = client.markets(names)
    markets.sort(key=lambda m: m.name)
    print(f"{'COIN':<12}{'PRICE':>16}{'MAX LEV':>10}")
    for m in markets:
        print(f"{m.name:<12}{m.mid:>16,.6g}{m.max_leverage:>9}x")
    print(f"\n{len(markets)} markets ({args.group}).")


def cmd_balance(args, hl):
    acct = client_for(hl, need_wallet=True).account()
    print(f"Address:      {acct.address}")
    print(f"Account value: ${acct.account_value:,.2f}")
    print(f"Withdrawable:  ${acct.withdrawable:,.2f}")
    print(f"Open positions: {len(acct.positions)}")
    if acct.account_value == 0:
        w = resolve_wallet()
        print(f"\nEmpty — fund it at {w.faucet_url}")


def cmd_positions(args, hl):
    acct = client_for(hl, need_wallet=True).account()
    if not acct.positions:
        print("No open positions.")
        return
    print(f"{'COIN':<10}{'SIDE':<7}{'SIZE':>14}{'ENTRY':>14}{'uPnL':>12}{'LEV':>6}")
    for p in acct.positions:
        print(f"{p.coin:<10}{p.side:<7}{p.size:>14,.6g}{p.entry:>14,.6g}"
              f"{p.unrealized_pnl:>+12,.2f}{p.leverage:>5.0f}x")


def _trade(args, hl, is_long: bool):
    client = client_for(hl, need_wallet=True)
    lev = args.lev if args.lev is not None else hl.get("default_leverage")
    side = "LONG" if is_long else "SHORT"
    print(f"{side} ${args.usd:g} of {args.coin}" + (f" at {lev}x" if lev else "") + " (testnet)...")
    fn = client.long if is_long else client.short
    result = fn(args.coin, args.usd, leverage=lev)
    print(result)


def cmd_long(args, hl):
    _trade(args, hl, is_long=True)


def cmd_short(args, hl):
    _trade(args, hl, is_long=False)


def cmd_close(args, hl):
    client = client_for(hl, need_wallet=True)
    print(f"Closing {args.coin} (testnet)...")
    print(client.close(args.coin))


def main() -> None:
    load_dotenv()
    hl = load_hl_config()

    parser = argparse.ArgumentParser(description="DeFi wallet + Hyperliquid perps (testnet)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("create").set_defaults(func=cmd_create)
    sub.add_parser("address").set_defaults(func=cmd_address)

    p_mkt = sub.add_parser("markets")
    p_mkt.add_argument("--group", choices=["majors", "memes", "all"], default="majors")
    p_mkt.set_defaults(func=cmd_markets)

    sub.add_parser("balance").set_defaults(func=cmd_balance)
    sub.add_parser("positions").set_defaults(func=cmd_positions)

    for name, fn in (("long", cmd_long), ("short", cmd_short)):
        p = sub.add_parser(name)
        p.add_argument("coin")
        p.add_argument("usd", type=float)
        p.add_argument("--lev", type=int, default=None)
        p.set_defaults(func=fn)

    p_close = sub.add_parser("close")
    p_close.add_argument("coin")
    p_close.set_defaults(func=cmd_close)

    args = parser.parse_args()
    args.func(args, hl)


if __name__ == "__main__":
    main()
