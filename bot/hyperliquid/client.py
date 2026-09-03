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

import time
from dataclasses import dataclass

import pandas as pd

# Friendly names -> the canonical Hyperliquid coin. Gold trades as PAXG
# (tokenized gold); let callers say GOLD / XAU / XAU/USD and mean PAXG.
# Explicit table only — no generic suffix-stripping, which could mis-map real
# tickers.
COIN_ALIASES = {
    "GOLD": "PAXG",
    "XAU": "PAXG",
    "XAUUSD": "PAXG",
    "XAU/USD": "PAXG",
}


def resolve_coin(name: str) -> str:
    """Map a friendly alias to its canonical coin (case-insensitive); pass
    anything else through unchanged. Idempotent — PAXG resolves to PAXG."""
    return COIN_ALIASES.get(name.strip().upper(), name)


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
        return float(self.info.all_mids()[resolve_coin(coin)])

    def candles(self, coin: str, interval: str = "15m", lookback_hours: int = 48) -> pd.DataFrame:
        """OHLCV candles for a coin, shaped like the SMC strategy expects."""
        coin = resolve_coin(coin)
        now = int(time.time() * 1000)
        start = now - lookback_hours * 3600 * 1000
        raw = self.info.candles_snapshot(coin, interval, start, now)
        if not raw:
            raise RuntimeError(f"no candles for {coin} {interval}")
        df = pd.DataFrame(raw)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
        for src, dst in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
            df[dst] = df[src].astype(float)
        return df[["timestamp", "open", "high", "low", "close", "volume"]].copy()

    def markets(self, names: list[str] | None = None) -> list[Market]:
        universe = self._universe_map()
        mids = self.info.all_mids()
        wanted = [resolve_coin(n) for n in names] if names is not None else list(universe.keys())
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

    def fills(self, limit: int = 500) -> list[dict]:
        """Recent fills, most recent first — each carries `closedPnl` (0 for
        an opening fill, the realized P&L for a closing one)."""
        if not self.address:
            raise RuntimeError("no wallet/address — connect with a key first")
        raw = self.info.user_fills(self.address)
        raw.sort(key=lambda f: f.get("time", 0), reverse=True)
        return raw[:limit]

    def watchlist_tickers(self, coins: list[str]) -> list[dict]:
        """Live mark price + 24h % change for each coin, in ONE API call.

        meta_and_asset_ctxs() returns markPx + prevDayPx for the whole universe
        at once — far cheaper (and rate-limit-safe) than a candles call per
        coin. 24h change is (markPx - prevDayPx) / prevDayPx."""
        meta, ctxs = self.info.meta_and_asset_ctxs()
        by_name = {a["name"]: ctxs[i] for i, a in enumerate(meta["universe"]) if i < len(ctxs)}
        out = []
        for coin in coins:
            ctx = by_name.get(resolve_coin(coin))
            if ctx is None:
                out.append({"symbol": coin, "error": "not listed"})
                continue
            mark = float(ctx.get("markPx") or 0)
            prev = float(ctx.get("prevDayPx") or 0)
            row = {"symbol": coin, "mid": mark}
            if prev > 0:
                row["change_24h_pct"] = (mark - prev) / prev * 100
            if ctx.get("funding") is not None:
                row["funding_rate"] = float(ctx["funding"])
            out.append(row)
        return out

    # --- execution -------------------------------------------------------

    def _size_from_usd(self, coin: str, usd: float) -> float:
        coin = resolve_coin(coin)
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
        coin = resolve_coin(coin)
        if leverage is not None:
            self.exchange.update_leverage(int(leverage), coin, is_cross=True)
        sz = self._size_from_usd(coin, usd)
        return self.exchange.market_open(coin, is_buy, sz, slippage=0.05)

    def close(self, coin: str):
        self._require_exchange()
        return self.exchange.market_close(resolve_coin(coin))

    def _trigger_prices(self, coin: str, closing_is_buy: bool, trigger_px: float, slippage: float) -> tuple[float, float]:
        """Round a trigger price and derive its limit_px, both to the venue's
        actual precision rule: 5 significant figures AND `6 - szDecimals`
        decimal places (mirrors the SDK's private `_slippage_price`). A raw
        unrounded float (ordinary float-arithmetic noise) gets rejected
        outright by the SDK's wire encoder — caught live: a real order failed
        with `float_to_wire causes rounding` because only limit_px was
        rounded here, triggerPx was passed straight through unrounded.
        `limit_px` is the worst acceptable price once the trigger fires, same
        idea as market_open's slippage bound."""
        sz_decimals = int(self._universe_map().get(coin, {}).get("szDecimals", 0))

        def round_px(px: float) -> float:
            return round(float(f"{px:.5g}"), 6 - sz_decimals)

        trigger_px = round_px(trigger_px)
        adj = trigger_px * (1 + slippage) if closing_is_buy else trigger_px * (1 - slippage)
        return trigger_px, round_px(adj)

    def attach_bracket(
        self, coin: str, is_buy: bool, size: float, stop_loss: float, take_profit: float,
        slippage: float = 0.03,
    ):
        """Place a reduce-only stop-loss + take-profit trigger pair on an open
        position, grouped as `positionTpsl` so either one filling cancels the
        other. `is_buy` is the side of the ORIGINAL position (True=long) — the
        closing triggers fire on the opposite side, sized to exactly flatten it.
        """
        self._require_exchange()
        coin = resolve_coin(coin)
        closing_is_buy = not is_buy

        def trigger_order(raw_trigger_px: float, tpsl: str) -> dict:
            trigger_px, limit_px = self._trigger_prices(coin, closing_is_buy, raw_trigger_px, slippage)
            return {
                "coin": coin,
                "is_buy": closing_is_buy,
                "sz": size,
                "limit_px": limit_px,
                "order_type": {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": tpsl}},
                "reduce_only": True,
            }

        orders = [trigger_order(stop_loss, "sl"), trigger_order(take_profit, "tp")]
        return self.exchange.bulk_orders(orders, grouping="positionTpsl")

    def modify_trigger_order(
        self, coin: str, oid: int, is_buy: bool, size: float, trigger_px: float, tpsl: str,
        slippage: float = 0.03,
    ):
        """Move an existing resting trigger order (identified by `oid`) to a
        new trigger price in place, rather than cancel + replace. `is_buy` is
        the side of the CLOSING order itself (as already resting), same
        convention as the individual legs attach_bracket() builds — not the
        position's own side. Used to ratchet a stop-loss forward as a
        position gains, without ever touching the take-profit leg."""
        self._require_exchange()
        coin = resolve_coin(coin)
        trigger_px, limit_px = self._trigger_prices(coin, is_buy, trigger_px, slippage)
        return self.exchange.modify_order(
            oid, coin, is_buy, size, limit_px,
            order_type={"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": tpsl}},
            reduce_only=True,
        )

    def open_trigger_orders(self, coin: str | None = None) -> list[dict]:
        """Resting trigger (SL/TP) orders, optionally filtered to one coin —
        used to check whether an open position already has bracket
        protection, so a caller can tell "never attached" apart from
        "already covered" without guessing from local state alone."""
        if not self.address:
            raise RuntimeError("no wallet/address — connect with a key first")
        orders = self.info.frontend_open_orders(self.address)
        out = [o for o in orders if o.get("isTrigger")]
        if coin:
            coin = resolve_coin(coin)
            out = [o for o in out if o.get("coin") == coin]
        return out
