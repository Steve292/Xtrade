"""
Tests for the shared arm/disarm flag (bot/live_state.py) — no network.

Run directly (`python tests/test_live_state.py`) or under pytest.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import live_state


def _tmp_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".json")
    import os
    os.close(fd)
    p = Path(name)
    p.unlink()
    return p


def test_defaults_to_disarmed_when_file_missing():
    path = _tmp_path()  # never created
    assert live_state.is_armed(path) is False


def test_set_then_get_armed():
    path = _tmp_path()
    try:
        live_state.set_armed(True, path)
        assert live_state.is_armed(path) is True
        live_state.set_armed(False, path)
        assert live_state.is_armed(path) is False
    finally:
        path.unlink(missing_ok=True)


def test_corrupt_file_defaults_to_disarmed():
    path = _tmp_path()
    try:
        path.write_text("not valid json{{{")
        assert live_state.is_armed(path) is False  # fail safe, never fail open
    finally:
        path.unlink(missing_ok=True)


def test_min_confidence_defaults_to_zero():
    path = _tmp_path()  # never created
    assert live_state.get_min_confidence(path) == 0.0


def test_set_then_get_min_confidence():
    path = _tmp_path()
    try:
        live_state.set_min_confidence(0.85, path)
        assert live_state.get_min_confidence(path) == 0.85
    finally:
        path.unlink(missing_ok=True)


def test_min_confidence_clamped_to_unit_interval():
    path = _tmp_path()
    try:
        live_state.set_min_confidence(1.5, path)
        assert live_state.get_min_confidence(path) == 1.0
        live_state.set_min_confidence(-0.2, path)
        assert live_state.get_min_confidence(path) == 0.0
    finally:
        path.unlink(missing_ok=True)


def test_confidence_and_armed_coexist_without_clobbering():
    # Setting one must not wipe the other — they share one JSON file.
    path = _tmp_path()
    try:
        live_state.set_armed(True, path)
        live_state.set_min_confidence(0.75, path)
        assert live_state.is_armed(path) is True
        assert live_state.get_min_confidence(path) == 0.75
        live_state.set_armed(False, path)  # flip armed, confidence must survive
        assert live_state.is_armed(path) is False
        assert live_state.get_min_confidence(path) == 0.75
    finally:
        path.unlink(missing_ok=True)


def test_corrupt_file_defaults_confidence_to_zero():
    path = _tmp_path()
    try:
        path.write_text("garbage{{")
        assert live_state.get_min_confidence(path) == 0.0
    finally:
        path.unlink(missing_ok=True)


# ---- auto_fire_pct (hands-off threshold vs. the blended final_pct) --------

def test_auto_fire_pct_defaults_to_100_so_nothing_fires_unattended():
    # Opposite default to min_confidence on purpose: absent config must mean
    # "queue everything for approval", never "fire everything".
    path = _tmp_path()  # never created
    assert live_state.get_auto_fire_pct(path) == 100.0


def test_set_then_get_auto_fire_pct():
    path = _tmp_path()
    try:
        live_state.set_auto_fire_pct(90.0, path)
        assert live_state.get_auto_fire_pct(path) == 90.0
    finally:
        path.unlink(missing_ok=True)


def test_auto_fire_pct_clamped_to_0_100():
    path = _tmp_path()
    try:
        live_state.set_auto_fire_pct(140.0, path)
        assert live_state.get_auto_fire_pct(path) == 100.0
        live_state.set_auto_fire_pct(-5.0, path)
        assert live_state.get_auto_fire_pct(path) == 0.0
    finally:
        path.unlink(missing_ok=True)


def test_corrupt_auto_fire_pct_fails_safe_to_100():
    path = _tmp_path()
    try:
        path.write_text('{"auto_fire_pct": "not-a-number"}')
        assert live_state.get_auto_fire_pct(path) == 100.0
    finally:
        path.unlink(missing_ok=True)


def test_all_three_controls_coexist_without_clobbering():
    path = _tmp_path()
    try:
        live_state.set_armed(True, path)
        live_state.set_min_confidence(0.55, path)
        live_state.set_auto_fire_pct(90.0, path)
        assert live_state.is_armed(path) is True
        assert live_state.get_min_confidence(path) == 0.55
        assert live_state.get_auto_fire_pct(path) == 90.0
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
