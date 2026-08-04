"""
Hyperliquid auto-trader.

Pulls candles for a coin, runs the SMC strategy top-down (LTF + HTF), then puts
the signal through the full TradeScreener (SMC + Fibonacci + top-down + risk +
sniper). ONLY an approved signal is sized and — in live mode — sent to the venue
(testnet or mainnet, per config) as a real long/short.

Safety: dry-run by default. It screens and prints the full breakdown but sends
no order unless `dry_run=False`, which also requires a funded (key-bearing)
client.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot import live_state, pending_trades
from bot.capital_guard import CapitalGuard, OpenRisk, trading_day
from bot.marketdata import candles_with_binance_fallback
from bot.screening import ScreenResult, TradeScreener
from bot.smc.strategy import Signal, SignalType, SMCStrategy
from bot.unified_screen import evaluate_unified


@dataclass
class TradePlan:
    coin: str
    side: str  # "long" | "short"
    usd: float
    leverage: int
    entry: float
    stop_loss: float
    take_profit: float
    risk_pct: float


def _parse_fill(resp) -> dict | None:
    """Pull {size, price} out of a market-order response if it actually
    filled; None for a rejected/unfilled order (e.g. insufficient margin)."""
    try:
        status = resp["response"]["data"]["statuses"][0]
        filled = status["filled"]
        return {"size": float(filled["totalSz"]), "price": float(filled["avgPx"])}
    except (KeyError, IndexError, TypeError):
        return None


def _has_error(resp) -> bool:
    """True if a bulk-order response has any per-order error, OR if the
    shape is unrecognized — deliberately conservative, since this gates
    whether we warn that a position is unprotected."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return True
    try:
        statuses = resp["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return True
    return any(isinstance(s, dict) and "error" in s for s in statuses)


def _infer_bracket_from_resting(pos, resting: list[dict]) -> dict | None:
    """Reconstruct tracking info for a position that already has a resting
    bracket we didn't place (or didn't record) ourselves — identifies which
    leg is the SL by which side of entry its triggerPx sits on, since the
    venue's open-orders listing doesn't echo back an explicit sl/tp label."""
    legs = []
    for o in resting:
        try:
            legs.append((float(o["triggerPx"]), o["oid"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(legs) < 2:
        return None
    if pos.side == "long":
        sl_px, sl_oid = min(legs)  # stop sits below entry
        tp_px, tp_oid = max(legs)  # target sits above entry
    else:
        sl_px, sl_oid = max(legs)  # stop sits above entry
        tp_px, tp_oid = min(legs)  # target sits below entry
    return {
        "side": pos.side, "entry": pos.entry,
        "initial_stop_loss": sl_px, "stop_loss": sl_px, "take_profit": tp_px,
        "milestones_locked": 0, "sl_oid": sl_oid, "tp_oid": tp_oid,
    }


def _parse_bracket_oids(resp) -> tuple[int, int] | None:
    """Pull the two order ids out of a successful attach_bracket() response,
    in submission order (sl, tp) — attach_bracket always submits [sl, tp], so
    Hyperliquid's positional response corresponds the same way. None if the
    shape doesn't match (already covered by _has_error before this is called,
    but stay defensive rather than assume)."""
    try:
        statuses = resp["response"]["data"]["statuses"]
        if len(statuses) != 2:
            return None
        oids = [s["resting"]["oid"] if "resting" in s else s["filled"]["oid"] for s in statuses]
        return oids[0], oids[1]
    except (KeyError, IndexError, TypeError):
        return None


class HyperliquidTrader:
    def __init__(
        self,
        client,
        strategy: SMCStrategy,
        screener: TradeScreener,
        risk_pct: float = 1.0,
        leverage: int = 3,
        max_notional_pct: float = 100.0,  # cap position notional at N% of buying power
        capital_guard: CapitalGuard | None = None,
        combined_guard: CapitalGuard | None = None,
    ):
        self.client = client
        self.strategy = strategy
        self.screener = screener
        self.risk_pct = risk_pct
        self.leverage = leverage
        self.max_notional_pct = max_notional_pct
        self.capital_guard = capital_guard
        self.combined_guard = combined_guard
        self._tracked: dict[str, dict] = {}  # coin -> {side, entry, stop_loss, take_profit}

    def guard_check(self, account_value: float, confidence: float | None = None) -> tuple[bool, str | None]:
        """Daily-loss / drawdown / concurrent-risk gate, checked right before an
        order is sent. Returns (True, None) if no guard is configured.

        combined_guard is checked first and is purely additive — it halts
        across both live venues (see bot/combined_ledger.py) but never
        overrides this venue's own capital_guard; its state is updated
        separately, by whichever caller feeds it the combined balance each
        pass, not from account_value here (which is Hyperliquid-only).

        `confidence` (when supplied) is checked against the runtime
        authorization floor set from the control panel (live_state.min_confidence):
        raising that floor means "only auto-authorize the higher-probability
        setups." A signal already passed the config-level min_confidence to be
        approved at all — this is an additional, live-adjustable gate on top.

        Per-position risk isn't tracked by the venue (Hyperliquid only reports
        current size/entry, not the risk_pct used when it was opened), so each
        open position is approximated at the trader's own configured risk_pct
        — a reasonable stand-in since every trade this bot places is sized
        with that same value.
        """
        if self.combined_guard is not None and self.combined_guard.halted:
            return False, f"combined ledger: {self.combined_guard.halt_reason}"
        if confidence is not None:
            floor = live_state.get_min_confidence()
            if confidence < floor:
                return False, f"confidence {confidence:.0%} below authorization floor {floor:.0%}"
        if self.capital_guard is None:
            return True, None
        self.capital_guard.update(account_value, trading_day())
        open_positions = [
            OpenRisk(i, self.risk_pct) for i in range(len(self.client.account().positions))
        ]
        return self.capital_guard.can_open_new_trade(open_positions, self.risk_pct)

    @staticmethod
    def queue_if_below_auto_fire(coin: str, signal: Signal, plan: TradePlan, unified) -> float | None:
        """Split an approved setup: fire unattended, or park it for review.

        Returns None when final_pct clears the hands-off threshold (caller
        should fire). Otherwise queues the setup for Approve/Cancel and
        returns the threshold it fell short of, so the caller can report it.

        Keyed on the unified gate's BLENDED final_pct rather than raw
        confidence — a high-confidence signal that smart money disagrees with
        should still get a human look, which is the whole point of the blend.
        Shared by both Hyperliquid entry points (run_once here and
        hypertrade.py's scan_and_report) so the two can't drift apart.
        """
        threshold = live_state.get_auto_fire_pct()
        if unified.final_pct >= threshold:
            return None
        pending_trades.add(
            venue="hl", symbol=coin, side=plan.side,
            entry_price=plan.entry, stop_loss=plan.stop_loss, take_profit=plan.take_profit,
            confidence=signal.confidence, final_pct=unified.final_pct,
            smart_money_direction=unified.smart_money_direction,
            smart_money_agreement=unified.smart_money_agreement_count,
            size=plan.usd,  # USD notional on this venue (MT5 queues lots instead)
        )
        return threshold

    def _plan(
        self, coin: str, signal: Signal, account_value: float, withdrawable: float | None = None
    ) -> TradePlan | None:
        risk_amount = account_value * (self.risk_pct / 100)
        stop_frac = abs(signal.entry - signal.stop_loss) / signal.entry
        if stop_frac <= 0:
            return None
        notional = risk_amount / stop_frac
        # Cap by FREE margin (withdrawable), not total account value — an existing
        # open position already ties up margin that isn't actually available for a
        # new order. In dry-run there's no live withdrawable figure, so fall back
        # to account_value (matches prior behavior). Also honour the $10 floor.
        free = withdrawable if withdrawable is not None else account_value
        buying_power = free * self.leverage * (self.max_notional_pct / 100)
        notional = min(notional, buying_power)
        if notional < 10:
            return None  # can't place a compliant order within the risk budget / free margin
        return TradePlan(
            coin=coin,
            side=signal.type.value,
            usd=round(notional, 2),
            leverage=self.leverage,
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            risk_pct=self.risk_pct,
        )

    def evaluate(
        self, coin: str, ltf: str, htf: str, account_value: float, withdrawable: float | None = None,
        smart_money_direction: str = "NEUTRAL", smart_money_bullish_count: int = 0,
        smart_money_bearish_count: int = 0,
    ):
        """Return (signal, ScreenResult, TradePlan|None, UnifiedResult) without
        trading. The unified gate (bot/unified_screen.py) requires BOTH the
        seven-gate structure screen AND the smart-money read (defaults to
        NEUTRAL, which never blocks, when the caller doesn't supply one) —
        only a unified-approved signal gets sized into a plan.

        Candles fall back to Binance (bot/marketdata.py) if Hyperliquid's own
        fetch fails (rate limit / connection reset) -- Hyperliquid stays the
        primary source (it's the actual execution venue), Binance only fills
        in a scan pass that would otherwise silently error out for this coin.
        If BOTH sources fail this raises, same as the plain
        self.client.candles() call used to -- callers (evaluate_many's
        per-coin try/except, run_once) already handle that failure mode, so
        it must stay a raise here rather than quietly returning a placeholder
        signal that would trip `unified.approved` on a None."""
        df = candles_with_binance_fallback(self.client, coin, ltf, 72)
        htf_df = candles_with_binance_fallback(self.client, coin, htf, 240)
        if df is None or htf_df is None:
            raise RuntimeError(f"no candles for {coin} (Hyperliquid and Binance both unavailable)")
        signal = self.strategy.analyze(df, htf_df)
        result = self.screener.screen(signal, df, htf_df)
        unified = evaluate_unified(
            signal, result, smart_money_direction, smart_money_bullish_count, smart_money_bearish_count
        )
        plan = self._plan(coin, signal, account_value, withdrawable) if unified.approved else None
        return signal, result, plan, unified

    def execute(self, plan: TradePlan):
        """Send the approved trade to the venue (real order, whichever venue
        the client is connected to — testnet or mainnet), then attach a
        reduce-only stop-loss/take-profit bracket so the position is actually
        enforced instead of riding unprotected. A rejected/unfilled order
        never gets a bracket attempt (nothing to protect); a bracket that
        fails to attach after a real fill is a loud warning, never silent —
        a silently-unprotected position is exactly the bug this closes."""
        if plan.side == "long":
            resp = self.client.long(plan.coin, plan.usd, leverage=plan.leverage)
        else:
            resp = self.client.short(plan.coin, plan.usd, leverage=plan.leverage)

        fill = _parse_fill(resp)
        if fill is None:
            return resp

        try:
            bracket = self.client.attach_bracket(
                plan.coin, is_buy=(plan.side == "long"), size=fill["size"],
                stop_loss=plan.stop_loss, take_profit=plan.take_profit,
            )
        except Exception as e:
            print(f"  !! WARNING: {plan.coin} filled but bracket SL/TP FAILED to attach "
                  f"({type(e).__name__}: {e}) — position is UNPROTECTED, check it manually.")
            return resp

        if _has_error(bracket):
            print(f"  !! WARNING: {plan.coin} filled but bracket SL/TP was REJECTED "
                  f"({bracket}) — position is UNPROTECTED, check it manually.")
            return resp

        self._tracked[plan.coin] = {
            "side": plan.side, "entry": fill["price"],
            "initial_stop_loss": plan.stop_loss,  # immutable — the R-multiple basis for ratchet_stops()
            "stop_loss": plan.stop_loss, "take_profit": plan.take_profit,
            "milestones_locked": 0,
        }
        oids = _parse_bracket_oids(bracket)
        if oids:
            self._tracked[plan.coin]["sl_oid"], self._tracked[plan.coin]["tp_oid"] = oids
        return resp

    def check_exits(self, open_positions: list) -> list[dict]:
        """Reconcile tracked positions against the venue's current open
        positions (passed in by the caller, already fetched this pass — no
        extra network call here). Any tracked coin no longer open has closed,
        via its SL/TP bracket or a manual close, and is reported + dropped."""
        open_coins = {p.coin for p in open_positions}
        closed = []
        for coin in list(self._tracked):
            if coin not in open_coins:
                closed.append({"coin": coin, **self._tracked.pop(coin)})
        return closed

    def enforce_brackets(self, open_positions: list, account_value: float) -> list[dict]:
        """Self-healing safety net, not just a best-effort attach at
        execute() time: for every currently open position, confirm it
        actually has a resting SL/TP bracket, and (re)attach one if not.

        Covers cases execute()'s one-shot attempt can't: a bracket that
        failed (a bad price, a transient venue error) and only got a logged
        warning, a position that predates this tracker (bot restarted while
        it was open), or a bracket that was cancelled some other way. Uses
        the tracked plan's own SL/TP when known; otherwise falls back to a
        stop distance sized off this trader's own risk_pct of current equity
        (see the comment below on why NOT a flat percentage of price)."""
        enforced = []
        for pos in open_positions:
            resting = self.client.open_trigger_orders(pos.coin)
            if len(resting) >= 2:
                # Already protected. If we don't have it tracked (e.g. this
                # bracket was attached by a previous process before oid
                # tracking existed, or before this process started), backfill
                # from the resting orders themselves — otherwise it's
                # invisible to ratchet_stops() forever, not just this pass.
                if pos.coin not in self._tracked:
                    backfilled = _infer_bracket_from_resting(pos, resting)
                    if backfilled:
                        self._tracked[pos.coin] = backfilled
                continue

            tracked = self._tracked.get(pos.coin)
            if tracked:
                stop_loss, take_profit = tracked["stop_loss"], tracked["take_profit"]
            else:
                # The position's size was already fixed when it opened — often
                # against a tight sniper stop (0.2-1%) that, combined with the
                # buying-power cap, produced a LARGE size. Applying a flat
                # max_stop_pct (e.g. 2%) to that same size risks far more
                # dollars than this trader's own risk_pct intends. Caught
                # live: POPCAT lost tracking after a restart, got re-protected
                # at a flat 2% stop on its full size, and the eventual
                # stop-out cost ~6% of account equity in one trade instead of
                # the intended ~1%. Fix: solve backwards from a fixed dollar
                # risk budget (account_value * risk_pct) to a stop distance
                # for THIS size, instead of a fixed price percentage.
                cfg = self.screener.cfg if self.screener else None
                rr = cfg.min_rr if cfg else 2.0
                risk_amount = account_value * (self.risk_pct / 100)
                stop_distance = risk_amount / pos.size if pos.size else 0
                if pos.side == "long":
                    stop_loss, take_profit = pos.entry - stop_distance, pos.entry + stop_distance * rr
                else:
                    stop_loss, take_profit = pos.entry + stop_distance, pos.entry - stop_distance * rr

            try:
                result = self.client.attach_bracket(
                    pos.coin, is_buy=(pos.side == "long"), size=pos.size,
                    stop_loss=stop_loss, take_profit=take_profit,
                )
            except Exception as e:
                print(f"  !! ENFORCE FAILED: {pos.coin} still unprotected "
                      f"({type(e).__name__}: {e}) — check it manually.")
                continue
            if _has_error(result):
                print(f"  !! ENFORCE FAILED: {pos.coin} still unprotected ({result})")
                continue

            self._tracked[pos.coin] = {
                "side": pos.side, "entry": pos.entry,
                "initial_stop_loss": stop_loss,  # immutable — the R-multiple basis for ratchet_stops()
                "stop_loss": stop_loss, "take_profit": take_profit,
                "milestones_locked": 0,
            }
            oids = _parse_bracket_oids(result)
            if oids:
                self._tracked[pos.coin]["sl_oid"], self._tracked[pos.coin]["tp_oid"] = oids
            enforced.append({"coin": pos.coin, "stop_loss": stop_loss, "take_profit": take_profit})
            print(f"  ++ enforced bracket on {pos.coin}: SL {stop_loss:.6g} / TP {take_profit:.6g}")
        return enforced

    def ratchet_stops(self, open_positions: list, lock_in_pct: float = 0.25) -> list[dict]:
        """Trail the stop-loss forward as a position gains, locking in
        `lock_in_pct` of profit for every full multiple of the position's
        original risk (1R, 2R, 3R...) it reaches — the take-profit and the
        rest of the position are untouched, this only ever tightens the SL,
        never loosens it. R-multiples are measured off `initial_stop_loss`
        (fixed at open), not the current, possibly-already-ratcheted stop, so
        each milestone locks in a consistent slice of real progress.

        Needs `sl_oid` (captured when the bracket was placed) to move the
        resting order in place; positions tracked before this existed, or
        still missing an oid for any other reason, are skipped here — the
        next enforce_brackets() pass re-attaches a fresh bracket and this
        picks them up from there."""
        by_coin = {p.coin: p for p in open_positions}
        ratcheted = []
        for coin, info in self._tracked.items():
            pos = by_coin.get(coin)
            if pos is None or "sl_oid" not in info:
                continue

            entry = info["entry"]
            risk_per_unit = abs(entry - info["initial_stop_loss"])
            if risk_per_unit <= 0 or pos.size <= 0:
                continue

            current_price = (
                entry + pos.unrealized_pnl / pos.size if pos.side == "long"
                else entry - pos.unrealized_pnl / pos.size
            )
            gain_per_unit = (current_price - entry) if pos.side == "long" else (entry - current_price)
            r_multiple = gain_per_unit / risk_per_unit
            milestone = int(r_multiple) if r_multiple >= 1 else 0
            if milestone <= info.get("milestones_locked", 0):
                continue  # no new full-R milestone reached since the last check

            locked_r = milestone * lock_in_pct
            new_sl = entry + locked_r * risk_per_unit if pos.side == "long" else entry - locked_r * risk_per_unit
            current_sl = info["stop_loss"]
            improves = new_sl > current_sl if pos.side == "long" else new_sl < current_sl
            if not improves:
                continue

            try:
                result = self.client.modify_trigger_order(
                    coin, oid=info["sl_oid"], is_buy=(pos.side != "long"), size=pos.size,
                    trigger_px=new_sl, tpsl="sl",
                )
            except Exception as e:
                print(f"  !! RATCHET FAILED: {coin} stop not advanced ({type(e).__name__}: {e})")
                continue
            if _has_error(result):
                print(f"  !! RATCHET FAILED: {coin} stop not advanced ({result})")
                continue

            info["stop_loss"] = new_sl
            info["milestones_locked"] = milestone
            ratcheted.append({"coin": coin, "milestone": milestone, "new_stop_loss": new_sl})
            print(f"  ~~ ratcheted {coin} stop to {new_sl:.6g} (locked {locked_r:.2f}R after reaching {milestone}R)")
        return ratcheted

    def scan(
        self, coins: list[str], ltf: str, htf: str, account_value: float, withdrawable: float | None = None,
        smart_money_direction: str = "NEUTRAL", smart_money_bullish_count: int = 0,
        smart_money_bearish_count: int = 0,
    ):
        """Evaluate a list of coins. Returns (coin, signal, result, plan, unified, error)
        rows; one coin failing (e.g. no candles) never aborts the rest of the scan."""
        rows = []
        for coin in coins:
            try:
                signal, result, plan, unified = self.evaluate(
                    coin, ltf, htf, account_value, withdrawable,
                    smart_money_direction, smart_money_bullish_count, smart_money_bearish_count,
                )
                rows.append((coin, signal, result, plan, unified, None))
            except Exception as e:  # keep scanning the rest of the watchlist
                rows.append((coin, None, None, None, None, str(e)))
        return rows

    def run_once(
        self, coin: str, ltf: str, htf: str, account_value: float, dry_run: bool = True,
        withdrawable: float | None = None, smart_money_direction: str = "NEUTRAL",
        smart_money_bullish_count: int = 0, smart_money_bearish_count: int = 0,
    ):
        signal, result, plan, unified = self.evaluate(
            coin, ltf, htf, account_value, withdrawable,
            smart_money_direction, smart_money_bullish_count, smart_money_bearish_count,
        )

        if signal.type == SignalType.NONE:
            print(f"[{coin}] no SMC setup — {signal.reason}")
            return None

        print(f"[{coin}] {signal.type.value.upper()} candidate ({signal.confidence:.0%}) "
              f"— {signal.reason}")
        print(f"  entry {signal.entry:.4g}  SL {signal.stop_loss:.4g}  TP {signal.take_profit:.4g}")
        print("  Screening:")
        print(result.table())
        print(f"  Smart money: {unified.smart_money_direction} "
              f"({unified.smart_money_agreement_count}/9 modules agree) | Final: {unified.final_pct:.0f}%")

        if not unified.approved:
            print(f"  -> not traded ({unified.reason})\n")
            return result

        if plan is None:
            print("  -> approved, but not sizable — no free margin (locked in an existing "
                  "position) or amount < $10 min. No order.\n")
            return result

        print(f"  Plan: {plan.side.upper()} ${plan.usd} of {coin} at {plan.leverage}x "
              f"(risk {plan.risk_pct}% of ${account_value:,.2f})")
        if dry_run:
            print("  -> DRY RUN — no order sent (use --live on a funded wallet to fire)\n")
        else:
            allowed, reason = self.guard_check(account_value, confidence=signal.confidence)
            if not allowed:
                print(f"  -> BLOCKED by capital guard: {reason}\n")
            else:
                threshold = self.queue_if_below_auto_fire(coin, signal, plan, unified)
                if threshold is not None:
                    print(f"  -> final {unified.final_pct:.0f}% below auto-fire {threshold:.0f}% "
                          f"— QUEUED for approval on the control panel\n")
                else:
                    print(f">>> LIVE ORDER FIRED: {plan.side.upper()} {coin} ${plan.usd} at {plan.leverage}x "
                          f"SL={plan.stop_loss:.6g} TP={plan.take_profit:.6g}")
                    print("  ", self.execute(plan), "\n")
        return result
