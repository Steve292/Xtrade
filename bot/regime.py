"""
Regime Detection Engine — the macro/crypto "gatekeeper".

Weighted 0-100 score across 8 macro factors, each scored as a binary
risk-on-condition-met/not-met — the source blueprint gives a single
threshold per factor (e.g. "VIX < 22"), not a graduated curve, so this is a
direct, literal implementation of that table rather than an invented
smoothing function.

ETF 7-day net flow and exchange-reserve 7-day change have no free data
source (checked: this project has no Glassnode/CoinGlass/ETF-flow API key
anywhere in .env.example). When a caller can't supply one, pass `None` —
that factor's weight is redistributed proportionally across whichever
factors DO have data this pass, so a missing input degrades the score's
precision rather than silently dragging it toward RISK_OFF just because
data's unavailable. Stablecoin SSR is NOT in that bucket: it's computed for
real from CoinGecko's free /global + /coins/categories endpoints (BTC market
cap / stablecoin market cap) — see bot/marketdata.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# name -> (weight, risk_on_check). Weights sum to 100.
_FACTORS: dict[str, tuple[int, "callable"]] = {
    "yield_10y_4h_change_pct": (15, lambda v: v < 0.05),
    "dxy_24h_change_pct": (15, lambda v: v < 0.3),
    "vix_level": (15, lambda v: v < 22),
    "yield_curve_10y_3m": (10, lambda v: v > -0.25),
    "btc_24h_change_pct": (15, lambda v: v > 2.0),
    "etf_7d_net_flow_usd": (15, lambda v: v > 0),
    "stablecoin_ssr": (10, lambda v: v < 3.0),
    "exchange_reserve_7d_change_pct": (5, lambda v: v < 0),
}

# (score floor, label) — checked highest-first, first match wins.
_LABELS: tuple[tuple[int, str], ...] = (
    (70, "RISK_ON_GROWTH"),
    (60, "RISK_ON_INFLATION"),
    (40, "NEUTRAL"),
    (25, "STAGFLATION"),
    (0, "RISK_OFF"),
)


@dataclass
class RegimeInputs:
    yield_10y_4h_change_pct: float | None = None
    dxy_24h_change_pct: float | None = None
    vix_level: float | None = None
    yield_curve_10y_3m: float | None = None
    btc_24h_change_pct: float | None = None
    etf_7d_net_flow_usd: float | None = None
    stablecoin_ssr: float | None = None
    exchange_reserve_7d_change_pct: float | None = None


@dataclass
class RegimeResult:
    score: float
    regime: str
    factors: dict[str, bool] = field(default_factory=dict)  # available factors -> passed?
    missing: list[str] = field(default_factory=list)  # factors with no data this pass


def _label_for(score: float) -> str:
    for floor, label in _LABELS:
        if score >= floor:
            return label
    return "RISK_OFF"  # unreachable: the last floor is 0


def score_regime(inputs: RegimeInputs) -> RegimeResult:
    available: list[tuple[str, int, bool]] = []
    missing: list[str] = []
    for name, (weight, check) in _FACTORS.items():
        value = getattr(inputs, name)
        if value is None:
            missing.append(name)
        else:
            available.append((name, weight, bool(check(value))))

    if not available:
        # No data at all this pass. NEUTRAL is the honest "don't know" — not
        # RISK_OFF, which would falsely assert a bearish signal from silence.
        return RegimeResult(score=50.0, regime="NEUTRAL", missing=missing)

    total_weight = sum(w for _, w, _ in available)
    earned = sum(w for _, w, passed in available if passed)
    score = earned / total_weight * 100

    return RegimeResult(
        score=score,
        regime=_label_for(score),
        factors={name: passed for name, _, passed in available},
        missing=missing,
    )
