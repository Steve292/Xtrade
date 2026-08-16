#!/usr/bin/env python3
"""
Multi-chain balance scanner for funding the Hyperliquid deposit wallet.
Read-only -- checks native + stablecoin balances across several EVM chains
(and TRC20 USDT on Tron, if given) so you know the shortest path to get
funds into Hyperliquid.

    python scan_deposits.py                  # scan the saved/env DeFi wallet's address
    python scan_deposits.py --address 0x...  # scan an explicit address
    python scan_deposits.py --tron T...      # also scan TRC20 USDT on Tron

Tron address can also come from the TRON_ACCOUNT_ADDRESS env var.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from bot.funding import scan_and_report
from bot.wallet import DefiWallet


def main() -> None:
    load_dotenv()

    ap = argparse.ArgumentParser(description="Scan a wallet's balances across chains (read-only)")
    ap.add_argument("--address", help="EVM address to scan (defaults to the saved/env DeFi wallet)")
    ap.add_argument("--tron", default=os.getenv("TRON_ACCOUNT_ADDRESS", ""),
                     help="Tron address to also scan for TRC20 USDT (or set TRON_ACCOUNT_ADDRESS)")
    args = ap.parse_args()

    address = args.address
    if not address:
        wallet = DefiWallet.from_env() or DefiWallet.load()
        if wallet is None:
            sys.exit("No wallet found and no --address given. Run:  python hyperwallet.py create")
        address = wallet.address

    print(scan_and_report(address, args.tron))


if __name__ == "__main__":
    main()
