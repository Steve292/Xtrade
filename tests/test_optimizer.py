"""
Tests for bot/backtest/optimizer.py — the Section 8 walk-forward optimizer.
No network. The pure helpers (default_param_grid, _window_regime_bucket,
_split_windows) get precise assertions; run_walk_forward() itself is
exercised end-to-end against a small synthetic OHLCV series checking
structural invariants (well-formed report, correct None-on-insufficient-data
behavior, deterministic epsilon-greedy branch selection via an injected rng)
rather than exact Sharpe/Calmar numbers, which would be fragile against
synthetic data. See scripts/walk_forward_optimize.py for a live run against
real historical data.

Run directly (`python tests/test_optimizer.py`) or under pytest.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from bot.backtest.optimizer import (
    ParamCandidateResult,
    _split_windows,
    _window_regime_bucket,
    default_param_grid,
    run_walk_forward,
)

CURRENT = {
    "swing_lookback": 5,
    "order_block_lookback": 20,
    "fvg_min_size_pct": 0.001,
    "liquidity_tolerance_pct": 0.0005,
    "reward_risk_ratio": 2.0,
}


# --- default_param_grid -----------------------------------------------


def test_grid_has_nine_combos():
    grid = default_param_grid(CURRENT)
    assert len(grid) == 9


def test_grid_first_combo_is_the_current_params():
    grid = default_param_grid(CURRENT)
    assert grid[0] == CURRENT


def test_grid_fills_in_missing_keys_with_defaults():
    grid = default_param_grid({"swing_lookback": 8})
    assert grid[0]["order_block_lookback"] == 20  # untouched key gets the module default
    assert grid[0]["swing_lookback"] == 8


def test_grid_perturbations_only_change_one_key_each():
    grid = default_param_grid(CURRENT)
    for combo in grid[1:]:
        diffs = [k for k in CURRENT if combo[k] != CURRENT[k]]
        assert len(diffs) == 1, f"{combo} differs from current in {diffs}"


def test_grid_swing_lookback_floor_is_respected():
    grid = default_param_grid({**CURRENT, "swing_lookback": 3})
    lookbacks = [c["swing_lookback"] for c in grid]
    assert min(lookbacks) >= 2  # max(2, 3-2)=2, never goes to 1 or below


# --- _window_regime_bucket -----------------------------------------------


def _closes_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_regime_bucket_boundaries():
    assert _window_regime_bucket(_closes_df([100, 120])) == "STRONG_UP"
    assert _window_regime_bucket(_closes_df([100, 105])) == "WEAK_UP"
    assert _window_regime_bucket(_closes_df([100, 101])) == "RANGE"
    assert _window_regime_bucket(_closes_df([100, 95])) == "WEAK_DOWN"
    assert _window_regime_bucket(_closes_df([100, 80])) == "STRONG_DOWN"


def test_regime_bucket_handles_zero_start():
    assert _window_regime_bucket(_closes_df([0, 50])) == "RANGE"


# --- _split_windows ---------------------------------------------------


def _synthetic_candles(days: float, interval_minutes: int = 15, start="2026-01-01") -> pd.DataFrame:
    """A gently oscillating-with-trend series -- enough real swing structure
    for SMCStrategy to have a chance at finding something, not pure noise."""
    bars = int(days * 24 * 60 / interval_minutes)
    ts = pd.date_range(start=start, periods=bars, freq=f"{interval_minutes}min")
    t = np.arange(bars)
    trend = t * 0.02
    wave = 40 * np.sin(t / 12.0) + 15 * np.sin(t / 47.0)
    close = 20000 + trend + wave
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 3, bars)
    close = close + noise
    high = close + np.abs(rng.normal(5, 2, bars))
    low = close - np.abs(rng.normal(5, 2, bars))
    open_ = close + rng.normal(0, 2, bars)
    volume = np.abs(rng.normal(100, 20, bars))
    return pd.DataFrame(
        {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


def test_split_windows_returns_none_with_too_little_history():
    df = _synthetic_candles(days=5)  # far short of optimize_days + test_days
    assert _split_windows(df, optimize_days=180, test_days=30) is None


def test_split_windows_splits_by_calendar_time():
    df = _synthetic_candles(days=20)
    result = _split_windows(df, optimize_days=10, test_days=5, min_bars=10)
    assert result is not None
    optimize_window, test_window = result
    assert optimize_window["timestamp"].max() <= test_window["timestamp"].min()
    span_days = (test_window["timestamp"].max() - test_window["timestamp"].min()).total_seconds() / 86400
    assert span_days <= 5.1  # test window shouldn't overrun its requested span


# --- run_walk_forward ---------------------------------------------------



# BacktestEngine re-scans the whole window on every single bar (an existing,
# pre-this-session performance characteristic — see
# scripts/walk_forward_optimize.py's docstring for the real-data implication)
# — so these integration tests deliberately use the smallest windows that
# still clear SMCStrategy's own 50-bar analysis floor, to keep the suite
# fast. optimize_days/test_days of 0.65 -> ~62 bars each at 15m.
_SMALL_KWARGS = dict(optimize_days=0.65, test_days=0.65)


def test_run_walk_forward_returns_none_on_insufficient_data():
    df = _synthetic_candles(days=0.5)  # far short of even the smallest windows
    report = run_walk_forward(df, CURRENT, **_SMALL_KWARGS)
    assert report is None


def test_run_walk_forward_produces_a_well_formed_report():
    df = _synthetic_candles(days=1.4)
    report = run_walk_forward(df, CURRENT, epsilon=0.0, **_SMALL_KWARGS)
    assert report is not None
    assert len(report.candidates) == 9
    assert report.recommendation in ("DEPLOY", "KEEP_CURRENT")
    assert report.regime_bucket in ("STRONG_UP", "WEAK_UP", "RANGE", "WEAK_DOWN", "STRONG_DOWN")
    assert report.chosen_params in [c.params for c in report.candidates]
    assert report.chosen_by == "best"  # epsilon=0.0 -> always the top candidate


def test_epsilon_one_always_explores():
    df = _synthetic_candles(days=1.4)
    report = run_walk_forward(df, CURRENT, epsilon=1.0, rng=random.Random(2), **_SMALL_KWARGS)
    best = max(report.candidates, key=lambda c: c.optimize_sharpe)
    assert report.chosen_by == "explore"
    assert report.chosen_params != best.params or len(report.candidates) == 1


def test_deploy_requires_both_thresholds_not_just_one():
    # Impossible-to-clear calmar_threshold must force KEEP_CURRENT even
    # though sharpe_threshold is trivially satisfied — proves both gates are
    # required, not just one.
    df = _synthetic_candles(days=1.4)
    report = run_walk_forward(
        df, CURRENT, epsilon=0.0, sharpe_threshold=-999, calmar_threshold=999999, **_SMALL_KWARGS
    )
    assert report.recommendation == "KEEP_CURRENT"


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
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
