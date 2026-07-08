"""
Isolated tests for the Hyperliquid client + DeFi wallet — no network or funded
key required. Stubs the SDK's Info/Exchange to verify sizing, order routing,
leverage, account parsing, and the read-only guard.

Run directly (`python tests/test_hyperliquid.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.hyperliquid.client import HyperliquidClient
from bot.wallet import DefiWallet


class StubInfo:
    def __init__(self):
        self.calls = []

    def meta(self, dex=""):
        return {"universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "marginTableId": 1},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25, "marginTableId": 2},
            {"name": "XMR", "szDecimals": 2, "maxLeverage": 10, "marginTableId": 3},
        ]}

    def all_mids(self, dex=""):
        return {"BTC": "50000.0", "ETH": "2000.0", "XMR": "300.0"}

    def user_state(self, address, dex=""):
        return {
            "marginSummary": {"accountValue": "1000.5", "totalMarginUsed": "120.0"},
            "withdrawable": "880.5",
            "assetPositions": [
                {"type": "oneWay", "position": {
                    "coin": "BTC", "szi": "0.01", "entryPx": "49000",
                    "unrealizedPnl": "10.0", "leverage": {"type": "cross", "value": 5}}},
                {"type": "oneWay", "position": {
                    "coin": "ETH", "szi": "-0.5", "entryPx": "2100",
                    "unrealizedPnl": "-5.0", "leverage": {"type": "cross", "value": 3}}},
            ],
        }


class StubExchange:
    def __init__(self):
        self.calls = []

    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("update_leverage", leverage, name, is_cross))
        return {"status": "ok"}

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append(("market_open", name, is_buy, sz, slippage))
        return {"status": "ok", "name": name, "is_buy": is_buy, "sz": sz}

    def market_close(self, coin, sz=None, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append(("market_close", coin))
        return {"status": "ok", "closed": coin}


def _client(with_exchange=True):
    ex = StubExchange() if with_exchange else None
    return HyperliquidClient(StubInfo(), ex, address="0xabc", testnet=True), ex


def test_markets_filtered_and_parsed():
    c, _ = _client()
    ms = {m.name: m for m in c.markets(["BTC", "XMR"])}
    assert set(ms) == {"BTC", "XMR"}
    assert ms["BTC"].mid == 50000.0 and ms["BTC"].max_leverage == 40
    assert ms["XMR"].mid == 300.0 and ms["XMR"].sz_decimals == 2


def test_long_sizes_and_buys():
    c, ex = _client()
    c.long("BTC", usd=100)  # 100 / 50000 = 0.002
    call = ex.calls[-1]
    assert call[0] == "market_open" and call[1] == "BTC"
    assert call[2] is True  # is_buy
    assert call[3] == 0.002


def test_short_sells():
    c, ex = _client()
    c.short("XMR", usd=90)  # 90 / 300 = 0.3, szDecimals=2 -> 0.3
    call = ex.calls[-1]
    assert call[1] == "XMR" and call[2] is False and call[3] == 0.3


def test_leverage_applied_before_order():
    c, ex = _client()
    c.long("BTC", usd=100, leverage=5)
    kinds = [x[0] for x in ex.calls]
    assert kinds == ["update_leverage", "market_open"]
    assert ex.calls[0] == ("update_leverage", 5, "BTC", True)


def test_size_rounds_to_szdecimals():
    c, ex = _client()
    c.long("ETH", usd=100)  # 100 / 2000 = 0.05, szDecimals=4 -> 0.05
    assert ex.calls[-1][3] == 0.05


def test_tiny_amount_rejected():
    c, _ = _client()
    try:
        c.long("BTC", usd=0.01)  # 0.01/50000 rounds to 0 at 5 decimals
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_close_routes_to_market_close():
    c, ex = _client()
    c.close("SOL")
    assert ex.calls[-1] == ("market_close", "SOL")


def test_readonly_client_cannot_trade():
    c, _ = _client(with_exchange=False)
    for fn in (lambda: c.long("BTC", 100), lambda: c.short("BTC", 100), lambda: c.close("BTC")):
        try:
            fn()
            assert False, "expected RuntimeError on read-only client"
        except RuntimeError:
            pass


def test_account_parses_positions():
    c, _ = _client()
    acct = c.account()
    assert acct.account_value == 1000.5 and acct.withdrawable == 880.5
    byc = {p.coin: p for p in acct.positions}
    assert byc["BTC"].side == "long" and byc["BTC"].size == 0.01 and byc["BTC"].leverage == 5
    assert byc["ETH"].side == "short" and byc["ETH"].size == 0.5
    assert byc["ETH"].unrealized_pnl == -5.0


def test_wallet_create_and_roundtrip(tmp_path=None):
    w = DefiWallet.create()
    assert w.address.startswith("0x") and len(w.address) == 42
    assert w.private_key
    w2 = DefiWallet.from_key(w.private_key)
    assert w2.address == w.address  # key deterministically yields the same address


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
