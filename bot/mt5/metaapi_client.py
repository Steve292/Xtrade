"""
MetaApi.cloud-backed MT5 client — exposes the exact same public interface as
`MT5Client` (bot/mt5/client.py), so `MT5Broker` works unchanged regardless of
which backend is selected. This is the alternative to the raw `mt5linux`
bridge: MetaApi hosts and manages the MT5 terminal for you, so there's no
Windows VPS to provision or keep alive.

MetaApi's own SDK is fully async; every method here bridges onto a persistent
background event loop so callers still get a plain synchronous call, matching
`mt5linux`'s client and keeping `MT5Broker` free of asyncio entirely.

Setup this backend needs — all done by the user, never by this code:
1. Create a MetaApi.cloud account and generate an API token.
2. Have (or create) a broker MT5 demo/live account: login, password, server.
3. Put METAAPI_TOKEN / MT5_LOGIN / MT5_PASSWORD / MT5_SERVER in `.env`
   yourself — this module only reads them.

Known caveat: MetaApi's historical-candle RPC is documented as G1-only. If an
account provisions as G2, `copy_rates()` raises a clear error rather than
failing deep inside the strategy — run `scripts/metaapi_smoke_test.py` first
to find out before relying on it.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pandas as pd

# MT5 trade-return codes that mean success (see MetatraderTradeResponse's
# docstring in the installed SDK's metaapi/models.py): 0, 10008-10010, 10025
# succeed; every other code is an error.
_SUCCESS_CODES = {0, 10008, 10009, 10010, 10025}


@dataclass
class SymbolInfo:
    """Same shape as bot.mt5.client.SymbolInfo — the subset of a symbol spec
    the bot needs for lot sizing/pricing."""

    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float


class MetaApiClient:
    """Wrapper over a MetaApi.cloud RPC connection for one MT5 account.

    Pass an already-connected (connection, account) pair directly (e.g. a
    stub in tests, with loop=None so calls run inline via asyncio.run), or
    build a real one over the network with `MetaApiClient.connect(...)`.
    """

    def __init__(self, connection, account, loop=None, thread=None):
        self._connection = connection
        self._account = account
        self._loop = loop
        self._thread = thread

    def _run(self, coro):
        if self._loop is None:  # test-stub path: no background thread needed
            return asyncio.run(coro)
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    @classmethod
    def connect(cls, token: str, login: str, password: str, server: str) -> "MetaApiClient":
        from metaapi_cloud_sdk import MetaApi  # lazy: only needed for a live connection

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        async def _setup():
            api = MetaApi(token)
            accounts = await api.metatrader_account_api.get_accounts_with_infinite_scroll_pagination()
            account = next(
                (a for a in accounts if str(a.login) == str(login) and a.type.startswith("cloud")), None
            )
            if account is None:
                account = await api.metatrader_account_api.create_account(
                    {
                        "name": f"smc-bot-{login}",
                        "type": "cloud",
                        "login": login,
                        "password": password,
                        "server": server,
                        "platform": "mt5",
                        "application": "MetaApi",
                        "magic": 770077,
                    }
                )
            await account.deploy()
            await account.wait_connected()
            connection = account.get_rpc_connection()
            await connection.connect()
            await connection.wait_synchronized()
            return connection, account

        connection, account = asyncio.run_coroutine_threadsafe(_setup(), loop).result()
        return cls(connection, account, loop, thread)

    # --- market data -----------------------------------------------------

    def copy_rates(self, symbol: str, timeframe: str, count: int = 200) -> pd.DataFrame:
        candles = self._run(
            self._account.get_historical_candles(symbol=symbol, timeframe=timeframe, start_time=None, limit=count)
        )
        if not candles:
            raise RuntimeError(
                f"no candles for {symbol} {timeframe} — MetaApi's historical-candle RPC is "
                f"documented as G1-only; run scripts/metaapi_smoke_test.py to check this account"
            )
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["time"])
        return df[["timestamp", "open", "high", "low", "close", "volume"]].copy()

    def symbol_info(self, symbol: str) -> SymbolInfo:
        spec = self._run(self._connection.get_symbol_specification(symbol=symbol))
        price = self._run(self._connection.get_symbol_price(symbol=symbol))
        return SymbolInfo(
            name=symbol,
            digits=int(spec["digits"]),
            point=float(spec["point"]),
            tick_size=float(spec["tickSize"]),
            # MetaApi has no static per-lot tick value on the symbol spec (unlike raw MT5's
            # symbol_info().trade_tick_value) — profitTickValue on the live price snapshot is
            # the closest equivalent (equal to lossTickValue for the vast majority of symbols).
            tick_value=float(price["profitTickValue"]),
            volume_min=float(spec["minVolume"]),
            volume_max=float(spec["maxVolume"]),
            volume_step=float(spec["volumeStep"]),
            contract_size=float(spec["contractSize"]),
        )

    def tick(self, symbol: str) -> tuple[float, float]:
        price = self._run(self._connection.get_symbol_price(symbol=symbol))
        return float(price["bid"]), float(price["ask"])

    def account_balance(self) -> float:
        info = self._run(self._connection.get_account_information())
        return float(info["balance"])

    # --- execution -------------------------------------------------------

    def market_order(self, symbol: str, side: str, volume: float, sl: float, tp: float, comment: str = ""):
        """Send a market order. `side` is 'long' or 'short'. Unlike raw MT5's
        order_send (which always returns a result object), MetaApi *raises*
        TradeException on a failed trade — caught here and folded into the
        same dict-shaped result either way, so order_succeeded() below works
        uniformly regardless of which path produced it."""
        order_fn = self._connection.create_market_buy_order if side == "long" else self._connection.create_market_sell_order
        options = {"comment": comment[:31]} if comment else None
        try:
            return self._run(order_fn(symbol, volume, stop_loss=sl, take_profit=tp, options=options))
        except Exception as e:
            return {
                "numericCode": getattr(e, "numericCode", -1),
                "stringCode": getattr(e, "stringCode", type(e).__name__),
                "error": str(e),
            }

    def get_position(self, symbol: str):
        """Return the first open position for `symbol` (a dict), or None."""
        positions = self._run(self._connection.get_positions())
        return next((p for p in positions if p["symbol"] == symbol), None)

    def close_position(self, position):
        """Close an open MT5 position by id. `position` is whatever get_position() returned."""
        try:
            return self._run(self._connection.close_position(position_id=str(position["id"])))
        except Exception as e:
            return {
                "numericCode": getattr(e, "numericCode", -1),
                "stringCode": getattr(e, "stringCode", type(e).__name__),
                "error": str(e),
            }

    def order_succeeded(self, result) -> bool:
        return isinstance(result, dict) and result.get("numericCode") in _SUCCESS_CODES
