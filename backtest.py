#!/usr/bin/env python3
"""Run SMC strategy backtest on historical data."""

import argparse
import os
import sys

import yaml
from dotenv import load_dotenv

from bot.backtest.engine import BacktestEngine, format_report
from bot.exchange import Exchange
from bot.smc.strategy import SMCStrategy


def fetch_history(
    symbol: str, timeframe: str, bars: int, exchange_id: str = "binance"
) -> "pd.DataFrame":
    import pandas as pd

    ex = Exchange(exchange_id=exchange_id, mode="paper")
    all_bars: list = []
    since = None

    while len(all_bars) < bars:
        batch = ex.client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        all_bars = batch + all_bars if since else batch
        since = batch[0][0] - ex.client.parse_timeframe(timeframe) * 1000
        if len(batch) < 1000:
            break

    all_bars = all_bars[-bars:]
    df = pd.DataFrame(
        all_bars, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="SMC strategy backtester")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--bars", type=int, default=None, help="Historical bars to fetch (default: config.yaml's backtest_bars, or 2000)"
    )
    parser.add_argument("--csv", default="", help="Use local CSV instead of live fetch")
    parser.add_argument("--output", default="", help="Save equity curve CSV")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    bars = args.bars if args.bars is not None else config.get("backtest_bars", 2000)

    strategy = SMCStrategy(
        swing_lookback=config.get("swing_lookback", 5),
        order_block_lookback=config.get("order_block_lookback", 20),
        fvg_min_size_pct=config.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=config.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=config.get("reward_risk_ratio", 2.0),
    )

    if args.csv:
        import pandas as pd

        df = pd.read_csv(args.csv, parse_dates=["timestamp"])
    else:
        print(f"Fetching {bars} bars of {config['symbol']} ({config['timeframe']})...")
        df = fetch_history(
            config["symbol"],
            config["timeframe"],
            bars,
            os.getenv("EXCHANGE", "binance"),
        )

    engine = BacktestEngine(
        strategy=strategy,
        initial_balance=config.get("initial_balance", 10000.0),
        risk_pct=config.get("risk_per_trade_pct", 1.0),
    )

    result = engine.run(df, htf=config.get("higher_timeframe", "1h"))
    print(format_report(result))

    if args.output and result.timestamps:
        import pandas as pd

        pd.DataFrame({"timestamp": result.timestamps, "equity": result.equity_curve}).to_csv(
            args.output, index=False
        )
        print(f"\nEquity curve saved to {args.output}")


if __name__ == "__main__":
    main()
