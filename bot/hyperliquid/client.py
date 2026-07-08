"""
Hyperliquid perps client — long/short across majors, gold (PAXG), XMR, and
memecoins on a single venue. Defaults to TESTNET so nothing here touches real
money until you deliberately flip it.

Wraps the official `hyperliquid-python-sdk` (`Info` for read-only market data
and account state, `Exchange` for signed orders). The SDK objects are injectable
so the whole client is unit-testable without a network or a funded key.

Sizing note: Hyperliquid orders are sized in *coin units*, but people think in
dollars, so `long`/`short` take a USD notional and convert to size using the
live mid price and the asset's `szDecimals`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Market:
    name: str
    mid: float
    max_leverage: int
    sz_decimals: int


@dataclass
class Position:
    coin: str
    side: str  # "long" | "short"
    size: float  # coin units (absolute)
    entry: float
    unrealized_pnl: float
    leverage: float


@dataclass
class Account:
    address: str
    account_value: float
    withdrawable: float
    positions: list[Position]


class HyperliquidClient:
    def __init__(self, info, exchange=None, address: str | None = None, testnet: bool = True):
        self.info = info
        self.exchange = exchange  # None => read-only (no wallet)
        self.address = address
        self.testnet = testnet
        self._universe: dict[str, dict] | None = None

    @classmethod
    def connect(
        cls,
        private_key: str = "",
        address: str = "",
        testnet: bool = True,
    ) -> "HyperliquidClient":
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        base = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        info = Info(base, skip_ws=True)
        exchange = None
        addr = address or None
        if private_key:
            from eth_account import Account as EthAccount
            from hyperliquid.exchange import Exchange

            wallet = EthAccount.from_key(private_key)
            addr = wallet.address
            exchange = Exchange(wallet, base)
        return cls(info, exchange, addr, testnet=testnet)

    # --- market data -----------------------------------------------------

    def _universe_map(self) -> dict[str, dict]:
        if self._universe is None:
            self._universe = {a["name"]: a for a in self.info.meta()["universe"]}
        return self._universe

    def mid(self, coin: str) -> float:
        return float(self.info.all_mids()[coin])

    def markets(self, names: list[str] | None = None) -> list[Market]:
        universe = self._universe_map()
        mids = self.info.all_mids()
        wanted = names if names is not None else list(universe.keys())
        out: list[Market] = []
        for name in wanted:
            spec = universe.get(name)
            if spec is None or name not in mids:
                continue
            out.append(
                Market(
                    name=name,
                    mid=float(mids[name]),
                    max_leverage=int(spec["maxLeverage"]),
                    sz_decimals=int(spec["szDecimals"]),
                )
            )
        return out

    # --- account ---------------------------------------------------------

    def account(self) -> Account:
        if not self.address:
            raise RuntimeError("no wallet/address — connect with a key first")
        state = self.info.user_state(self.address)
        margin = state.get("marginSummary", {})
        positions: list[Position] = []
        for item in state.get("assetPositions", []):
            p = item.get("position", {})
            szi = float(p.get("szi", 0) or 0)
            if szi == 0:
                continue
            positions.append(
                Position(
                    coin=p["coin"],
                    side="long" if szi > 0 else "short",
                    size=abs(szi),
                    entry=float(p.get("entryPx") or 0),
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                    leverage=float((p.get("leverage") or {}).get("value", 0)),
                )
            )
        return Account(
            address=self.address,
            account_value=float(margin.get("accountValue", 0) or 0),
            withdrawable=float(state.get("withdrawable", 0) or 0),
            positions=positions,
        )

    # --- execution -------------------------------------------------------

    def _size_from_usd(self, coin: str, usd: float) -> float:
        spec = self._universe_map().get(coin)
        if spec is None:
            raise ValueError(f"{coin} is not a listed perp on this venue")
        sz = round(usd / self.mid(coin), int(spec["szDecimals"]))
        if sz <= 0:
            raise ValueError(
                f"${usd} of {coin} rounds to size 0 at {spec['szDecimals']} decimals — increase the amount"
            )
        return sz

    def _require_exchange(self):
        if self.exchange is None:
            raise RuntimeError("read-only client — connect with a private key to trade")

    def long(self, coin: str, usd: float, leverage: int | None = None):
        return self._open(coin, True, usd, leverage)

    def short(self, coin: str, usd: float, leverage: int | None = None):
        return self._open(coin, False, usd, leverage)

    def _open(self, coin: str, is_buy: bool, usd: float, leverage: int | None):
        self._require_exchange()
        if leverage is not None:
            self.exchange.update_leverage(int(leverage), coin, is_cross=True)
        sz = self._size_from_usd(coin, usd)
        return self.exchange.market_open(coin, is_buy, sz, slippage=0.05)

    def close(self, coin: str):
        self._require_exchange()
        return self.exchange.market_close(coin)
