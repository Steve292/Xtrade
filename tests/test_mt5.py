"""
Isolated tests for the MT5 venue — no live bridge or account required.

Covers lot sizing math and a paper-mode dry-run of the broker adapter against a
stub client. Run directly (`python tests/test_mt5.py`) or under pytest.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.mt5.broker import MT5Broker
from bot.mt5.client import SymbolInfo
from bot.risk import calc_lot_size

# EURUSD-like specs: 5-digit, 1 tick = $1 per 1.0 lot, 0.01 lot granularity.
EURUSD = SymbolInfo(
    name="EURUSD",
    digits=5,
    point=0.00001,
    tick_size=0.00001,
    tick_value=1.0,
    volume_min=0.01,
    volume_max=100.0,
    volume_step=0.01,
    contract_size=100000.0,
)


class StubClient:
    """Minimal stand-in for MT5Client used by the broker in paper mode."""

    def __init__(self, info: SymbolInfo):
        self._info = info

    def symbol_info(self, symbol: str) -> SymbolInfo:
        return self._info

    def copy_rates(self, symbol: str, timeframe: str, count: int = 200) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2025-01-01", periods=count, freq="15min"),
                "open": [1.1] * count,
                "high": [1.1] * count,
                "low": [1.1] * count,
                "close": [1.1] * count,
                "volume": [100] * count,
            }
        )


def test_lot_size_basic():
    # 1% of 10k = $100 risk; 100-point stop => loss_per_lot = 100 * $1 = $100 => 1.0 lot.
    lots = calc_lot_size(EURUSD, balance=10000, entry=1.10000, stop_loss=1.09900, risk_pct=1.0)
    assert abs(lots - 1.0) < 1e-9, lots


def test_lot_size_floored_to_step():
    # Risk-correct size 0.153... floors to 0.15 (step 0.01), not rounded up.
    lots = calc_lot_size(EURUSD, balance=10000, entry=1.10000, stop_loss=1.09347, risk_pct=1.0)
    assert lots == 0.15, lots


def test_lot_size_below_min_returns_zero():
    # Tiny risk -> size below volume_min -> skip rather than over-risk.
    lots = calc_lot_size(EURUSD, balance=10000, entry=1.10000, stop_loss=1.09900, risk_pct=0.001)
    assert lots == 0.0, lots


def test_lot_size_clamped_to_max():
    # 2% risk with a 1-point stop would want 200 lots; clamps to volume_max=100.
    lots = calc_lot_size(EURUSD, balance=10000, entry=1.10000, stop_loss=1.09999, risk_pct=2.0)
    assert lots == 100.0, lots


def test_lot_size_degenerate_inputs():
    assert calc_lot_size(EURUSD, 10000, 1.1, 1.1, 1.0) == 0.0  # zero stop distance
    zero_tick = SymbolInfo("X", 5, 0.00001, 0.0, 1.0, 0.01, 100.0, 0.01, 100000.0)
    assert calc_lot_size(zero_tick, 10000, 1.10000, 1.09900, 1.0) == 0.0


def test_broker_paper_long_tp():
    broker = MT5Broker(StubClient(EURUSD), symbol="EURUSD", mode="paper", initial_balance=10000)
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                         reason="test", symbol="EURUSD")
    assert broker.position is not None and broker.position.side == "long"

    closed = broker.check_exit(1.10200)  # TP
    assert closed
    assert broker.position is None
    # 200 points * $1 * 1.0 lot = +$200
    assert math.isclose(broker.balance, 10200.0, abs_tol=1e-6), broker.balance
    assert broker.trade_log[-1]["outcome"] == "TP"


def test_broker_paper_long_sl():
    broker = MT5Broker(StubClient(EURUSD), symbol="EURUSD", mode="paper", initial_balance=10000)
    broker.open_position("long", entry=1.10000, size=1.0, sl=1.09900, tp=1.10200,
                         reason="test", symbol="EURUSD")
    broker.check_exit(1.09900)  # SL
    assert math.isclose(broker.balance, 9900.0, abs_tol=1e-6), broker.balance
    assert broker.trade_log[-1]["outcome"] == "SL"


def test_broker_paper_short_tp():
    broker = MT5Broker(StubClient(EURUSD), symbol="EURUSD", mode="paper", initial_balance=10000)
    broker.open_position("short", entry=1.10000, size=1.0, sl=1.10100, tp=1.09800,
                         reason="test", symbol="EURUSD")
    broker.check_exit(1.09800)  # short TP (price falls)
    assert math.isclose(broker.balance, 10200.0, abs_tol=1e-6), broker.balance
    assert broker.trade_log[-1]["outcome"] == "TP"


def test_broker_only_one_position():
    broker = MT5Broker(StubClient(EURUSD), symbol="EURUSD", mode="paper", initial_balance=10000)
    broker.open_position("long", 1.10000, 1.0, 1.09900, 1.10200, "a", "EURUSD")
    broker.open_position("long", 1.10000, 2.0, 1.09900, 1.10200, "b", "EURUSD")
    assert broker.position.size == 1.0  # second open ignored while one is open


def test_broker_fetch_ohlcv_shape():
    broker = MT5Broker(StubClient(EURUSD), symbol="EURUSD", mode="paper")
    df = broker.fetch_ohlcv("EURUSD", "15m", limit=10)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 10


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
