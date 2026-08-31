"""
Read-only equity aggregation across both live venues (Hyperliquid + MT5), so a
single CapitalGuard can halt new entries on EITHER venue when the COMBINED
account is bleeding — a bad day on one venue pauses both, not just itself.

Money never moves between the two custody systems (a regulated MT5 broker vs.
a self-custodied on-chain wallet) — there is no shared account to build. This
only gives shared risk oversight over the combined total, feeding the exact
same CapitalGuard class already used per-venue, just with its own state file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CombinedBalance:
    hl_value: float
    mt5_value_usd: float

    @property
    def total(self) -> float:
        return self.hl_value + self.mt5_value_usd


def fetch_combined_balance(
    hl_client, mt5_client, mt5_cent_divisor: float = 1.0
) -> CombinedBalance | None:
    """Best-effort read of both venues' current balance.

    A client passed as None means that venue isn't part of this run's combined
    ledger at all (e.g. this process only trades one venue) — not an error,
    contributes 0.

    Returns None if a client that WAS passed fails to report a balance. A
    partial reading is worse than no reading here: CapitalGuard is stateful
    (day_start_balance/peak_balance), and silently treating a fetch failure as
    "that venue has $0 right now" would look like a real loss and could
    corrupt the guard's baseline — possibly resetting day_start to an
    artificially low number that then hides a real future loss. Skipping the
    update for one pass is safe; feeding it bad data is not.
    """
    hl_value = 0.0
    if hl_client is not None:
        try:
            hl_value = hl_client.account().account_value
        except Exception as e:
            # Previously a bare `except Exception: return None` with no
            # logging at all -- every failure here (1,034 of 1,062 observed
            # "balance fetch failed" lines in one live session) vanished with
            # zero diagnostic trail. The other 28 only surfaced because
            # hypertrade.py happens to log a SEPARATE retry path for its own
            # reconnect attempts; this function's own failures were silent.
            print(f"  [combined ledger] Hyperliquid balance fetch failed: "
                  f"{type(e).__name__}: {str(e)[:150]}")
            return None

    mt5_value = 0.0
    if mt5_client is not None:
        try:
            mt5_value = mt5_client.account_balance() / mt5_cent_divisor
        except Exception as e:
            print(f"  [combined ledger] MT5 balance fetch failed: "
                  f"{type(e).__name__}: {str(e)[:150]}")
            return None

    return CombinedBalance(hl_value=hl_value, mt5_value_usd=mt5_value)


def reconnect_if_needed(client, connect_fn):
    """If `client` is already live, return it unchanged. If it's None, make
    ONE attempt to (re)connect via `connect_fn()` (which must raise on
    failure) and return the result — still None if that attempt fails.
    Never raises.

    This exists so a venue connection that failed once doesn't stay None for
    the rest of a process's (often days-long) life: both hypertrade.py and
    bot/runner.py call this every pass rather than only at startup. It's
    deliberately NOT merged into fetch_combined_balance above — that
    function's "client=None means this venue isn't tracked, contributes $0"
    contract is correct for a deployment that never tracks a given venue, but
    would be wrong for "tried to track it, currently unreachable": callers
    that always intend to track both venues must check the return here and
    skip fetch_combined_balance entirely while it's still None, rather than
    pass the None through and have it silently read as a real $0 balance.
    """
    if client is not None:
        return client
    try:
        return connect_fn()
    except Exception as e:
        # Same gap as fetch_combined_balance had, and arguably the ROOT of
        # many of its downstream failures: if a dead client never reconnects
        # because connect_fn() keeps failing silently, every subsequent
        # balance fetch fails for a reason this function alone could explain
        # and never did. Every ~30s call from both live loops when the client
        # is down previously left zero trace of WHY reconnection isn't
        # happening.
        print(f"  [reconnect] attempt failed ({type(e).__name__}: {str(e)[:120]})")
        return None
