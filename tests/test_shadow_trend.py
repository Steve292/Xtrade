"""Shadow tracker: rule behaviour, no lookahead, outage catch-up, and the
guarantee that it contains no way to place an order."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from bot.shadow_trend import Params, State, add_indicators, replay, step, summarize


def _h4(n=1500, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0.05, 0.8, n))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="4h"), "open": open_,
        "high": np.maximum(open_, close) + rng.uniform(0.05, 0.7, n),
        "low": np.minimum(open_, close) - rng.uniform(0.05, 0.7, n), "close": close, "volume": 1.0,
    })


def _d1(h4):
    d = h4.set_index("timestamp")["close"].resample("1D").last().dropna().reset_index()
    return d


def test_no_order_code_anywhere_in_shadow_mode():
    for f in ("bot/shadow_trend.py", "scripts/shadow_trend.py"):
        src = (ROOT / f).read_text()
        for banned in ("market_order", "order_send", "open_position", "close_position", "TRADE_ACTION"):
            assert banned not in src, f"{banned} found in {f}"


def test_long_stop_fills_at_the_open_when_price_gaps_through():
    st = State(position={"side": 1, "entry": 100.0, "stop": 98.0,
                         "signal_time": "2025-01-01 00:00:00", "entry_time": "2025-01-01 04:00:00"})
    bar = pd.Series({"timestamp": pd.Timestamp("2025-01-02"), "open": 95.0, "high": 96.0, "low": 94.0,
                     "close": 95.0, "es": 99.0}).to_frame().T.itertuples(index=False).__next__()
    trade = step(st, bar, 0.0, (0.0, 0.0))
    assert trade["reason"] == "stop" and trade["exit"] == 95.0 and trade["R"] == -2.5


def test_future_bars_cannot_change_past_trades():
    h4 = _h4(); d1 = _d1(h4)
    full = replay(add_indicators(h4, d1), 0.1, (0.0, 0.0)).trades
    cut = h4.iloc[:1000]
    early = replay(add_indicators(cut, d1[d1.timestamp <= cut.timestamp.iloc[-1]]), 0.1, (0.0, 0.0)).trades
    assert len(early) > 0
    assert [t["entry_time"] for t in early] == [t["entry_time"] for t in full][:len(early)]


def test_catching_up_after_an_outage_equals_running_straight_through():
    df = add_indicators(_h4(), _d1(_h4()))
    whole = replay(df, 0.1, (0.0, 0.0), start=60).trades
    assert whole
    st = State()
    for bar in df.iloc[60:700].itertuples(index=False):
        step(st, bar, 0.1, (0.0, 0.0))
    resumed = State(pending=st.pending, position=st.position, trades=st.trades)   # as saved to JSON
    for bar in df.iloc[700:].itertuples(index=False):
        step(resumed, bar, 0.1, (0.0, 0.0))
    assert resumed.trades == whole


def test_swap_cost_reduces_a_long():
    df = add_indicators(_h4(), _d1(_h4()))
    free = replay(df, 0.1, (0.0, 0.0)).trades
    paid = replay(df, 0.1, (-0.5, 0.0)).trades
    assert sum(t["R"] for t in paid) < sum(t["R"] for t in free)


def test_summary_of_nothing_is_safe():
    assert summarize([]) == {"trades": 0}
