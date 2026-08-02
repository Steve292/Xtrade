"""
Capital preservation: daily loss limit, max drawdown circuit breaker, and
concurrent-risk caps. This is the module that halts trading rather than let a
losing streak compound into something that takes the account out of the game.

State (day_start_balance/peak_balance/current_day) must survive process
restarts, or the guard is nearly toothless: a fresh in-memory instance starts
with day_start_balance=None, so the very first update() after any restart
resets the daily-loss baseline to whatever the CURRENT (possibly already
diminished) balance is — and peak_balance resets the same way, forgetting any
true historical peak. Caught live: a bot that restarted repeatedly (routine
redeploys) lost real money across several restarts, and the guard never
tripped once, because it only ever compared against its own most recent
restart's starting balance, never the account's actual trajectory.

Extended with the rest of the "self-evolving bot" blueprint's Section 7
circuit breakers: weekly drawdown, a consecutive-loss size-halving counter,
and VIX/correlated-crash/liquidity-shock halts. All of the new inputs are
optional and default to None/off, so a caller that never passes them (both
currently-armed live accounts, until this is deliberately wired in) sees
zero behavior change. Two of the blueprint's Section 7 actions are
deliberately NOT auto-executed here — "flush 50%/100% of positions" and
"lock 10% of profit into stablecoins" are each a real order/conversion, so
this guard only ever detects and reports them (check_profit_lock, the
halt_reason text); a human — or an already-armed, separately-approved
execution path — acts on the report, the same boundary this whole project
has drawn everywhere else.

peak_balance rebases to the current balance on every trading-day rollover
(see trading_day() below), same as day_start_balance — at explicit user
request, after a manual balance change (a withdrawal, not a bot trading
loss) left a stale historical peak that halted every subsequent trading day
on a "drawdown" the bot never actually caused. Trade-off, accepted
deliberately: max_drawdown_pct is now an intraday/per-trading-day check
rather than a true multi-day cumulative one — it still survives restarts
WITHIN a trading day (the anti-toothless-guard fix below still applies
there), but a slow bleed spread thinly across many separate trading days no
longer trips it on its own; max_daily_loss_pct still bounds each individual
day independently either way.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_STATE_PATH = Path("capital_guard_state.json")

_NY_TZ = ZoneInfo("America/New_York")
_SESSION_ROLLOVER_HOUR = 17  # 5pm ET — standard forex/CFD daily market close & rollover


def trading_day(now: datetime | None = None) -> date:
    """The current trading day/session, per the industry-standard forex/CFD
    daily close: 5pm America/New_York — not local midnight. A session
    labeled date D runs from (D-1) 17:00 ET to D 17:00 ET, so passing this
    (instead of date.today()) as update()'s `today` makes the daily-loss
    halt (and the weekly one, which derives its ISO week from the same
    value) auto-clear at actual market close rather than an arbitrary
    calendar-day cutoff that could fall mid-session.

    `now`, when passed explicitly (tests), is treated as already being NY
    local time if it's naive; an aware datetime is converted."""
    if now is None:
        now = datetime.now(_NY_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_NY_TZ)
    else:
        now = now.astimezone(_NY_TZ)
    d = now.date()
    return d + timedelta(days=1) if now.hour >= _SESSION_ROLLOVER_HOUR else d


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

    # Section 7 extensions — all optional/off by default.
    max_weekly_loss_pct: float | None = None
    consecutive_loss_limit: int | None = None
    vix_halt_threshold: float | None = None
    correlated_crash_btc_pct: float | None = None  # e.g. -5.0
    correlated_crash_spx_pct: float | None = None  # e.g. -3.0
    liquidity_shock_spread_pct: float | None = None  # e.g. 0.1
    liquidity_shock_volume_drop_pct: float | None = None  # e.g. -50.0
    profit_lock_trigger_pct: float | None = None  # e.g. 20.0
    profit_lock_fraction: float = 0.10

    day_start_balance: float | None = None
    peak_balance: float | None = None
    current_day: date | None = None
    halted: bool = False
    halt_reason: str | None = None

    week_start_balance: float | None = None
    current_week: str | None = None
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    baseline_balance: float | None = None  # set once, ever — profit-lock's reference point
    profit_locks_triggered: int = 0
    locked_amount: float = 0.0

    _size_halved: bool = False
    _timed_halt_until: float | None = None  # epoch seconds; weekly/correlated-crash cooldowns
    _timed_halt_reason: str | None = None

    _state_path: Path | None = None  # set by load(); None means no persistence (e.g. in tests)

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH, **config) -> "CapitalGuard":
        """Build a guard from saved state if present, so restarts don't blind
        it to the account's real trajectory. `config` are the constructor's
        configured thresholds (max_daily_loss_pct etc.) — always taken fresh
        from config.yaml, never from the saved file, so a config change takes
        effect immediately."""
        guard = cls(**config)
        guard._state_path = path
        if path.exists():
            data = json.loads(path.read_text())
            guard.day_start_balance = data.get("day_start_balance")
            guard.peak_balance = data.get("peak_balance")
            cd = data.get("current_day")
            guard.current_day = date.fromisoformat(cd) if cd else None
            guard.halted = data.get("halted", False)
            guard.halt_reason = data.get("halt_reason")
            guard.week_start_balance = data.get("week_start_balance")
            guard.current_week = data.get("current_week")
            guard.consecutive_losses = data.get("consecutive_losses", 0)
            guard.consecutive_wins = data.get("consecutive_wins", 0)
            guard.baseline_balance = data.get("baseline_balance")
            guard.profit_locks_triggered = data.get("profit_locks_triggered", 0)
            guard.locked_amount = data.get("locked_amount", 0.0)
            guard._size_halved = data.get("size_halved", False)
            guard._timed_halt_until = data.get("timed_halt_until")
            guard._timed_halt_reason = data.get("timed_halt_reason")
        return guard

    def save(self) -> None:
        if self._state_path is None:
            return
        data = {
            "day_start_balance": self.day_start_balance,
            "peak_balance": self.peak_balance,
            "current_day": self.current_day.isoformat() if self.current_day else None,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "week_start_balance": self.week_start_balance,
            "current_week": self.current_week,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "baseline_balance": self.baseline_balance,
            "profit_locks_triggered": self.profit_locks_triggered,
            "locked_amount": self.locked_amount,
            "size_halved": self._size_halved,
            "timed_halt_until": self._timed_halt_until,
            "timed_halt_reason": self._timed_halt_reason,
        }
        self._state_path.write_text(json.dumps(data))

    def update(
        self,
        current_balance: float,
        today: date,
        now_ts: float | None = None,
        vix: float | None = None,
        btc_24h_change_pct: float | None = None,
        spx_24h_change_pct: float | None = None,
        spread_pct: float | None = None,
        volume_change_24h_pct: float | None = None,
    ) -> None:
        """`now_ts` (epoch seconds) and the market-wide inputs are all
        optional — pass them once a real feed exists (bot/marketdata.py) to
        turn on the Section 7 VIX/correlated-crash/liquidity-shock checks;
        omitted, this behaves exactly as before."""
        now_ts = time.time() if now_ts is None else now_ts

        if self.baseline_balance is None:
            self.baseline_balance = current_balance  # set once, ever — profit-lock's reference point

        if self.current_day != today:
            self.current_day = today
            self.day_start_balance = current_balance
            # Peak rebases here too — see the module docstring's note on why
            # (a manual balance change, not a bot loss, must not leave a
            # stale peak halting every future trading day on a drawdown the
            # bot never caused).
            self.peak_balance = current_balance
        iso_year, iso_week, _ = today.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        if self.current_week != week_key:
            self.current_week = week_key
            self.week_start_balance = current_balance
        if self.peak_balance is None or current_balance > self.peak_balance:
            self.peak_balance = current_balance

        self.halted = False
        self.halt_reason = None

        # A timed cooldown (weekly-DD / correlated-crash) overrides everything
        # below until it actually expires, independent of any recovery in the
        # meantime — the blueprint specifies a fixed wait, not "until healthy".
        if self._timed_halt_until is not None and now_ts < self._timed_halt_until:
            self.halted = True
            self.halt_reason = self._timed_halt_reason
            self.save()
            return
        self._timed_halt_until = None
        self._timed_halt_reason = None

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

        if not self.halted and self.max_weekly_loss_pct is not None and self.week_start_balance:
            weekly_change_pct = (
                (current_balance - self.week_start_balance) / self.week_start_balance * 100
            )
            if weekly_change_pct <= -abs(self.max_weekly_loss_pct):
                self.halted = True
                self.halt_reason = f"Weekly loss limit reached ({weekly_change_pct:.2f}%) — 48h cooldown"
                self._timed_halt_until = now_ts + 48 * 3600
                self._timed_halt_reason = self.halt_reason

        if not self.halted and self.vix_halt_threshold is not None and vix is not None:
            if vix >= self.vix_halt_threshold:
                self.halted = True
                self.halt_reason = f"VIX spike ({vix:.1f} >= {self.vix_halt_threshold:.1f})"

        if (
            not self.halted
            and self.correlated_crash_btc_pct is not None
            and self.correlated_crash_spx_pct is not None
            and btc_24h_change_pct is not None
            and spx_24h_change_pct is not None
            and btc_24h_change_pct <= self.correlated_crash_btc_pct
            and spx_24h_change_pct <= self.correlated_crash_spx_pct
        ):
            self.halted = True
            self.halt_reason = (
                f"Correlated crash (BTC {btc_24h_change_pct:.2f}%, SPX {spx_24h_change_pct:.2f}%) "
                "— 24h cooldown"
            )
            self._timed_halt_until = now_ts + 24 * 3600
            self._timed_halt_reason = self.halt_reason

        if (
            not self.halted
            and self.liquidity_shock_spread_pct is not None
            and spread_pct is not None
            and spread_pct > self.liquidity_shock_spread_pct
        ):
            self.halted = True
            self.halt_reason = f"Liquidity shock — spread {spread_pct:.3f}% > {self.liquidity_shock_spread_pct:.3f}%"

        if (
            not self.halted
            and self.liquidity_shock_volume_drop_pct is not None
            and volume_change_24h_pct is not None
            and volume_change_24h_pct <= self.liquidity_shock_volume_drop_pct
        ):
            self.halted = True
            self.halt_reason = f"Liquidity shock — 24h volume {volume_change_24h_pct:.1f}%"

        self.save()  # persist immediately — a restart must never lose this state again

    def record_trade_result(self, won: bool) -> None:
        """Feed in one closed trade's outcome to drive the consecutive-loss
        size-halving rule. A single win resets the loss streak; exiting the
        halved state specifically needs 2 CONSECUTIVE wins — a loss in
        between resets that count rather than merely pausing it."""
        if won:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            if self._size_halved and self.consecutive_wins >= 2:
                self._size_halved = False
                self.consecutive_wins = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            if (
                self.consecutive_loss_limit is not None
                and self.consecutive_losses >= self.consecutive_loss_limit
            ):
                self._size_halved = True
        self.save()

    @property
    def size_multiplier(self) -> float:
        """0.5 while halved by the consecutive-loss rule, 1.0 otherwise —
        meant to be applied alongside bot/position_sizing.py's factors, not
        folded into can_open_new_trade()'s go/no-go boolean."""
        return 0.5 if self._size_halved else 1.0

    def check_profit_lock(self, current_balance: float) -> float | None:
        """Returns a dollar amount that should be moved to stablecoin this
        pass, the first time cumulative return crosses each new
        `profit_lock_trigger_pct` milestone since `baseline_balance` — or
        None if no new milestone has been crossed. Report-only: moving funds
        into a stablecoin is a real conversion, so — same as every other
        cross-asset transfer in this project — this only detects and
        reports it; a human executes the actual move."""
        if self.profit_lock_trigger_pct is None or not self.baseline_balance:
            return None
        total_return_pct = (current_balance - self.baseline_balance) / self.baseline_balance * 100
        milestones = int(total_return_pct // self.profit_lock_trigger_pct)
        if milestones <= self.profit_locks_triggered:
            return None
        new_milestones = milestones - self.profit_locks_triggered
        self.profit_locks_triggered = milestones
        amount = current_balance * self.profit_lock_fraction * new_milestones
        self.locked_amount += amount
        self.save()
        return amount

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
