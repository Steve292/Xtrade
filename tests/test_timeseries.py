"""
Tests for bot/timeseries.py — the persisted rolling-EMA helper. No network.

Run directly (`python tests/test_timeseries.py`) or under pytest.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.timeseries import ema, load_samples, record_sample, trend

DAY = 86400.0


def _tmp_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".json")
    import os

    os.close(fd)
    p = Path(name)
    p.unlink()  # record_sample/load_samples must behave against a missing file
    return p


def test_missing_file_has_no_samples():
    path = _tmp_path()
    assert load_samples(path, "btc_d") == []
    assert trend(path, "btc_d") is None


def test_single_sample_is_not_enough_for_a_trend():
    path = _tmp_path()
    try:
        record_sample(path, "btc_d", 55.0, now=1000.0)
        assert trend(path, "btc_d", min_samples=2) is None
    finally:
        path.unlink(missing_ok=True)


def test_rising_trend_when_latest_is_above_its_ema():
    path = _tmp_path()
    try:
        t0 = 1_700_000_000.0
        for i, v in enumerate([50.0, 50.0, 50.0, 60.0]):  # jump on the last sample
            record_sample(path, "btc_d", v, now=t0 + i * DAY)
        assert trend(path, "btc_d", span_days=7, now=t0 + 3 * DAY) == "rising"
    finally:
        path.unlink(missing_ok=True)


def test_falling_trend_when_latest_is_below_its_ema():
    path = _tmp_path()
    try:
        t0 = 1_700_000_000.0
        for i, v in enumerate([50.0, 50.0, 50.0, 40.0]):
            record_sample(path, "btc_d", v, now=t0 + i * DAY)
        assert trend(path, "btc_d", span_days=7, now=t0 + 3 * DAY) == "falling"
    finally:
        path.unlink(missing_ok=True)


def test_pruning_drops_samples_older_than_max_age():
    path = _tmp_path()
    try:
        t0 = 1_700_000_000.0
        record_sample(path, "btc_d", 10.0, now=t0, max_age_days=5)
        record_sample(path, "btc_d", 20.0, now=t0 + 10 * DAY, max_age_days=5)  # prunes the first
        samples = load_samples(path, "btc_d", max_age_days=5, now=t0 + 10 * DAY)
        assert samples == [(t0 + 10 * DAY, 20.0)]
    finally:
        path.unlink(missing_ok=True)


def test_series_are_independent_per_key_in_one_file():
    path = _tmp_path()
    try:
        record_sample(path, "btc_d", 55.0, now=1000.0)
        record_sample(path, "meme_share", 1.2, now=1000.0)
        assert load_samples(path, "btc_d", now=1000.0) == [(1000.0, 55.0)]
        assert load_samples(path, "meme_share", now=1000.0) == [(1000.0, 1.2)]
    finally:
        path.unlink(missing_ok=True)


def test_ema_seeds_from_first_sample():
    assert ema([(0.0, 42.0)], span_days=7) == 42.0


def test_ema_barely_moves_for_a_tiny_time_step():
    # A sample one second after the first should barely nudge a 7-day EMA.
    e = ema([(0.0, 50.0), (1.0, 100.0)], span_days=7)
    assert e is not None and abs(e - 50.0) < 0.01


def test_corrupt_state_file_is_treated_as_empty():
    path = _tmp_path()
    try:
        path.write_text("{not valid json")
        assert load_samples(path, "btc_d", now=1000.0) == []
        record_sample(path, "btc_d", 1.0, now=1000.0)  # must not raise
        assert load_samples(path, "btc_d", now=1000.0) == [(1000.0, 1.0)]
    finally:
        path.unlink(missing_ok=True)


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
