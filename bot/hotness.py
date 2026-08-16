"""
"What's Hot" 4-factor matrix + Meme Season Score.

detect_hotness() is a direct, literal implementation of the source
blueprint's 3-critical-signal table — no interpretation needed there, it's
already binary conditions on 4 trend directions.

meme_season_score() is NOT that precise: the blueprint gives component
weights and a conceptual input for each ("MEME.D Trend", "BTC.D Inverse",
...) but no exact normalization formula, so the 0-100 sub-scores below are
this module's own reasonable, documented interpretation — pick different
scaling constants and you'd get a different (still defensible) number. Where
the blueprint calls for a signal with no free data source (a genuine
MEME.C/TOTAL "acceleration" distinct from its own trend), this reuses the
nearest real signal already computed rather than inventing a fake one — see
the comment at that component.

Both functions are pure — they take already-computed trend directions /
dominance snapshots (see bot/timeseries.py + bot/marketdata.py for how those
get produced) and never do their own I/O, so they're fully unit-testable
against plain values.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DominanceTrend:
    """"rising" | "falling" | "flat" | None (not enough history yet) for
    each of the 4-factor matrix's inputs — see bot/timeseries.py's `trend()`."""

    btc_d: str | None = None
    meme_share: str | None = None
    others_d: str | None = None
    stable_c: str | None = None


@dataclass
class HotnessResult:
    signal: str  # MEME_ROTATION_ACTIVE | ALT_SEASON_STARTING | RISK_OFF_WARNING | NEUTRAL
    multiplier: float
    confidence: float  # fraction (0-1) of the 4 factors that had trend data this pass


def detect_hotness(trend: DominanceTrend) -> HotnessResult:
    btc_falling = trend.btc_d == "falling"
    btc_rising = trend.btc_d == "rising"
    meme_rising = trend.meme_share == "rising"
    others_rising = trend.others_d == "rising"
    stable_rising = trend.stable_c == "rising"

    inputs = (trend.btc_d, trend.meme_share, trend.others_d, trend.stable_c)
    confidence = sum(1 for v in inputs if v is not None) / len(inputs)

    # Order matters: MEME_ROTATION_ACTIVE is a strict superset of
    # ALT_SEASON_STARTING's two conditions, so check it first.
    if btc_falling and meme_rising and others_rising:
        return HotnessResult("MEME_ROTATION_ACTIVE", 2.5, confidence)
    if btc_falling and meme_rising:
        return HotnessResult("ALT_SEASON_STARTING", 2.0, confidence)
    if btc_rising and stable_rising:
        return HotnessResult("RISK_OFF_WARNING", 0.0, confidence)
    return HotnessResult("NEUTRAL", 1.0, confidence)


@dataclass
class MemeScoreInputs:
    meme_dominance_change_24h_pct: float | None = None  # MEME.D Trend (25%) + MEME.C/TOTAL accel (10%)
    meme_top10_avg_return_7d_pct: float | None = None  # MEME.C Momentum (20%)
    btc_dominance_pct: float | None = None  # BTC.D Inverse (20%)
    others_dominance_change_24h_pct: float | None = None  # OTHERS.D Trend (15%)
    stablecoin_dominance_pct: float | None = None  # STABLE.C Inverse (10%)


@dataclass
class MemeScoreResult:
    score: float
    zone: str
    action: str
    size_multiplier: float
    missing: list[str] = field(default_factory=list)


def _scale(value: float, low: float, high: float) -> float:
    """Map value linearly from [low, high] to [0, 100], clamped outside."""
    if high == low:
        return 50.0
    return max(0.0, min(100.0, (value - low) / (high - low) * 100))


def _climate_action(score: float) -> tuple[str, str, float]:
    if score >= 80:
        return (
            "PEAK_MANIA",
            "Take 75% profits; halt new meme entries until score < 65 "
            "(report only — a human executes the actual close)",
            0.0,
        )
    if score >= 60:
        return "ACTIVE_SEASON", "Increase meme allocation 10-20%", 1.8
    if score >= 40:
        return "WARMING_UP", "Standard sizing", 1.3
    if score >= 20:
        return "COOLING", "Reduce sizing; exit if the score keeps dropping", 0.5
    return (
        "MEME_WINTER",
        "Exit all meme positions (report only — a human executes the actual close)",
        0.0,
    )


def meme_season_score(inputs: MemeScoreInputs) -> MemeScoreResult:
    components: list[tuple[str, float, float]] = []
    missing: list[str] = []

    def add(name: str, weight: float, value: float | None, scorer) -> None:
        if value is None:
            missing.append(name)
        else:
            components.append((name, weight, scorer(value)))

    add("meme_d_trend", 25, inputs.meme_dominance_change_24h_pct, lambda v: _scale(v, -10, 10))
    add("meme_c_momentum", 20, inputs.meme_top10_avg_return_7d_pct, lambda v: _scale(v, -50, 50))
    add("btc_d_inverse", 20, inputs.btc_dominance_pct, lambda v: 100 - _scale(v, 35, 65))
    add(
        "others_d_trend",
        15,
        inputs.others_dominance_change_24h_pct,
        lambda v: _scale(v, -5, 5),
    )
    add(
        "stable_c_inverse",
        10,
        inputs.stablecoin_dominance_pct,
        lambda v: 100 - _scale(v, 5, 20),
    )
    # MEME.C/TOTAL acceleration: no free source distinguishes this from the
    # meme dominance trend above, so it deliberately reuses that same input
    # at its own blueprint-specified weight rather than fabricating a
    # separate number.
    add(
        "meme_c_total_accel",
        10,
        inputs.meme_dominance_change_24h_pct,
        lambda v: _scale(v, -10, 10),
    )

    if not components:
        return MemeScoreResult(
            score=50.0,
            zone="WARMING_UP",
            action="No data this pass — treating as neutral",
            size_multiplier=1.0,
            missing=missing,
        )

    total_weight = sum(w for _, w, _ in components)
    weighted = sum(w * s for _, w, s in components)
    score = weighted / total_weight

    zone, action, multiplier = _climate_action(score)
    return MemeScoreResult(
        score=score, zone=zone, action=action, size_multiplier=multiplier, missing=missing
    )
