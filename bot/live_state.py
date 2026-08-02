"""
Runtime state shared between the trading loop process and the control panel
process (two separate Python processes — this file is how they talk). `--live`
on the CLI stays the infra-level capability switch (baked into the launchd
plist); these flags are the moment-to-moment controls, toggleable from the
control panel without restarting the service.

Two runtime controls live here:
  - armed: master on/off. Disarmed = screen only, never place an order.
    Defaults to disarmed if the file doesn't exist — a fresh checkout, or a
    control panel that's never been used, must never trade live by accident.
  - min_confidence: an authorization floor (0.0–1.0). Only signals whose SMC
    confidence is at or above this fire. 0.0 = no extra runtime gate beyond
    config.yaml's own min_confidence. Raising it means "only auto-authorize
    the higher-probability setups."
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path("live_state.json")


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict, path: Path) -> None:
    path.write_text(json.dumps(data))


def is_armed(path: Path = DEFAULT_PATH) -> bool:
    return bool(_read(path).get("armed", False))


def set_armed(armed: bool, path: Path = DEFAULT_PATH) -> None:
    data = _read(path)
    data["armed"] = bool(armed)
    _write(data, path)


def get_min_confidence(path: Path = DEFAULT_PATH) -> float:
    """Runtime authorization floor in [0, 1]. Malformed/missing -> 0.0 (no
    extra gate), the permissive-toward-config default, so a corrupt value can
    never silently block every trade without an obvious reason."""
    try:
        v = float(_read(path).get("min_confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def set_min_confidence(value: float, path: Path = DEFAULT_PATH) -> None:
    data = _read(path)
    data["min_confidence"] = max(0.0, min(1.0, float(value)))
    _write(data, path)
