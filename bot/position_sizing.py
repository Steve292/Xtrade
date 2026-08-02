"""
Position sizing formula — blueprint Section 6.

    final_risk_pct = base_risk_pct * regime_alloc_weight * hotness_multiplier
                      * volatility_adjust * confidence_multiplier

This is a pure sizing CALCULATOR — it returns a risk percentage, it does not
size or place an order. bot/risk.py's calc_position_size()/calc_lot_size()
already turn a risk_pct into an actual dollar/lot amount for Hyperliquid/MT5
respectively; this module feeds that same risk_pct input, it doesn't
replace it.

Not wired into bot/runner.py's live execute() path yet — deliberately, this
first pass keeps every multiplier here informational only (surfaced on the
dashboard), gated behind a config flag before it can change what either live
account actually risks per trade.

The blueprint's individual multiplier caps (hotness up to 2.5x, volatility
up to 2.0x) can compound past its own apparent intent when combined naively
(2.5 * 2.0 = 5x base risk on one trade) — final_risk_pct() clamps the
combined non-base-risk multiplier to MAX_COMBINED_MULTIPLIER, because
capital preservation matters more than literal fidelity to numbers that
were never specified to multiply against each other unchecked.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

BASE_RISK_PCT = {
    "btc": 2.0,
    "eth": 2.0,
    "alt": 1.5,
    "meme": 1.0,
    "equity": 1.5,
    "commodity": 1.0,
}

MAX_COMBINED_MULTIPLIER = 3.0
MIN_COMBINED_MULTIPLIER = 0.0

# The blueprint names "Regime Alloc Weight" as a Section 6 sizing factor but
# never specifies the actual regime-label -> weight mapping — this project's
# regime labels (bot/regime.py) didn't previously connect to sizing at all,
# so this is a real gap being closed, not a verbatim spec. Own reasonable,
# documented interpretation, same spirit as bot/hotness.py's meme-score
# normalization constants: risk-on growth scales up modestly, stagflation
# scales down hard, risk-off is a full stop.
_REGIME_ALLOC_WEIGHT = {
    "RISK_ON_GROWTH": 1.2,
    "RISK_ON_INFLATION": 1.0,
    "NEUTRAL": 1.0,
    "STAGFLATION": 0.6,
    "RISK_OFF": 0.0,
}


def regime_alloc_weight(regime_label: str) -> float:
    """Maps a bot/regime.py label to a position-sizing allocation weight.
    Unknown labels default to 1.0 (neutral) rather than raising."""
    return _REGIME_ALLOC_WEIGHT.get(regime_label, 1.0)


def staged_fixed_risk_usd(
    combined_balance: float,
    low_risk_usd: float = 3.0,
    high_risk_usd: float = 6.0,
    threshold_usd: float = 100.0,
) -> float:
    """Step-function fixed-dollar risk per trade — `low_risk_usd` while
    combined capital+profit (both venues) is below `threshold_usd`,
    `high_risk_usd` once it reaches or exceeds it. A user-specified rule,
    not derived from the blueprint. `combined_balance` is meant to be the
    SAME figure bot/combined_ledger.py's fetch_combined_balance() already
    computes for the cross-venue capital guard, not a per-venue balance."""
    return high_risk_usd if combined_balance >= threshold_usd else low_risk_usd


def risk_pct_for_fixed_usd(risk_usd: float, balance: float) -> float:
    """Converts a fixed-dollar risk target into the risk_pct that
    bot/risk.py's calc_position_size()/calc_lot_size() already expect.
    Meant to be recomputed fresh every pass against the CURRENT balance —
    that's what keeps the actual dollar risk pinned near risk_usd as
    balance drifts, rather than the risk_pct itself being a stale, one-time
    snapshot. 0.0 (skip sizing) for a non-positive balance rather than a
    division error or a nonsensical negative/inf percentage."""
    if balance <= 0:
        return 0.0
    return risk_usd / balance * 100


def asset_class_base_risk_pct(asset_class: str) -> float:
    try:
        return BASE_RISK_PCT[asset_class.lower()]
    except KeyError:
        raise ValueError(
            f"unknown asset class {asset_class!r} — expected one of {sorted(BASE_RISK_PCT)}"
        )


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR from a DataFrame with high/low/close columns (same shape
    HyperliquidClient.candles() / MT5's candle fetch already produce). The
    first `period` rows are NaN — not enough bars yet for a first reading."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def volatility_adjust(atr_20: float, atr_100_avg: float) -> float:
    """1.0 / (ATR_20 / ATR_100_avg), clamped to [0.3, 2.0]: current volatility
    running hot relative to its own recent history shrinks size; running
    cool lets it grow, within a sane band either way."""
    if atr_100_avg <= 0:
        return 1.0
    raw = (1.0 / (atr_20 / atr_100_avg)) if atr_20 > 0 else 2.0
    return max(0.3, min(2.0, raw))


@dataclass
class SizingFactors:
    base_risk_pct: float
    regime_alloc_weight: float = 1.0
    hotness_multiplier: float = 1.0
    volatility_adjust: float = 1.0
    confidence_multiplier: float = 1.0


def final_risk_pct(factors: SizingFactors) -> float:
    """The final position-risk percentage, after clamping the combined
    (non-base-risk) multiplier — see module docstring for why."""
    combined = (
        factors.regime_alloc_weight
        * factors.hotness_multiplier
        * factors.volatility_adjust
        * factors.confidence_multiplier
    )
    combined = max(MIN_COMBINED_MULTIPLIER, min(MAX_COMBINED_MULTIPLIER, combined))
    return factors.base_risk_pct * combined


@dataclass
class VolumeExhaustionCheck:
    action: str  # "HOLD" | "EXIT_80_PCT" | "CANCEL_TPS_TRAIL_FULL"


def check_volume_exhaustion(breakout_volume: float, current_30m_volume: float) -> VolumeExhaustionCheck:
    """Section 6's TP1 volume-confirmation rule — only meaningful once TP1
    has actually been hit (the caller decides that; this just classifies the
    volume shape). Report-only, same as every other portfolio-level call in
    this module: it returns a recommended action, it doesn't execute one."""
    if breakout_volume <= 0:
        return VolumeExhaustionCheck("HOLD")
    change_pct = (current_30m_volume - breakout_volume) / breakout_volume * 100
    if change_pct <= -35:
        return VolumeExhaustionCheck("EXIT_80_PCT")
    if change_pct >= 50:
        return VolumeExhaustionCheck("CANCEL_TPS_TRAIL_FULL")
    return VolumeExhaustionCheck("HOLD")
