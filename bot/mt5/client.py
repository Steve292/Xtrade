"""
Thin wrapper over an MT5 connection (the `mt5linux` rpyc client by default).

Only this module touches the raw MetaTrader5 API surface and its constants —
everything else in the bot works against the small, typed methods below. That
keeps the rest of the codebase free of MT5-specific details and makes the whole
MT5 venue unit-testable by injecting a stub in place of the raw client.

The raw client is imported lazily inside `connect()` so this module imports
fine on a Mac that does not have `mt5linux` installed yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Bot timeframe strings -> MT5 timeframe constant names (resolved on the raw
# client at call time, so no dependency on the constants at import time).
_TIMEFRAME_ATTR = {
    "1m": "TIMEFRAME_M1",
    "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30",
    "1h": "TIMEFRAME_H1",
    "4h": "TIMEFRAME_H4",
    "1d": "TIMEFRAME_D1",
}


@dataclass
class SymbolInfo:
    """The subset of MT5 symbol specs the bot needs for lot sizing/pricing."""

    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float


class MT5Client:
    """Wrapper over a raw MetaTrader5-compatible client.

    Pass a raw client directly (e.g. a stub in tests), or build one over the
    network with `MT5Client.connect(...)`.
    """

    def __init__(self, raw=None):
        self._mt5 = raw

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        login: str = "",
        password: str = "",
        server: str = "",
    ) -> "MT5Client":
        from mt5linux import MetaTrader5  # lazy: only needed for a live bridge

        raw = MetaTrader5(host=host, port=int(port))
        if not raw.initialize():
            raise ConnectionError(
                f"MT5 initialize() failed at {host}:{port} — is the remote "
                f"terminal + mt5linux server running? last_error={raw.last_error()}"
            )
        if login:
            if not raw.login(int(login), password=password, server=server):
                raise ConnectionError(
                    f"MT5 login failed for {login}@{server}: {raw.last_error()}"
                )
        return cls(raw)

    # --- market data -----------------------------------------------------

    def copy_rates(self, symbol: str, timeframe: str, count: int = 200) -> pd.DataFrame:
        attr = _TIMEFRAME_ATTR.get(timeframe)
        if attr is None:
            raise ValueError(f"Unsupported MT5 timeframe: {timeframe!r}")
        tf_const = getattr(self._mt5, attr)
        rates = self._mt5.copy_rates_from_pos(symbol, tf_const, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(
                f"copy_rates returned no data for {symbol} {timeframe}: "
                f"{self._mt5.last_error()}"
            )
        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s")
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["timestamp", "open", "high", "low", "close", "volume"]].copy()

    def symbol_info(self, symbol: str) -> SymbolInfo:
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(
                f"symbol_info({symbol!r}) is None — is the symbol name/suffix "
                f"correct for this broker? {self._mt5.last_error()}"
            )
        return SymbolInfo(
            name=info.name,
            digits=int(info.digits),
            point=float(info.point),
            tick_size=float(info.trade_tick_size),
            tick_value=float(info.trade_tick_value),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            contract_size=float(info.trade_contract_size),
        )

    def tick(self, symbol: str) -> tuple[float, float]:
        """Return (bid, ask) for the symbol."""
        t = self._mt5.symbol_info_tick(symbol)
        if t is None:
            raise RuntimeError(f"no tick for {symbol}: {self._mt5.last_error()}")
        return float(t.bid), float(t.ask)

    def account_balance(self) -> float:
        acct = self._mt5.account_info()
        if acct is None:
            raise RuntimeError(f"account_info() is None: {self._mt5.last_error()}")
        return float(acct.balance)

    # --- execution -------------------------------------------------------

    def market_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        sl: float,
        tp: float,
        comment: str = "",
    ):
        """Send a market order. `side` is 'long' or 'short'. Returns the raw result."""
        bid, ask = self.tick(symbol)
        if side == "long":
            order_type = self._mt5.ORDER_TYPE_BUY
            price = ask
        else:
            order_type = self._mt5.ORDER_TYPE_SELL
            price = bid
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": 770077,
            "comment": comment[:31],
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        return self._mt5.order_send(request)

    def get_position(self, symbol: str):
        """Return the first open position for `symbol`, or None."""
        positions = self._mt5.positions_get(symbol=symbol)
        if not positions:
            return None
        return positions[0]

    def close_position(self, position):
        """Close an open MT5 position by sending the opposite market deal."""
        symbol = position.symbol
        bid, ask = self.tick(symbol)
        is_long = position.type == self._mt5.POSITION_TYPE_BUY
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(position.volume),
            "type": self._mt5.ORDER_TYPE_SELL if is_long else self._mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": bid if is_long else ask,
            "deviation": 20,
            "magic": 770077,
            "comment": "smc close",
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        return self._mt5.order_send(request)

    def order_succeeded(self, result) -> bool:
        return result is not None and result.retcode == self._mt5.TRADE_RETCODE_DONE
