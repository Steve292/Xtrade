"""
Tests for telling this bot's positions apart from everything else on the
account (bot/mt5/client.py's magic filtering + ticket reconciliation).

The account these run against is traded by hand as well as by the bot, so
"is there a position on this symbol" and "is MY position still open" are two
different questions. No network: a fake raw MT5 module supplies positions.

Run directly (`python tests/test_manual_positions.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.mt5.client import BOT_MAGIC, MT5Client


class FakePosition:
    def __init__(self, symbol, ticket, magic, type_=0, volume=0.1):
        self.symbol = symbol
        self.ticket = ticket
        self.magic = magic
        self.type = type_
        self.volume = volume
        self.price_open = 100.0
        self.sl = 90.0
        self.tp = 120.0
        self.profit = 1.5


class FakeMT5:
    def __init__(self, positions):
        self._positions = positions

    def positions_get(self, symbol=None):
        if symbol is None:
            return list(self._positions)
        return [p for p in self._positions if p.symbol == symbol]


def _client(positions) -> MT5Client:
    c = MT5Client.__new__(MT5Client)
    c._mt5 = FakeMT5(positions)
    return c


BOT_POS = FakePosition("BTCUSDc", ticket=111, magic=BOT_MAGIC)
MANUAL_POS = FakePosition("BTCUSDc", ticket=222, magic=0)
MANUAL_GOLD = FakePosition("XAUUSDc", ticket=333, magic=0)
OTHER_EA = FakePosition("XAUUSDc", ticket=444, magic=999999)


# --- origin tagging ---------------------------------------------------------


def test_positions_are_tagged_bot_or_manual_by_magic():
    rows = _client([BOT_POS, MANUAL_POS]).all_positions()
    by_ticket = {r["ticket"]: r for r in rows}
    assert by_ticket[111]["origin"] == "bot"
    assert by_ticket[222]["origin"] == "manual"


def test_another_eas_positions_read_as_manual():
    """"manual" means anything this bot did not place — including a different
    robot. Only BOT_MAGIC is ours."""
    assert _client([OTHER_EA]).all_positions()[0]["origin"] == "manual"


def test_positions_split_groups_by_origin():
    split = _client([BOT_POS, MANUAL_POS, MANUAL_GOLD]).positions_split()
    assert [p["ticket"] for p in split["bot"]] == [111]
    assert sorted(p["ticket"] for p in split["manual"]) == [222, 333]


def test_split_of_a_flat_account_is_empty_both_sides():
    assert _client([]).positions_split() == {"bot": [], "manual": []}


# --- get_position must not mistake a manual trade for the bot's -------------


def test_get_position_ignores_a_manual_position_on_the_same_symbol():
    """The bug this guards: a hand-placed BTC position previously read as the
    bot's own, because the lookup keyed only on symbol."""
    assert _client([MANUAL_POS]).get_position("BTCUSDc") is None


def test_get_position_finds_the_bots_own():
    assert _client([MANUAL_POS, BOT_POS]).get_position("BTCUSDc").ticket == 111


def test_get_position_magic_none_restores_any_position_lookup():
    assert _client([MANUAL_POS]).get_position("BTCUSDc", magic=None).ticket == 222


# --- ticket reconciliation --------------------------------------------------


def test_position_by_ticket_is_exact():
    c = _client([BOT_POS, MANUAL_POS])
    assert c.position_by_ticket(111).ticket == 111
    assert c.position_by_ticket(999) is None


def test_closed_bot_trade_reads_as_closed_even_with_a_manual_one_open():
    """The reconciliation bug in full: the bot's ticket 111 is gone, but a
    manual position remains on the same symbol. Keyed on symbol the trade
    looks open forever and the slot never frees; keyed on ticket it is
    correctly closed."""
    c = _client([MANUAL_POS])
    assert c.position_by_ticket(111) is None, "bot's closed trade must read as closed"
    assert c.get_position("BTCUSDc", magic=None) is not None, "manual one is still there"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
