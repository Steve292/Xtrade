#!/usr/bin/env python3
"""
MT5 bridge smoke test — run this FIRST once your remote endpoint is up.

Connects to the MT5 bridge using the MT5_* values in .env, then prints account
info, the symbol's trading specs, and the most recent candles. It never sends
an order, so it is safe to run against a live demo account.

    python scripts/mt5_smoke_test.py            # uses mt5_symbol from config.yaml
    python scripts/mt5_smoke_test.py XAUUSD     # override the symbol
"""

from __future__ import annotations

import os
import sys

import yaml
from dotenv import load_dotenv

from bot.mt5.client import MT5Client


def main() -> None:
    load_dotenv()
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    symbol = sys.argv[1] if len(sys.argv) > 1 else config.get("mt5_symbol", "EURUSD")
    timeframe = config.get("mt5_timeframe", "15m")
    host = os.getenv("MT5_HOST", "127.0.0.1")
    port = os.getenv("MT5_PORT", "18812")

    print(f"Connecting to MT5 bridge at {host}:{port} ...")
    client = MT5Client.connect(
        host=host,
        port=port,
        login=os.getenv("MT5_LOGIN", ""),
        password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""),
    )
    print("  connected.\n")

    print(f"Account balance: {client.account_balance():,.2f}\n")

    info = client.symbol_info(symbol)
    print(f"Symbol specs — {info.name}")
    print(f"  digits={info.digits} point={info.point}")
    print(f"  tick_size={info.tick_size} tick_value={info.tick_value}")
    print(f"  volume min/step/max = {info.volume_min}/{info.volume_step}/{info.volume_max}")
    print(f"  contract_size={info.contract_size}\n")

    bid, ask = client.tick(symbol)
    print(f"Current tick — bid={bid} ask={ask} spread={ask - bid:.{info.digits}f}\n")

    df = client.copy_rates(symbol, timeframe, count=5)
    print(f"Last 5 {timeframe} candles:")
    print(df.to_string(index=False))
    print("\nSmoke test passed — the bridge, symbol, and data feed all work.")


if __name__ == "__main__":
    main()
