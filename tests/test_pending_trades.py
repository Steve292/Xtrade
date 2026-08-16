"""
Tests for bot/pending_trades.py — the approve/cancel queue for setups that
cleared the unified gate but fell below the auto-fire threshold. No network,
no real files outside a temp path; `now` is injected everywhere so expiry is
tested deterministically rather than with sleeps.

Run directly (`python tests/test_pending_trades.py`) or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import pending_trades as pt


def _tmp_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    p = Path(name)
    p.unlink()  # must behave fine against a path that doesn't exist yet
    return p


def _add(path, symbol="BTC", side="long", final_pct=75.0, now=1000.0, venue="hl", ttl=900.0):
    return pt.add(
        venue=venue, symbol=symbol, side=side, entry_price=100.0, stop_loss=80.0,
        take_profit=140.0, confidence=0.7, final_pct=final_pct,
        path=path, now=now, ttl_seconds=ttl,
    )


def test_missing_file_is_an_empty_queue():
    p = _tmp_path()
    assert pt.list_pending(p) == []


def test_add_then_list():
    p = _tmp_path()
    try:
        _add(p, symbol="ETH", final_pct=82.5)
        live = pt.list_pending(p, now=1001.0)
        assert len(live) == 1
        assert live[0]["symbol"] == "ETH"
        assert live[0]["final_pct"] == 82.5
        assert live[0]["status"] == "pending"
    finally:
        p.unlink(missing_ok=True)


def test_same_setup_refreshes_instead_of_duplicating():
    # The loops rescan every ~30s; the same live setup must not pile up.
    p = _tmp_path()
    try:
        first = _add(p, symbol="BTC", side="long", final_pct=70.0, now=1000.0)
        second = _add(p, symbol="BTC", side="long", final_pct=77.0, now=1030.0)
        live = pt.list_pending(p, now=1031.0)
        assert len(live) == 1
        assert second["id"] == first["id"]        # same entry, refreshed
        assert live[0]["final_pct"] == 77.0       # latest score won
        assert live[0]["expires_at"] > first["created_at"] + 900.0  # expiry extended
    finally:
        p.unlink(missing_ok=True)


def test_opposite_side_is_a_separate_entry():
    p = _tmp_path()
    try:
        _add(p, symbol="BTC", side="long", now=1000.0)
        _add(p, symbol="BTC", side="short", now=1000.0)
        assert len(pt.list_pending(p, now=1001.0)) == 2
    finally:
        p.unlink(missing_ok=True)


def test_entries_expire_after_ttl():
    p = _tmp_path()
    try:
        _add(p, now=1000.0, ttl=900.0)
        assert len(pt.list_pending(p, now=1899.0)) == 1   # just inside
        assert pt.list_pending(p, now=1901.0) == []       # past expiry
    finally:
        p.unlink(missing_ok=True)


def test_approve_returns_entry_and_removes_it_from_the_queue():
    p = _tmp_path()
    try:
        e = _add(p, now=1000.0)
        got = pt.resolve(e["id"], "approved", path=p, now=1010.0)
        assert got is not None and got["status"] == "approved"
        assert pt.list_pending(p, now=1011.0) == []
    finally:
        p.unlink(missing_ok=True)


def test_cancel_removes_it_from_the_queue():
    p = _tmp_path()
    try:
        e = _add(p, now=1000.0)
        assert pt.resolve(e["id"], "cancelled", path=p, now=1010.0) is not None
        assert pt.list_pending(p, now=1011.0) == []
    finally:
        p.unlink(missing_ok=True)


def test_resolving_an_expired_entry_returns_none():
    # "Too late" must be distinguishable from success — approving a stale
    # setup must never look like it worked.
    p = _tmp_path()
    try:
        e = _add(p, now=1000.0, ttl=900.0)
        assert pt.resolve(e["id"], "approved", path=p, now=5000.0) is None
    finally:
        p.unlink(missing_ok=True)


def test_resolving_twice_returns_none_the_second_time():
    p = _tmp_path()
    try:
        e = _add(p, now=1000.0)
        assert pt.resolve(e["id"], "approved", path=p, now=1010.0) is not None
        assert pt.resolve(e["id"], "approved", path=p, now=1020.0) is None
    finally:
        p.unlink(missing_ok=True)


def test_resolving_unknown_id_returns_none():
    p = _tmp_path()
    try:
        _add(p, now=1000.0)
        assert pt.resolve("nope", "approved", path=p, now=1010.0) is None
    finally:
        p.unlink(missing_ok=True)


def test_get_returns_only_live_entries():
    p = _tmp_path()
    try:
        e = _add(p, now=1000.0, ttl=900.0)
        assert pt.get(e["id"], p, now=1010.0) is not None
        assert pt.get(e["id"], p, now=5000.0) is None
    finally:
        p.unlink(missing_ok=True)


def test_purge_expired_reports_how_many_it_dropped():
    p = _tmp_path()
    try:
        _add(p, symbol="BTC", now=1000.0, ttl=100.0)
        _add(p, symbol="ETH", now=1000.0, ttl=100000.0)
        assert pt.purge_expired(p, now=2000.0) == 1
        assert [e["symbol"] for e in pt.list_pending(p, now=2001.0)] == ["ETH"]
    finally:
        p.unlink(missing_ok=True)


def test_corrupt_file_reads_as_empty_not_a_crash():
    p = _tmp_path()
    try:
        p.write_text("{not json at all")
        assert pt.list_pending(p) == []
    finally:
        p.unlink(missing_ok=True)


def test_list_is_newest_first():
    p = _tmp_path()
    try:
        # Long TTLs so BOTH are still live at the assertion time — this test
        # is about ordering, not expiry.
        _add(p, symbol="BTC", now=1000.0, ttl=100000.0)
        _add(p, symbol="ETH", now=2000.0, ttl=100000.0)
        assert [e["symbol"] for e in pt.list_pending(p, now=2001.0)] == ["ETH", "BTC"]
    finally:
        p.unlink(missing_ok=True)


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
