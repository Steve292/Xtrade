"""
Capital preservation: daily loss limit, max drawdown circuit breaker, and
concurrent-risk caps. This is the module that halts trading rather than let a
losing streak compound into something that takes the account out of the game.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class OpenRisk:
    ticket: int
    risk_pct: float


@dataclass
class CapitalGuard:
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    max_concurrent_trades: int = 3
    max_concurrent_open_risk_pct: float = 3.0

    day_start_balance: float | None = None
    peak_balance: float | None = None
    current_day: date | None = None
    halted: bool = False
    halt_reason: str | None = None

    def update(self, current_balance: float, today: date) -> None:
        if self.current_day != today:
            self.current_day = today
            self.day_start_balance = current_balance
        if self.peak_balance is None or current_balance > self.peak_balance:
            self.peak_balance = current_balance

        self.halted = False
        self.halt_reason = None

        if self.day_start_balance:
            daily_change_pct = (
                (current_balance - self.day_start_balance) / self.day_start_balance * 100
            )
            if daily_change_pct <= -abs(self.max_daily_loss_pct):
                self.halted = True
                self.halt_reason = f"Daily loss limit reached ({daily_change_pct:.2f}%)"

        if self.peak_balance and not self.halted:
            drawdown_pct = (self.peak_balance - current_balance) / self.peak_balance * 100
            if drawdown_pct >= abs(self.max_drawdown_pct):
                self.halted = True
                self.halt_reason = f"Max drawdown limit reached ({drawdown_pct:.2f}% from peak)"

    def can_open_new_trade(
        self,
        open_positions: list[OpenRisk],
        new_trade_risk_pct: float,
    ) -> tuple[bool, str | None]:
        if self.halted:
            return False, self.halt_reason
        if len(open_positions) >= self.max_concurrent_trades:
            return False, "Max concurrent trades reached"
        current_open_risk = sum(p.risk_pct for p in open_positions)
        if current_open_risk + new_trade_risk_pct > self.max_concurrent_open_risk_pct:
            return False, "Max concurrent open risk would be exceeded"
        return True, None
