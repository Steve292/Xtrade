"""
Walk-forward parameter optimizer — blueprint Section 8, built on top of the
EXISTING BacktestEngine/SMCStrategy (bot/backtest/engine.py, bot/smc/
strategy.py) rather than a from-scratch system: SMCStrategy is the actual
live strategy trading real money on both venues, so "optimize the strategy's
parameters" means searching over ITS 5 real tunable constructor arguments
(swing_lookback, order_block_lookback, fvg_min_size_pct,
liquidity_tolerance_pct, reward_risk_ratio), not inventing a second,
unrelated system.

Three deliberate simplifications from the blueprint's literal wording, each
documented at the point it matters:
  - "Bayesian search" -> a curated 9-combo grid (default_param_grid). A full
    Bayesian optimizer is a real dependency for a search space this small;
    a grid covers the practically distinct choices without one.
  - "Regime-Aware Params: 5 separate parameter sets" -> _window_regime_bucket
    classifies a backtest window by ITS OWN realized trend, not true
    historical macro regime (which would need historical VIX/DXY/yield
    series aligned to each window — a separate, larger undertaking; see
    bot/regime.py for why that data is hard to get for free even for a
    single LIVE snapshot, let alone a historical series).
  - "ε-Greedy RL... 9 combinations run in parallel" -> run sequentially (no
    infrastructure here runs 9 simultaneous paper accounts) and select via
    real epsilon-greedy: 90% best-by-Sharpe, 10% a random runner-up.

This module NEVER writes to config.yaml or any live state — it returns a
report. Section 8's "if Sharpe > 1.5 and Calmar > 1.5, deploy" becomes a
RECOMMENDATION a human reviews and applies themselves, the same
report-then-a-human-acts boundary this project draws around every other
consequential action (profit-locking, position-flushing, live order
execution).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pandas as pd

from bot.backtest.engine import BacktestEngine, BacktestResult
from bot.smc.strategy import SMCStrategy

_DEFAULTS = {
    "swing_lookback": 5,
    "order_block_lookback": 20,
    "fvg_min_size_pct": 0.001,
    "liquidity_tolerance_pct": 0.0005,
    "reward_risk_ratio": 2.0,
}


def default_param_grid(current_params: dict) -> list[dict]:
    """9 candidates: the current live config (combo 0) plus 8 single-
    parameter perturbations of it — see module docstring for why this
    replaces literal Bayesian search."""
    base = {**_DEFAULTS, **current_params}
    variations = [
        {},
        {"swing_lookback": max(2, base["swing_lookback"] - 2)},
        {"swing_lookback": base["swing_lookback"] + 2},
        {"order_block_lookback": max(5, base["order_block_lookback"] - 10)},
        {"order_block_lookback": base["order_block_lookback"] + 10},
        {"reward_risk_ratio": max(1.0, base["reward_risk_ratio"] - 0.5)},
        {"reward_risk_ratio": base["reward_risk_ratio"] + 1.0},
        {"liquidity_tolerance_pct": base["liquidity_tolerance_pct"] * 2},
        {"fvg_min_size_pct": base["fvg_min_size_pct"] * 2},
    ]
    return [{**base, **delta} for delta in variations]


def _window_regime_bucket(df_window: pd.DataFrame) -> str:
    """Proxy regime bucket from the window's OWN realized return — see
    module docstring for why this isn't the true macro regime. 5 buckets to
    match the blueprint's "5 separate parameter sets" number."""
    start, end = float(df_window["close"].iloc[0]), float(df_window["close"].iloc[-1])
    if start == 0:
        return "RANGE"
    total_return_pct = (end - start) / start * 100
    if total_return_pct > 15:
        return "STRONG_UP"
    if total_return_pct > 3:
        return "WEAK_UP"
    if total_return_pct > -3:
        return "RANGE"
    if total_return_pct > -15:
        return "WEAK_DOWN"
    return "STRONG_DOWN"


def _split_windows(
    df: pd.DataFrame, optimize_days: int, test_days: int, min_bars: int = 50
) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Time-based (not bar-count) walk-forward split: the most recent
    `test_days` is the held-out forward-test window, the `optimize_days`
    immediately before that is what candidates are ranked on. None if
    there isn't enough real history for both windows to be meaningful."""
    if df.empty:
        return None
    end_ts = df["timestamp"].max()
    test_start = end_ts - pd.Timedelta(days=test_days)
    optimize_start = test_start - pd.Timedelta(days=optimize_days)
    optimize_window = df[(df["timestamp"] >= optimize_start) & (df["timestamp"] < test_start)].reset_index(drop=True)
    test_window = df[df["timestamp"] >= test_start].reset_index(drop=True)
    if len(optimize_window) < min_bars or len(test_window) < min_bars:
        return None
    return optimize_window, test_window


@dataclass
class ParamCandidateResult:
    params: dict
    optimize_sharpe: float
    optimize_calmar: float
    is_current: bool = False


@dataclass
class WalkForwardReport:
    regime_bucket: str
    candidates: list[ParamCandidateResult] = field(default_factory=list)
    chosen_params: dict = field(default_factory=dict)
    chosen_by: str = "best"  # "best" | "explore"
    test_sharpe: float = 0.0
    test_calmar: float = 0.0
    recommendation: str = "KEEP_CURRENT"  # "DEPLOY" | "KEEP_CURRENT"
    current_params: dict = field(default_factory=dict)


def run_walk_forward(
    df: pd.DataFrame,
    current_params: dict,
    htf: str = "1h",
    optimize_days: int = 180,
    test_days: int = 30,
    epsilon: float = 0.1,
    sharpe_threshold: float = 1.5,
    calmar_threshold: float = 1.5,
    risk_pct: float = 1.0,
    initial_balance: float = 10000.0,
    rng: random.Random | None = None,
) -> WalkForwardReport | None:
    """Runs the full optimize -> select -> forward-validate cycle once.
    Returns None if `df` doesn't cover enough real history for both windows
    (rather than silently optimizing on too little data)."""
    windows = _split_windows(df, optimize_days, test_days)
    if windows is None:
        return None
    optimize_window, test_window = windows
    rng = rng or random.Random()

    grid = default_param_grid(current_params)
    candidates: list[ParamCandidateResult] = []
    for i, params in enumerate(grid):
        strategy = SMCStrategy(**params)
        engine = BacktestEngine(strategy, initial_balance=initial_balance, risk_pct=risk_pct)
        result = engine.run(optimize_window, htf=htf)
        candidates.append(
            ParamCandidateResult(
                params=params,
                optimize_sharpe=result.sharpe_ratio,
                optimize_calmar=result.calmar_ratio,
                is_current=(i == 0),
            )
        )

    ranked = sorted(candidates, key=lambda c: c.optimize_sharpe, reverse=True)
    if len(ranked) > 1 and rng.random() < epsilon:
        chosen, chosen_by = rng.choice(ranked[1:]), "explore"
    else:
        chosen, chosen_by = ranked[0], "best"

    test_strategy = SMCStrategy(**chosen.params)
    test_engine = BacktestEngine(test_strategy, initial_balance=initial_balance, risk_pct=risk_pct)
    test_result: BacktestResult = test_engine.run(test_window, htf=htf)

    recommendation = (
        "DEPLOY"
        if test_result.sharpe_ratio > sharpe_threshold and test_result.calmar_ratio > calmar_threshold
        else "KEEP_CURRENT"
    )

    return WalkForwardReport(
        regime_bucket=_window_regime_bucket(optimize_window),
        candidates=candidates,
        chosen_params=chosen.params,
        chosen_by=chosen_by,
        test_sharpe=test_result.sharpe_ratio,
        test_calmar=test_result.calmar_ratio,
        recommendation=recommendation,
        current_params={**_DEFAULTS, **current_params},
    )


def format_walk_forward_report(report: WalkForwardReport) -> str:
    lines = [
        "=" * 64,
        "  Walk-Forward Optimization Report",
        "=" * 64,
        f"  Regime bucket (realized-trend proxy, not true macro regime): {report.regime_bucket}",
        f"  Candidates evaluated: {len(report.candidates)}",
        "",
        "  Optimize-window ranking (by Sharpe):",
    ]
    ranked = sorted(report.candidates, key=lambda c: c.optimize_sharpe, reverse=True)
    for i, c in enumerate(ranked, 1):
        marker = "  <- current live params" if c.is_current else ""
        lines.append(
            f"    {i}. Sharpe {c.optimize_sharpe:+.2f} / Calmar {c.optimize_calmar:+.2f}  {c.params}{marker}"
        )
    lines += [
        "",
        f"  Chosen ({report.chosen_by}): {report.chosen_params}",
        f"  Forward test-window Sharpe: {report.test_sharpe:+.2f}  (needs > 1.5)",
        f"  Forward test-window Calmar: {report.test_calmar:+.2f}  (needs > 1.5)",
        "",
        f"  RECOMMENDATION: {report.recommendation}",
    ]
    if report.recommendation == "DEPLOY":
        lines.append(
            "  -> If you agree, update config.yaml's SMC parameters yourself — "
            "nothing here writes to it automatically."
        )
    else:
        lines.append("  -> Keep the current live parameters; the challenger didn't clear the forward-test bar.")
    lines.append("=" * 64)
    return "\n".join(lines)
