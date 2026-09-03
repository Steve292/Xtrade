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
    # Detector modules that produced this setup, carried from Signal.detectors
    # so the exit can attribute its outcome back to them (bot/trade_grades.py).
    detectors: tuple = ()


class MT5Broker:
    def __init__(
        self,
        client: MT5Client,
        symbol: str,
        mode: str = "paper",
        initial_balance: float = 10000.0,
        cent_divisor: float = 1.0,
        grades_path=None,
    ):
        self.client = client
        self.symbol = symbol
        self.mode = mode
        self.balance = initial_balance
        self.cent_divisor = cent_divisor
        self.position: MT5Position | None = None
        self.trade_log: list[dict] = []
        # Where realised outcomes are recorded for grading (bot/trade_grades.py).
        # None = record nothing. Deliberately opt-in and injected rather than
        # defaulting to a module-level path: with an implicit default, every
        # test that exercises this broker in paper mode writes fixture trades
        # into the live grading store, and the dataset the grades are computed
        # from silently fills with EURUSD trades that never happened.
        self.grades_path = grades_path

    # --- data ------------------------------------------------------------

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        return self.client.copy_rates(symbol, timeframe, limit)

    def get_symbol_info(self) -> SymbolInfo:
        return self.client.symbol_info(self.symbol)

    def get_balance(self) -> float:
        """Raw account balance in the broker's own units — used for sizing.

        For a cent account this is cents, not dollars, but that's fine here:
        the broker also reports tick_value in the same cent units, so the
        risk-percentage math in calc_lot_size stays self-consistent.
        """
        if self.mode == "paper":
            return self.balance
        return self.client.account_balance()

    def get_display_balance(self) -> float:
        """Real-dollar balance for human-facing output (divides out cent accounts)."""
        return self.get_balance() / self.cent_divisor

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
        detectors: tuple = (),
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
                detectors=tuple(detectors or ()),
            )
            self.trade_log.append(
                {"action": "open", "side": side, "entry": entry, "lots": size,
                 "sl": sl, "tp": tp, "reason": reason}
            )
            print(f"[MT5 PAPER] OPEN {side.upper()} {size:g} lots {symbol} @ {entry:.5f}")
            print(f"            SL={sl:.5f} TP={tp:.5f} | {reason}")
            self._grade_entry(symbol, side, entry, sl, tp)
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
            detectors=tuple(detectors or ()),
        )
        self.trade_log.append(
            {"action": "open", "side": side, "entry": entry, "lots": size,
             "sl": sl, "tp": tp, "reason": reason, "ticket": self.position.ticket}
        )
        print(f"[MT5 LIVE] OPEN {side.upper()} {size:g} lots {symbol} "
              f"ticket={self.position.ticket} | {reason}")
        self._grade_entry(symbol, side, entry, sl, tp)

    def _grade_id(self) -> str:
        """Stable id for the open position: its ticket in live mode, or a
        symbol+entry key in paper mode where no ticket exists."""
        pos = self.position
        if pos is None:
            return ""
        return str(pos.ticket) if pos.ticket else f"{self.symbol}:{pos.entry}"

    def _grade_entry(self, symbol, side, entry, sl, tp) -> None:
        """Open a grading record. Wrapped because a failure to RECORD a trade
        must never prevent or unwind the trade itself -- this is observation."""
        if self.grades_path is None:
            return
        try:
            from bot.trade_grades import GradeBook
            book = GradeBook.load(self.grades_path)
            book.record_entry(
                self._grade_id(), symbol, side,
                getattr(self.position, "detectors", ()),
                entry, sl, tp,
            )
        except Exception as e:
            print(f"[grades] entry not recorded ({type(e).__name__}) — trade unaffected")

    def _grade_exit(self, pnl: float) -> None:
        if self.grades_path is None:
            return
        try:
            from bot.trade_grades import GradeBook
            book = GradeBook.load(self.grades_path)
            book.record_exit(self._grade_id(), pnl)
        except Exception as e:
            print(f"[grades] exit not recorded ({type(e).__name__}) — trade unaffected")

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
        self._grade_exit(pnl)
        self.position = None
        return True

    def _check_exit_live(self) -> bool:
        # SL/TP are attached to the order and enforced server-side. If the
        # position no longer exists, it was closed (SL/TP hit or manual).
        #
        # Matched by TICKET, not by symbol. Asking "is there a position on
        # this symbol" answers a different question on an account that also
        # carries manual trades: a hand-placed position on the same symbol
        # reads as this bot's own, so the bot never registers its real exit
        # and stays stuck holding a position that closed long ago -- blocking
        # every subsequent entry on that symbol. The ticket is the only thing
        # that identifies THIS trade.
        ticket = getattr(self.position, "ticket", None)
        if ticket is not None:
            still_open = self.client.position_by_ticket(ticket)
        else:
            # No ticket recorded (paper-mode carry-over or a fill whose result
            # could not be parsed) -- fall back to the symbol lookup, which is
            # now magic-filtered to this bot's own positions.
            still_open = self.client.get_position(self.symbol)
        if still_open is not None:
            return False
        balance = self.client.account_balance()
        # Realised P&L for THIS position, matched on position_id. None means
        # the closing deal could not be found -- the trade is then left
        # ungraded rather than recorded as break-even, which would look like
        # a real flat outcome and skew every grade drawn from it.
        pnl = None
        if ticket is not None:
            pnl = self.client.realized_pnl_for_position(ticket)
        if pnl is None:
            print(f"[grades] no closing deal found for ticket={ticket} — "
                  f"trade left ungraded rather than recorded as flat")
        else:
            self._grade_exit(pnl)
        self.trade_log.append(
            {"action": "close", "side": self.position.side,
             "ticket": self.position.ticket, "balance": balance, "pnl": pnl}
        )
        print(f"[MT5 LIVE] CLOSE {self.position.side.upper()} "
              f"ticket={self.position.ticket} | "
              f"Balance=${balance / self.cent_divisor:.2f}"
              + (f" ({balance:.2f} account units)" if self.cent_divisor != 1 else ""))
        self.position = None
        return True

    def _paper_pnl(self, exit_price: float) -> float:
        pos = self.position
        if pos is None or pos.tick_size == 0:
            return 0.0
        direction = 1.0 if pos.side == "long" else -1.0
        ticks = (exit_price - pos.entry) * direction / pos.tick_size
        return ticks * pos.tick_value * pos.size
