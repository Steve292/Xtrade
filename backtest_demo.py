#!/usr/bin/env python3
"""Offline backtest demo using synthetic data (no network needed)."""

import numpy as np
import pandas as pd

from bot.backtest.engine import BacktestEngine, format_report
from bot.smc.strategy import SMCStrategy


def generate_data(bars: int = 3000) -> pd.DataFrame:
    np.random.seed(7)
    price = 65000.0
    rows = []
    ts = pd.Timestamp("2025-01-01")

    for i in range(bars):
        # Inject occasional trends and sweeps
        drift = 80 * np.sin(i / 80) + np.random.randn() * 200
        o = price
        c = max(price + drift, 1000)
        h = max(o, c) + abs(np.random.randn() * 150)
        l = min(o, c) - abs(np.random.randn() * 150)
        rows.append([ts, o, h, l, c, abs(np.random.randn() * 500)])
        price = c
        ts += pd.Timedelta(minutes=15)

    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


if __name__ == "__main__":
    df = generate_data(3000)
    engine = BacktestEngine(SMCStrategy(), initial_balance=10000, risk_pct=1.0)
    result = engine.run(df, htf="1h")
    print(format_report(result))
