"""
Tests for bot/backtest/engine.py's calmar_ratio (new — added for the
walk-forward optimizer's deploy/revert gate). BacktestResult's other
properties (sharpe_ratio, max_drawdown_pct, etc.) predate this and are
exercised indirectly by bot/backtest/optimizer.py's own tests.

Run directly (`python tests/test_backtest.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.backtest.engine import BacktestResult, _infer_bars_per_year


def test_calmar_ratio_positive_return_with_drawdown():
    # 1000 -> 1200 (steady climb) with one dip to 900 along the way.
    curve = [1000, 1050, 900, 1000, 1100, 1200]
    result = BacktestResult(equity_curve=curve, initial_balance=1000, final_balance=1200)
    assert result.max_drawdown_pct > 0
    assert result.calmar_ratio > 0


def test_calmar_ratio_zero_drawdown_positive_return_is_infinite():
    curve = [1000, 1050, 1100, 1200]  # monotonic climb, never dips below prior peak
    result = BacktestResult(equity_curve=curve, initial_balance=1000, final_balance=1200)
    assert result.max_drawdown_pct == 0
    assert result.calmar_ratio == float("inf")


def test_calmar_ratio_zero_growth_and_zero_drawdown_is_zero():
    curve = [1000, 1000, 1000]
    result = BacktestResult(equity_curve=curve, initial_balance=1000, final_balance=1000)
    assert result.calmar_ratio == 0.0


def test_calmar_ratio_is_negative_for_a_losing_curve():
    # A real losing strategy should read as a NEGATIVE Calmar (worse than a
    # flat 0.0, which is reserved for the true "no data"/zero-growth case).
    curve = [1000, 900, 800]
    result = BacktestResult(equity_curve=curve, initial_balance=1000, final_balance=800)
    assert result.calmar_ratio < 0.0


def test_calmar_ratio_growth_of_zero_or_below_is_zero():
    # growth <= 0 is the actual degenerate case (e.g. balance wiped out).
    curve = [1000, 500, 0]
    result = BacktestResult(equity_curve=curve, initial_balance=1000, final_balance=0)
    assert result.calmar_ratio == 0.0


def test_calmar_ratio_needs_at_least_two_points():
    result = BacktestResult(equity_curve=[1000], initial_balance=1000, final_balance=1000)
    assert result.calmar_ratio == 0.0
    result_empty = BacktestResult(equity_curve=[], initial_balance=1000, final_balance=1000)
    assert result_empty.calmar_ratio == 0.0


def test_infer_bars_per_year_for_15m_matches_the_old_hardcoded_constant():
    ts = pd.date_range("2026-01-01", periods=10, freq="15min").tolist()
    assert abs(_infer_bars_per_year(ts) - 252 * 24 * 4) < 1e-6


def test_infer_bars_per_year_for_1h_is_a_quarter_of_15m():
    # The real bug this fixes: 1h bars have 1/4 as many bars/day as 15m, so
    # the OLD hardcoded-15m assumption over-annualized Sharpe/Calmar by ~2x
    # (sqrt(4)) whenever the optimizer ran on 1h data (its practical default).
    ts_15m = pd.date_range("2026-01-01", periods=10, freq="15min").tolist()
    ts_1h = pd.date_range("2026-01-01", periods=10, freq="1h").tolist()
    assert abs(_infer_bars_per_year(ts_1h) - _infer_bars_per_year(ts_15m) / 4) < 1e-6


def test_infer_bars_per_year_falls_back_with_too_few_timestamps():
    assert _infer_bars_per_year([]) == 252 * 24 * 4
    assert _infer_bars_per_year([pd.Timestamp("2026-01-01")]) == 252 * 24 * 4


def test_sharpe_ratio_uses_the_results_own_bars_per_year():
    curve = [1000, 1010, 1005, 1020, 1015, 1030]
    fast = BacktestResult(equity_curve=curve, bars_per_year=252 * 24 * 4)  # 15m
    slow = BacktestResult(equity_curve=curve, bars_per_year=252 * 24)  # 1h -- 1/4 as many
    assert fast.sharpe_ratio > 0 and slow.sharpe_ratio > 0
    assert abs(fast.sharpe_ratio / slow.sharpe_ratio - 2.0) < 1e-6  # sqrt(4) = 2


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
