"""
Pending-approval queue for setups that cleared the unified gate but landed
BELOW the auto-fire threshold (bot/live_state.py's auto_fire_pct).

At explicit user request: "fires 90% rated trades on automation and
responsive dashboard to enable approve/cancel trade". So the live loops now
split their approved candidates two ways:

  final_pct >= auto_fire_pct  -> fire immediately, no human step
  final_pct <  auto_fire_pct  -> queued HERE, awaiting Approve or Cancel
                                 from the control panel

Before this, a below-threshold setup was simply dropped with a log line and
was gone — there was no way to say "actually, take that one." That's the gap
this fills.

Cross-process, like bot/live_state.py: the two live loops (hypertrade.py,
bot/runner.py) WRITE entries, and the Flask control panel (webapp/server.py)
reads them and marks approve/cancel. Three separate OS processes share this
one file, so writes are atomic (tmp file + os.replace) rather than a plain
write_text — a dashboard reading mid-write must never see a truncated file
and silently conclude the queue is empty.

ENTRIES ARE PROPOSALS, NEVER ORDERS. Nothing here places a trade. An entry
records what the loop saw at the time; the actual order is only ever sent by
webapp/server.py's approve endpoint, which independently RE-SCREENS the setup
against live market data before sending anything. That matters because a
queued entry can sit for minutes while price moves — approving must never
replay a stale plan blindly.

Entries expire (ttl_seconds, default 15 min) so a queue left unattended
drains itself instead of accumulating setups whose market context is long
gone.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

DEFAULT_PATH = Path("pending_trades.json")
DEFAULT_TTL_SECONDS = 900.0  # 15 minutes


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _write(entries: list[dict], path: Path) -> None:
    """Atomic: write to a sibling temp file, then os.replace() it into place.
    A plain write_text() can be observed half-written by the other two
    processes reading this file concurrently."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(entries))
    os.replace(tmp, path)


def _is_live(entry: dict, now: float) -> bool:
    return entry.get("status") == "pending" and float(entry.get("expires_at", 0)) > now


def add(
    venue: str,
    symbol: str,
    side: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    confidence: float,
    final_pct: float,
    smart_money_direction: str = "NEUTRAL",
    smart_money_agreement: int = 0,
    size: float | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    path: Path = DEFAULT_PATH,
    now: float | None = None,
) -> dict:
    """Queue one setup for human approval. Returns the stored entry.

    Deliberately idempotent per (venue, symbol, side): the loops re-scan every
    ~30s and would otherwise pile up dozens of near-identical entries for the
    same setup while it persists. An existing live entry for the same
    venue+symbol+side is REFRESHED in place (latest prices/score, expiry
    extended) rather than duplicated.
    """
    now = time.time() if now is None else now
    entries = [e for e in _read(path) if _is_live(e, now)]

    for e in entries:
        if (e["venue"], e["symbol"], e["side"]) == (venue, symbol, side):
            e.update(
                entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
                confidence=confidence, final_pct=final_pct,
                smart_money_direction=smart_money_direction,
                smart_money_agreement=smart_money_agreement,
                size=size, expires_at=now + ttl_seconds, refreshed_at=now,
            )
            _write(entries, path)
            return e

    entry = {
        "id": uuid.uuid4().hex[:12],
        "venue": venue,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "confidence": confidence,
        "final_pct": final_pct,
        "smart_money_direction": smart_money_direction,
        "smart_money_agreement": smart_money_agreement,
        "size": size,
        "status": "pending",
        "created_at": now,
        "refreshed_at": now,
        "expires_at": now + ttl_seconds,
    }
    entries.append(entry)
    _write(entries, path)
    return entry


def list_pending(path: Path = DEFAULT_PATH, now: float | None = None) -> list[dict]:
    """Live (unexpired, still-pending) entries, newest first."""
    now = time.time() if now is None else now
    live = [e for e in _read(path) if _is_live(e, now)]
    return sorted(live, key=lambda e: e.get("created_at", 0), reverse=True)


def get(entry_id: str, path: Path = DEFAULT_PATH, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    for e in _read(path):
        if e.get("id") == entry_id and _is_live(e, now):
            return e
    return None


def resolve(entry_id: str, status: str, path: Path = DEFAULT_PATH,
            now: float | None = None) -> dict | None:
    """Mark an entry approved/cancelled and drop it from the live queue.
    Returns the entry, or None if it's already gone/expired — callers must
    treat None as "too late", never as success."""
    now = time.time() if now is None else now
    entries = _read(path)
    found = None
    for e in entries:
        if e.get("id") == entry_id and _is_live(e, now):
            e["status"] = status
            e["resolved_at"] = now
            found = e
            break
    if found is None:
        return None
    _write([e for e in entries if _is_live(e, now)], path)
    return found


def purge_expired(path: Path = DEFAULT_PATH, now: float | None = None) -> int:
    """Drop expired/resolved entries. Returns how many were removed."""
    now = time.time() if now is None else now
    entries = _read(path)
    live = [e for e in entries if _is_live(e, now)]
    if len(live) != len(entries):
        _write(live, path)
    return len(entries) - len(live)


def clear(path: Path = DEFAULT_PATH) -> None:
    _write([], path)
