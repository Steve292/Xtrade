"""
MT5 execution adapter.

Exposes the same method surface `bot/runner.py` already uses on `Exchange` and
`EVMDex` (`fetch_ohlcv`, `get_balance`, `open_position`, `check_exit`,
`.position`, `.trade_log`) so the runner's venue dispatch stays uniform. All
MT5-specific mechanics — lots instead of units, market orders via the client,
positions tracked by ticket — are localized here.

In `paper` mode the adapter pulls **real** candles from MT5 (so the strategy is
tested on live forex/CFD data) but simulates fills against a virtual balance,
mirroring `bot.exchange.Exchange`. In `live` mode it routes real orders to the
connected demo account.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .client import MT5Client, SymbolInfo


@dataclass
class MT5Position:
    side: str  # "long" | "short"
    entry: float
    size: float  # lots
    stop_loss: float
    take_profit: float
    reason: str
    tick_size: float
    tick_value: float
    ticket: int | None = None


class MT5Broker:
    def __init__(
        self,
        client: MT5Client,
        symbol: str,
        mode: str = "paper",
        initial_balance: float = 10000.0,
    ):
        self.client = client
        self.symbol = symbol
        self.mode = mode
        self.balance = initial_balance
        self.position: MT5Position | None = None
        self.trade_log: list[dict] = []

    # --- data ------------------------------------------------------------

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        return self.client.copy_rates(symbol, timeframe, limit)

    def get_symbol_info(self) -> SymbolInfo:
        return self.client.symbol_info(self.symbol)

    def get_balance(self) -> float:
        if self.mode == "paper":
            return self.balance
        return self.client.account_balance()

    # --- execution -------------------------------------------------------

    def open_position(
        self,
        side: str,
        entry: float,
        size: float,
        sl: float,
        tp: float,
        reason: str,
        symbol: str,
    ) -> None:
        if self.position is not None:
            return

        info = self.client.symbol_info(symbol)

        if self.mode == "paper":
            self.position = MT5Position(
                side=side,
                entry=entry,
                size=size,
                stop_loss=sl,
                take_profit=tp,
                reason=reason,
                tick_size=info.tick_size,
                tick_value=info.tick_value,
            )
            self.trade_log.append(
                {"action": "open", "side": side, "entry": entry, "lots": size,
                 "sl": sl, "tp": tp, "reason": reason}
            )
            print(f"[MT5 PAPER] OPEN {side.upper()} {size:g} lots {symbol} @ {entry:.5f}")
            print(f"            SL={sl:.5f} TP={tp:.5f} | {reason}")
            return

        result = self.client.market_order(symbol, side, size, sl, tp, comment=reason)
        if not self.client.order_succeeded(result):
            print(f"[MT5 LIVE] order rejected: retcode={getattr(result, 'retcode', '?')}")
            return
        self.position = MT5Position(
            side=side,
            entry=entry,
            size=size,
            stop_loss=sl,
            take_profit=tp,
            reason=reason,
            tick_size=info.tick_size,
            tick_value=info.tick_value,
            ticket=getattr(result, "order", None),
        )
        self.trade_log.append(
            {"action": "open", "side": side, "entry": entry, "lots": size,
             "sl": sl, "tp": tp, "reason": reason, "ticket": self.position.ticket}
        )
        print(f"[MT5 LIVE] OPEN {side.upper()} {size:g} lots {symbol} "
              f"ticket={self.position.ticket} | {reason}")

    def check_exit(self, current_price: float) -> bool:
        if self.position is None:
            return False

        if self.mode == "paper":
            return self._check_exit_paper(current_price)
        return self._check_exit_live()

    def _check_exit_paper(self, current_price: float) -> bool:
        pos = self.position
        if pos.side == "long":
            hit_sl = current_price <= pos.stop_loss
            hit_tp = current_price >= pos.take_profit
        else:
            hit_sl = current_price >= pos.stop_loss
            hit_tp = current_price <= pos.take_profit

        if not (hit_sl or hit_tp):
            return False

        pnl = self._paper_pnl(current_price)
        outcome = "TP" if hit_tp else "SL"
        self.balance += pnl
        self.trade_log.append(
            {"action": "close", "side": pos.side, "exit": current_price,
             "pnl": pnl, "outcome": outcome}
        )
        print(f"[MT5 PAPER] CLOSE {pos.side.upper()} @ {current_price:.5f} | "
              f"{outcome} | PnL=${pnl:+.2f} | Balance=${self.balance:.2f}")
        self.position = None
        return True

    def _check_exit_live(self) -> bool:
        # SL/TP are attached to the order and enforced server-side. If the
        # position no longer exists, it was closed (SL/TP hit or manual).
        still_open = self.client.get_position(self.symbol)
        if still_open is not None:
            return False
        balance = self.client.account_balance()
        self.trade_log.append(
            {"action": "close", "side": self.position.side,
             "ticket": self.position.ticket, "balance": balance}
        )
        print(f"[MT5 LIVE] CLOSE {self.position.side.upper()} "
              f"ticket={self.position.ticket} | Balance=${balance:.2f}")
        self.position = None
        return True

    def _paper_pnl(self, exit_price: float) -> float:
        pos = self.position
        if pos is None or pos.tick_size == 0:
            return 0.0
        direction = 1.0 if pos.side == "long" else -1.0
        ticks = (exit_price - pos.entry) * direction / pos.tick_size
        return ticks * pos.tick_value * pos.size
