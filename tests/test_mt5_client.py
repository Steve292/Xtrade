"""bot/mt5/client.py's MT5Client wraps a raw MetaTrader5-compatible object.
These pin the account-balance accessors against a minimal stub of that raw
object, since nothing else in the suite exercises them directly."""

import pytest

from bot.mt5.client import MT5Client


class _RawAccount:
    def __init__(self, balance, margin_free):
        self.balance = balance
        self.margin_free = margin_free


class _RawClient:
    def __init__(self, account=None, last_error=""):
        self._account = account
        self._last_error = last_error

    def account_info(self):
        return self._account

    def last_error(self):
        return self._last_error


def test_account_balance_reads_the_raw_balance_field():
    client = MT5Client(_RawClient(_RawAccount(balance=1234.5, margin_free=900.0)))
    assert client.account_balance() == 1234.5


def test_account_free_margin_reads_margin_free_not_balance():
    # The whole point of this accessor is that it's NOT the same number as
    # account_balance() once margin is locked up in an open position.
    client = MT5Client(_RawClient(_RawAccount(balance=1234.5, margin_free=900.0)))
    assert client.account_free_margin() == 900.0


def test_account_balance_raises_when_account_info_is_none():
    client = MT5Client(_RawClient(account=None, last_error="not connected"))
    with pytest.raises(RuntimeError, match="not connected"):
        client.account_balance()


def test_account_free_margin_raises_when_account_info_is_none():
    client = MT5Client(_RawClient(account=None, last_error="not connected"))
    with pytest.raises(RuntimeError, match="not connected"):
        client.account_free_margin()
