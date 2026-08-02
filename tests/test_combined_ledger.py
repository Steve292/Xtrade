"""
Tests for bot/combined_ledger.py — the cross-venue balance aggregator feeding
the shared MT5+Hyperliquid circuit breaker. No network.

Run directly (`python tests/test_combined_ledger.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.combined_ledger import fetch_combined_balance, reconnect_if_needed


class _StubAccount:
    def __init__(self, account_value):
        self.account_value = account_value


class _StubHLClient:
    def __init__(self, value=None, raises=False):
        self.value = value
        self.raises = raises

    def account(self):
        if self.raises:
            raise RuntimeError("hl unreachable")
        return _StubAccount(self.value)


class _StubMT5Client:
    def __init__(self, value=None, raises=False):
        self.value = value
        self.raises = raises

    def account_balance(self):
        if self.raises:
            raise RuntimeError("mt5 unreachable")
        return self.value


def test_both_venues_sum_to_total():
    hl = _StubHLClient(value=100.0)
    mt5 = _StubMT5Client(value=965.0)  # raw cent units
    combined = fetch_combined_balance(hl, mt5, mt5_cent_divisor=100.0)
    assert combined is not None
    assert combined.hl_value == 100.0
    assert combined.mt5_value_usd == 9.65
    assert combined.total == 109.65


def test_missing_client_contributes_zero_not_an_error():
    hl = _StubHLClient(value=250.0)
    combined = fetch_combined_balance(hl, None, mt5_cent_divisor=100.0)
    assert combined is not None
    assert combined.mt5_value_usd == 0.0
    assert combined.total == 250.0


def test_neither_venue_configured_gives_zero_total():
    combined = fetch_combined_balance(None, None)
    assert combined is not None
    assert combined.total == 0.0


def test_hl_fetch_failure_returns_none_not_partial_total():
    # A partial reading (e.g. "HL down, treat as $0") would look like a real
    # loss to the stateful CapitalGuard and could corrupt day_start_balance —
    # skipping the update entirely is the safe behavior, not zero-filling.
    hl = _StubHLClient(raises=True)
    mt5 = _StubMT5Client(value=100.0)
    combined = fetch_combined_balance(hl, mt5)
    assert combined is None


def test_mt5_fetch_failure_returns_none_not_partial_total():
    hl = _StubHLClient(value=100.0)
    mt5 = _StubMT5Client(raises=True)
    combined = fetch_combined_balance(hl, mt5)
    assert combined is None


# ---- reconnect_if_needed() --------------------------------------------

def test_reconnect_if_needed_passes_through_an_already_live_client():
    live = _StubMT5Client(value=1.0)
    calls = []
    result = reconnect_if_needed(live, lambda: calls.append(1) or _StubMT5Client(value=2.0))
    assert result is live
    assert calls == []  # connect_fn must not even be called when already connected


def test_reconnect_if_needed_connects_when_client_is_none():
    fresh = _StubMT5Client(value=5.0)
    result = reconnect_if_needed(None, lambda: fresh)
    assert result is fresh


def test_reconnect_if_needed_stays_none_when_connect_fn_raises():
    def _fail():
        raise ConnectionRefusedError("bridge down")
    result = reconnect_if_needed(None, _fail)
    assert result is None


def test_reconnect_after_outage_recovers_the_client():
    # The exact scenario this was built for: a client that's None (outage)
    # eventually reconnects once the dependency comes back, without ever
    # needing the caller to restart.
    client = None
    client = reconnect_if_needed(client, lambda: (_ for _ in ()).throw(ConnectionRefusedError()))
    assert client is None  # still down

    recovered = _StubMT5Client(value=42.0)
    client = reconnect_if_needed(client, lambda: recovered)
    assert client is recovered  # recovered on the next attempt


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
