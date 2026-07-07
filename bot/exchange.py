from __future__ import annotations

from dataclasses import dataclass

import ccxt
import pandas as pd


@dataclass
class Position:
    side: str  # "long" | "short"
    entry: float
    size: float
    stop_loss: float
    take_profit: float
    reason: str


class Exchange:
    """Unified exchange interface with paper trading support."""

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        mode: str = "paper",
        initial_balance: float = 10000.0,
    ):
        self.mode = mode
        self.balance = initial_balance
        self.position: Position | None = None
        self.trade_log: list[dict] = []

        if mode == "live" and api_key and api_secret:
            exchange_class = getattr(ccxt, exchange_id)
            self.client = exchange_class(
                {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            )
        else:
            self.client = getattr(ccxt, exchange_id)({"enableRateLimit": True})

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200
    ) -> pd.DataFrame:
        raw = self.client.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def get_balance(self) -> float:
        if self.mode == "paper":
            return self.balance
        bal = self.client.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0))

    def open_position(
        self,
        side: str,
        entry: float,
        size: float,
        sl: float,
        tp: float,
        reason: str,
        symbol: str = "BTC/USDT",
    ) -> None:
        if self.position is not None:
            return

        if self.mode == "paper":
            self.position = Position(side, entry, size, sl, tp, reason)
            self.trade_log.append(
                {
                    "action": "open",
                    "side": side,
                    "entry": entry,
                    "size": size,
                    "sl": sl,
                    "tp": tp,
                    "reason": reason,
                }
            )
            print(f"[PAPER] OPEN {side.upper()} @ {entry:.2f} | SL={sl:.2f} TP={tp:.2f}")
            print(f"        Reason: {reason}")
        else:
            order_side = "buy" if side == "long" else "sell"
            self.client.create_market_order(
                symbol=symbol,
                side=order_side,
                amount=size,
            )
            self.position = Position(side, entry, size, sl, tp, reason)

    def check_exit(self, current_price: float) -> bool:
        if self.position is None:
            return False

        pos = self.position
        hit_sl = hit_tp = False

        if pos.side == "long":
            hit_sl = current_price <= pos.stop_loss
            hit_tp = current_price >= pos.take_profit
        else:
            hit_sl = current_price >= pos.stop_loss
            hit_tp = current_price <= pos.take_profit

        if hit_sl or hit_tp:
            pnl = self._calc_pnl(current_price)
            outcome = "TP" if hit_tp else "SL"
            if self.mode == "paper":
                self.balance += pnl
            self.trade_log.append(
                {
                    "action": "close",
                    "side": pos.side,
                    "exit": current_price,
                    "pnl": pnl,
                    "outcome": outcome,
                }
            )
            print(
                f"[PAPER] CLOSE {pos.side.upper()} @ {current_price:.2f} | "
                f"{outcome} | PnL=${pnl:.2f} | Balance=${self.balance:.2f}"
            )
            self.position = None
            return True

        return False

    def _calc_pnl(self, exit_price: float) -> float:
        pos = self.position
        if pos is None:
            return 0.0
        if pos.side == "long":
            return (exit_price - pos.entry) * pos.size
        return (pos.entry - exit_price) * pos.size
