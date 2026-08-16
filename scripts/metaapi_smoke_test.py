#!/usr/bin/env python3
"""
MetaApi.cloud smoke test — run this FIRST once you have a MetaApi API token
and a broker MT5 account (login/password/server). Connects, deploys the
account with MetaApi if it isn't already, then prints account info, the
symbol's trading specs, and the most recent candles. It never sends an
order, so it is safe to run against a live demo account.

    python scripts/metaapi_smoke_test.py            # uses mt5_symbol from config.yaml
    python scripts/metaapi_smoke_test.py XAUUSD     # override the symbol

First connect can take a couple of minutes — MetaApi has to deploy and
synchronize the cloud terminal before anything else responds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from dotenv import load_dotenv

from bot.mt5.metaapi_client import MetaApiClient


def main() -> None:
    load_dotenv()
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    symbol = sys.argv[1] if len(sys.argv) > 1 else config.get("mt5_symbol", "EURUSD")
    timeframe = config.get("mt5_timeframe", "15m")

    token = os.getenv("METAAPI_TOKEN", "")
    login = os.getenv("MT5_LOGIN", "")
    server = os.getenv("MT5_SERVER", "")
    if not token or not login or not server:
        sys.exit(
            "Missing METAAPI_TOKEN / MT5_LOGIN / MT5_SERVER in .env — set these yourself "
            "(never paste the value to me), see docs/MT5_SETUP.md."
        )

    print("Connecting to MetaApi.cloud (first run may take a couple of minutes "
          "while it deploys the cloud terminal) ...")
    client = MetaApiClient.connect(
        token=token, login=login, password=os.getenv("MT5_PASSWORD", ""), server=server
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

    try:
        df = client.copy_rates(symbol, timeframe, count=5)
        print(f"Last 5 {timeframe} candles:")
        print(df.to_string(index=False))
        print("\nSmoke test passed — MetaApi, the symbol, and the data feed all work.")
    except RuntimeError as e:
        print(f"Candle fetch failed: {e}")
        print(
            "\nAccount balance, symbol specs and quotes all worked, but historical "
            "candles did not — MetaApi documents this RPC as G1-only, and this account "
            "may have provisioned as G2. mt5_backend: metaapi cannot run the strategy "
            "without candles; ask MetaApi support to move this account to G1, or fall "
            "back to mt5_backend: bridge."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
