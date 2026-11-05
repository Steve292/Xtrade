"""
Shadow tracker for the 4h trend pullback: records what the strategy WOULD do,
live, and never touches the account. Read-only by construction -- this module
only does arithmetic on candles, and tests/test_shadow_trend.py fails if order
code ever appears here or in scripts/shadow_trend.py.

Rules are the ones that passed the stress test on real MT5 data (2026-11-04):
long when the last closed daily is above its 50 SMA and the 4h EMA20 is above
EMA50, and a closed 4h bar dipped to EMA20 and closed back above it (shorts
mirrored). Enter at the next bar's open, stop 1.5 x ATR14, exit on a 4h close
through EMA50. The live loop and the replay both go through step(), so what is
tracked cannot drift from what was tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Params:
    fast: int = 20
    slow: int = 50
    daily_sma: int = 50
    atr_period: int = 14
    atr_mult: float = 1.5


@dataclass
class State:
    pending: dict | None = None
    position: dict | None = None
    trades: list = field(default_factory=list)


def add_indicators(h4: pd.DataFrame, d1: pd.DataFrame, p: Params = Params()) -> pd.DataFrame:
    """`h4` must hold CLOSED bars only. A daily row carries the PREVIOUS day's
    close-vs-SMA verdict, so a forming daily bar cannot leak into a signal."""
    daily = d1[["timestamp", "close"]].copy()
    sma = daily["close"].rolling(p.daily_sma).mean()
    daily["up"] = (daily["close"] > sma).shift(1)
    daily["dn"] = (daily["close"] < sma).shift(1)

    d = h4.reset_index(drop=True).copy()
    d["ef"] = d["close"].ewm(span=p.fast).mean()
    d["es"] = d["close"].ewm(span=p.slow).mean()
    prev = d["close"].shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(p.atr_period).mean()
    return pd.merge_asof(d, daily[["timestamp", "up", "dn"]], on="timestamp")


def _signal(bar) -> int:
    if pd.isna(bar.atr):
        return 0
    if bar.up is True and bar.ef > bar.es and bar.low <= bar.ef and bar.close > bar.ef:
        return 1
    if bar.dn is True and bar.ef < bar.es and bar.high >= bar.ef and bar.close < bar.ef:
        return -1
    return 0


def step(state: State, bar, spread: float, swap: tuple, p: Params = Params()) -> dict | None:
    """Advance one CLOSED 4h bar. Returns the trade it closed, if any."""
    if state.pending:
        sig = state.pending
        state.pending = None
        side = sig["side"]
        entry = bar.open + side * spread / 2
        state.position = {
            "side": side, "entry": entry, "stop": entry - side * p.atr_mult * sig["atr"],
            "signal_time": sig["time"], "entry_time": str(bar.timestamp),
        }

    pos = state.position
    if pos:
        side, stop = pos["side"], pos["stop"]
        px = None
        reason = None
        if side == 1 and bar.low <= stop:
            px, reason = min(stop, bar.open), "stop"   # a gap through the stop fills at the open
        elif side == -1 and bar.high >= stop:
            px, reason = max(stop, bar.open), "stop"
        elif (side == 1 and bar.close < bar.es) or (side == -1 and bar.close > bar.es):
            px, reason = bar.close - side * spread / 2, "trend_exit"
        if px is None:
            return None
        days = (pd.Timestamp(bar.timestamp) - pd.Timestamp(pos["signal_time"])) / pd.Timedelta(days=1)
        carry = (swap[0] if side == 1 else swap[1]) * days
        risk = abs(pos["entry"] - stop)
        trade = {**pos, "exit": float(px), "exit_time": str(bar.timestamp), "reason": reason,
                 "days": float(days), "R": float((side * (px - pos["entry"]) + carry) / risk)}
        state.position = None
        state.trades.append(trade)
        return trade

    side = _signal(bar)
    if side:
        state.pending = {"side": side, "atr": float(bar.atr), "time": str(bar.timestamp)}
    return None


def replay(df: pd.DataFrame, spread: float, swap: tuple, p: Params = Params(),
           start: int | None = None) -> State:
    """Run the same step() over history -- the backtest and the verifier."""
    state = State()
    first = p.slow + 10 if start is None else start
    for bar in df.iloc[first:].itertuples(index=False):
        step(state, bar, spread, swap, p)
    return state


def summarize(trades: list) -> dict:
    r = np.array([t["R"] for t in trades], dtype=float)
    if not len(r):
        return {"trades": 0}
    equity = r.cumsum()
    return {"trades": len(r), "win_rate": float((r > 0).mean()), "net_r": float(r.sum()),
            "per_trade_r": float(r.mean()),
            "max_drawdown_r": float((equity - np.maximum.accumulate(equity)).min())}
