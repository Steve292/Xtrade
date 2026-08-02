"""
Persisted rolling-sample series with a time-aware EMA/trend helper.

Why this exists: several regime/hotness metrics (BTC dominance, meme-sector
share, ...) are defined relative to their own 7-day EMA, but the free-tier
data sources this bot uses only ever return a live snapshot — no historical
dominance/category series is available without a paid plan. Rather than fake
a backfilled history, this records one real sample per call (matching the
blueprint's own "update every 4 hours" cadence) and computes the EMA over
whatever history has actually accumulated, the same way capital_guard_state
.json / live_state.json persist real state across restarts instead of
resetting it. Cold-start is honest: with fewer than `min_samples`, callers
get `None` back and are expected to degrade gracefully rather than guess.

All series for a given file live together as `{key: [[epoch_seconds, value], ...]}`
so related metrics (e.g. the 4-factor dominance matrix) share one state file
instead of scattering one JSON file per metric across the repo root.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path


def _load_raw(path: Path) -> dict[str, list[list[float]]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def record_sample(
    path: Path,
    key: str,
    value: float,
    now: float | None = None,
    max_age_days: float = 30.0,
) -> None:
    """Append one (timestamp, value) sample under `key`, pruning anything
    older than `max_age_days`. `now` is epoch seconds, injectable for tests."""
    now = time.time() if now is None else now
    data = _load_raw(path)
    series = data.get(key, [])
    series.append([now, value])
    cutoff = now - max_age_days * 86400
    data[key] = [s for s in series if s[0] >= cutoff]
    path.write_text(json.dumps(data))


def load_samples(
    path: Path,
    key: str,
    max_age_days: float = 30.0,
    now: float | None = None,
) -> list[tuple[float, float]]:
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    return [(t, v) for t, v in _load_raw(path).get(key, []) if t >= cutoff]


def ema(samples: list[tuple[float, float]], span_days: float) -> float | None:
    """Time-aware EMA. Samples land at irregular intervals (whenever the bot
    polls), so a fixed-N-period EMA would silently misweight uneven gaps —
    each step instead uses alpha = 1 - exp(-dt / span), the continuous-time
    equivalent, derived from the actual elapsed time since the previous
    sample. The first sample seeds the EMA outright."""
    if not samples:
        return None
    ordered = sorted(samples, key=lambda s: s[0])
    value = ordered[0][1]
    prev_t = ordered[0][0]
    span_seconds = span_days * 86400
    for t, v in ordered[1:]:
        dt = max(t - prev_t, 0.0)
        alpha = 1.0 - math.exp(-dt / span_seconds) if span_seconds > 0 else 1.0
        value = alpha * v + (1 - alpha) * value
        prev_t = t
    return value


def trend(
    path: Path,
    key: str,
    span_days: float = 7.0,
    min_samples: int = 2,
    max_age_days: float = 30.0,
    now: float | None = None,
) -> str | None:
    """'rising' if the latest sample sits above its own EMA, 'falling' if
    below, 'flat' if equal, or None if fewer than `min_samples` have been
    recorded yet (the metric is still bootstrapping)."""
    samples = load_samples(path, key, max_age_days=max_age_days, now=now)
    if len(samples) < min_samples:
        return None
    latest = sorted(samples, key=lambda s: s[0])[-1][1]
    e = ema(samples, span_days)
    if e is None:
        return None
    if latest > e:
        return "rising"
    if latest < e:
        return "falling"
    return "flat"
