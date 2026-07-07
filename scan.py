#!/usr/bin/env python3
"""Run one-shot SMC analysis (offline demo or live)."""

import argparse
import sys

import numpy as np
import pandas as pd

from bot.smc.strategy import SMCStrategy, SignalType


def generate_sample_data(bars: int = 200) -> pd.DataFrame:
    """Generate synthetic OHLCV resembling BTC price action."""
    np.random.seed(42)
    price = 65000.0
    rows = []
    ts = pd.Timestamp.now().floor("15min") - pd.Timedelta(minutes=15 * bars)

    for _ in range(bars):
        change = np.random.randn() * 150
        o = price
        c = price + change
        h = max(o, c) + abs(np.random.randn() * 80)
        l = min(o, c) - abs(np.random.randn() * 80)
        v = abs(np.random.randn() * 100)
        rows.append([ts, o, h, l, c, v])
        price = c
        ts += pd.Timedelta(minutes=15)

    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC strategy one-shot scan")
    parser.add_argument("--live", action="store_true", help="Fetch live data from exchange")
    parser.add_argument("--symbol", default="BTC/USDT")
    args = parser.parse_args()

    strategy = SMCStrategy()

    if args.live:
        from bot.exchange import Exchange

        ex = Exchange(mode="paper")
        df = ex.fetch_ohlcv(args.symbol, "15m", 200)
        htf_df = ex.fetch_ohlcv(args.symbol, "1h", 100)
        source = "LIVE"
    else:
        df = generate_sample_data(200)
        htf_df = generate_sample_data(100)
        source = "DEMO (synthetic)"

    signal = strategy.analyze(df, htf_df)
    price = float(df.iloc[-1]["close"])

    print("=" * 50)
    print(f"  SMC Scan — {source}")
    print("=" * 50)
    print(f"  Price:      ${price:,.2f}")
    print(f"  Signal:     {signal.type.value.upper()}")
    print(f"  Confidence: {signal.confidence:.0%}")
    print(f"  Reason:     {signal.reason}")

    if signal.type != SignalType.NONE:
        print(f"  Entry:      ${signal.entry:,.2f}")
        print(f"  Stop Loss:  ${signal.stop_loss:,.2f}")
        print(f"  Take Profit:${signal.take_profit:,.2f}")
        rr = abs(signal.take_profit - signal.entry) / abs(signal.entry - signal.stop_loss)
        print(f"  R:R:        1:{rr:.1f}")

    print("=" * 50)


if __name__ == "__main__":
    main()
