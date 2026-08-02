#!/usr/bin/env python3
"""
TraderX auto-trader for Hyperliquid. Venue (testnet or mainnet) is whatever
`hyperliquid.testnet` in config.yaml says — check the startup banner, it
states the live venue explicitly. Every candidate signal must clear the full
screen — SMC + Fibonacci + top-down + risk + sniper entry — before it can trade.

    python hypertrade.py BTC                  # dry-run: screen only, no orders
    python hypertrade.py XMR --risk 1 --lev 5 # dry-run with explicit risk/leverage
    python hypertrade.py BTC --loop           # keep scanning on the poll interval
    python hypertrade.py BTC --live           # place REAL orders on approval, on
                                              #   whichever venue config.yaml selects
                                              #   (needs a funded wallet)

Dry-run is the default and sends no orders. `--live` requires a wallet (from
HL_PRIVATE_KEY or wallet_testnet.json) funded on the configured venue.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from bot import live_state
from bot.capital_guard import CapitalGuard, trading_day
from bot.combined_ledger import fetch_combined_balance, reconnect_if_needed
from bot.hyperliquid.client import HyperliquidClient
from bot.hyperliquid.trader import HyperliquidTrader
from bot.market_snapshot import get_cached_snapshot
from bot.mt5.client import MT5Client
from bot.position_sizing import risk_pct_for_fixed_usd, staged_fixed_risk_usd
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.strategy import SMCStrategy, SignalType
from bot.wallet import DefiWallet


def _connect_mt5_ledger() -> MT5Client:
    """Raises on failure -- caller decides how to handle it. Split out so the
    main loop can retry this every pass (see the note on mt5_ledger_client
    below) instead of only ever trying once at startup."""
    return MT5Client.connect(
        host=os.getenv("MT5_HOST", "127.0.0.1"),
        port=os.getenv("MT5_PORT", "18812"),
        login=os.getenv("MT5_LOGIN", ""),
        password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""),
        terminal_path=os.getenv("MT5_TERMINAL_PATH", ""),
    )


def scan_and_report(trader, coins, ltf, htf, account_value, dry_run, withdrawable=None,
                     smart_money_direction="NEUTRAL", smart_money_bullish_count=0,
                     smart_money_bearish_count=0):
    rows = trader.scan(coins, ltf, htf, account_value, withdrawable,
                        smart_money_direction, smart_money_bullish_count, smart_money_bearish_count)
    print(f"{'COIN':<11}{'SIGNAL':<7}{'CONF':>5}  VERDICT")
    print("-" * 52)
    approved = []
    for coin, signal, result, plan, unified, err in rows:
        if err:
            print(f"{coin:<11}{'-':<7}{'':>5}  error: {err[:32]}")
        elif signal.type == SignalType.NONE:
            print(f"{coin:<11}{'-':<7}{'':>5}  no setup")
        elif unified.approved:
            print(f"{coin:<11}{signal.type.value.upper():<7}{signal.confidence:>4.0%}  "
                  f"APPROVED  final {unified.final_pct:.0f}%")
            approved.append((coin, signal, result, plan, unified))
        elif not unified.structure_ok:
            failed = next((c.name for c in result.checks if not c.passed), "?")
            print(f"{coin:<11}{signal.type.value.upper():<7}{signal.confidence:>4.0%}  rejected — {failed}")
        else:
            print(f"{coin:<11}{signal.type.value.upper():<7}{signal.confidence:>4.0%}  "
                  f"rejected — {unified.reason}")

    for coin, signal, result, plan, unified in approved:
        print(f"\n{coin} APPROVED — full screen:")
        print(result.table())
        print(f"  Smart money: {unified.smart_money_direction} "
              f"({unified.smart_money_agreement_count}/9 modules agree) | Final: {unified.final_pct:.0f}%")
        if plan is None:
            print("  -> approved, but not sizable — no free margin (locked in an existing "
                  "position) or amount < $10 min. No order.")
            continue
        print(f"  Plan: {plan.side.upper()} ${plan.usd} of {coin} at {plan.leverage}x")
        if dry_run:
            print("  -> DRY RUN — no order sent")
        else:
            allowed, reason = trader.guard_check(account_value, confidence=signal.confidence)
            if not allowed:
                print(f"  -> BLOCKED by capital guard: {reason}")
            else:
                print(f">>> LIVE ORDER FIRED: {plan.side.upper()} {coin} ${plan.usd} at {plan.leverage}x "
                      f"SL={plan.stop_loss:.6g} TP={plan.take_profit:.6g}")
                print("  ", trader.execute(plan))
    if not approved:
        print("\nNo setups cleared the full screen this pass.")


def main() -> None:
    load_dotenv()
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    hl = cfg.get("hyperliquid", {})

    ap = argparse.ArgumentParser(description="SMC-screened Hyperliquid auto-trader")
    ap.add_argument("coin", nargs="?", default="BTC")
    ap.add_argument("--watchlist", action="store_true", help="scan all configured majors + memecoins")
    ap.add_argument("--live", action="store_true",
                    help="place real orders on the venue set by hyperliquid.testnet in config.yaml (needs funded wallet)")
    ap.add_argument("--loop", action="store_true", help="scan continuously")
    ap.add_argument("--risk", type=float, default=cfg.get("risk_per_trade_pct", 1.0))
    ap.add_argument("--lev", type=int, default=hl.get("default_leverage", 3))
    ap.add_argument("--interval", default=cfg.get("timeframe", "15m"))
    ap.add_argument("--htf", default=cfg.get("higher_timeframe", "1h"))
    ap.add_argument("--balance", type=float, default=cfg.get("initial_balance", 10000.0),
                    help="account value to size against in dry-run")
    args = ap.parse_args()

    wallet = DefiWallet.from_env() or DefiWallet.load()
    if args.live and wallet is None:
        raise SystemExit("--live needs a wallet. Run: python hyperwallet.py create")

    client = HyperliquidClient.connect(
        private_key=wallet.private_key if wallet else "",
        testnet=hl.get("testnet", True),
    )

    strategy = SMCStrategy(
        swing_lookback=cfg.get("swing_lookback", 5),
        order_block_lookback=cfg.get("order_block_lookback", 20),
        fvg_min_size_pct=cfg.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=cfg.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=cfg.get("reward_risk_ratio", 2.0),
        stop_loss_pct=cfg.get("stop_loss_pct"),
    )
    screener = TradeScreener(ScreenConfig.from_dict(cfg.get("screening", {})))
    # Staged fixed-dollar risk (config.yaml's fixed_risk_usd) overrides
    # trader.risk_pct below when enabled — same mechanism bot/runner.py
    # uses for MT5, recomputed every pass from the live combined balance.
    fixed_risk_cfg = cfg.get("fixed_risk_usd", {})
    guard_cfg = cfg.get("capital_guard", {})
    guard_thresholds = {k: guard_cfg[k] for k in CapitalGuard.__dataclass_fields__ if k in guard_cfg}
    # load(), not the plain constructor — must survive restarts, see capital_guard.py's module docstring.
    capital_guard = CapitalGuard.load(**guard_thresholds)

    # Combined ledger: a second, independent guard fed the TOTAL across both
    # live venues (MT5 + Hyperliquid), so a bad day on MT5 can halt new
    # Hyperliquid entries too. Same thresholds as the per-venue guard, own
    # state file. Best-effort — if MT5 isn't reachable at startup, the main
    # loop retries the connection every pass (see below) rather than giving
    # up for the rest of this (often days-long) process's life; a stuck
    # None here previously made the combined guard silently treat MT5 as
    # "contributing $0" instead of skipping the update, which could fire a
    # false drawdown halt off a connectivity blip rather than a real loss.
    combined_guard = mt5_ledger_client = None
    mt5_cent_divisor = 100.0 if cfg.get("mt5_cent_account") else 1.0
    if args.live:
        combined_guard = CapitalGuard.load(Path("combined_guard_state.json"), **guard_thresholds)
        try:
            mt5_ledger_client = _connect_mt5_ledger()
            print("  Combined ledger: MT5 connected")
        except Exception as e:
            print(f"  Combined ledger: MT5 unavailable at startup ({type(e).__name__}) "
                  f"— guard runs on Hyperliquid balance alone, retrying each pass")

    trader = HyperliquidTrader(client, strategy, screener, risk_pct=args.risk, leverage=args.lev,
                                capital_guard=capital_guard, combined_guard=combined_guard)

    watch = None
    if args.watchlist:
        watch = list(dict.fromkeys((hl.get("majors") or []) + (hl.get("memecoins") or [])))

    venue_label = "testnet" if hl.get("testnet", True) else "REAL MONEY — mainnet"
    mode = f"LIVE ({venue_label} orders)" if args.live else "DRY RUN (no orders)"
    target = f"watchlist ({len(watch)} coins)" if watch else args.coin
    start_balance = client.account().account_value if args.live else args.balance
    print("=" * 60)
    print(f"  TraderX auto-trader — Hyperliquid {'testnet' if hl.get('testnet', True) else 'MAINNET'}")
    print(f"  Scan: {target}   TF: {args.interval}/{args.htf}   Risk: {args.risk}%   Lev: {args.lev}x")
    print(f"  Mode: {mode}   Sizing balance: ${start_balance:,.2f}")
    if args.live:
        print(f"  Armed: {'YES — will place real orders' if live_state.is_armed() else 'NO — screening only until armed via control panel'}")
    if args.live and start_balance == 0:
        print("  Wallet unfunded — screening only until you fund it via the faucet.")
    print("=" * 60)

    poll = cfg.get("poll_interval_sec", 30)
    try:
        while True:
            # A transient venue error (502, timeout, rate limit) must not kill a
            # long-running bot — log it and retry on the next pass.
            try:
                # Re-read the live balance/positions each pass so funding the
                # wallet mid-run automatically arms sniping. `armed` is the
                # control panel's runtime toggle — checked fresh every pass so
                # it can flip trading on/off without restarting the service.
                armed = live_state.is_armed()
                effective_dry_run = not (args.live and armed)

                # Global smart-money read (bot/unified_screen.py's second gate,
                # alongside the seven-gate screen below) — same cached,
                # NEUTRAL-on-failure approach bot/runner.py uses for MT5.
                try:
                    _snapshot = get_cached_snapshot(cfg)
                    sm_direction = _snapshot["smart_money"]["direction"]
                    sm_bullish = _snapshot["smart_money"]["bullish_count"]
                    sm_bearish = _snapshot["smart_money"]["bearish_count"]
                except Exception as e:
                    print(f"  [smart money] snapshot unavailable this pass ({type(e).__name__}) — treating as NEUTRAL")
                    sm_direction, sm_bullish, sm_bearish = "NEUTRAL", 0, 0

                if args.live:
                    acct = client.account()
                    account_value, withdrawable, open_positions = (
                        acct.account_value, acct.withdrawable, acct.positions
                    )
                    for c in trader.check_exits(open_positions):
                        print(f"[{c['coin']}] position closed — was {c['side'].upper()} "
                              f"entry {c['entry']:.4g}, SL {c['stop_loss']:.4g} / TP {c['take_profit']:.4g}")
                    trader.enforce_brackets(open_positions, account_value)
                    trader.ratchet_stops(open_positions)
                    if not armed:
                        print("  [disarmed — screening only, use the control panel's Activate "
                              "toggle to allow live orders]")

                    if combined_guard is not None:
                        was_down = mt5_ledger_client is None
                        mt5_ledger_client = reconnect_if_needed(mt5_ledger_client, _connect_mt5_ledger)
                        if was_down and mt5_ledger_client is not None:
                            print("  Combined ledger: MT5 reconnected")

                        if mt5_ledger_client is None:
                            # Deliberately skip fetch_combined_balance entirely
                            # here rather than call it with client=None: that
                            # function's None-means-"not tracked, contributes
                            # $0" contract is correct for a deployment that
                            # never tracks this venue, but WRONG here -- this
                            # run always tries to track MT5, so None only ever
                            # means "unreachable right now." Feeding it in as
                            # $0 would look like a real loss and could fire a
                            # false drawdown halt off a connectivity blip;
                            # skipping preserves the guard's last known state.
                            print("  [combined ledger] MT5 unreachable this pass — "
                                  "guard state unchanged, using last known status")
                        else:
                            combined = fetch_combined_balance(client, mt5_ledger_client, mt5_cent_divisor)
                            if combined is not None:
                                combined_guard.update(combined.total, trading_day())
                                if fixed_risk_cfg.get("enabled"):
                                    # combined.total only decides the $3-vs-$6 stage;
                                    # the pct is converted against THIS venue's own
                                    # account_value so the realized dollar risk on a
                                    # Hyperliquid trade is genuinely risk_usd even if
                                    # MT5's balance differs from Hyperliquid's.
                                    risk_usd = staged_fixed_risk_usd(
                                        combined.total,
                                        low_risk_usd=fixed_risk_cfg.get("low", 3.0),
                                        high_risk_usd=fixed_risk_cfg.get("high", 6.0),
                                        threshold_usd=fixed_risk_cfg.get("threshold_usd", 100.0),
                                    )
                                    trader.risk_pct = risk_pct_for_fixed_usd(risk_usd, account_value)
                                    print(f"  [fixed risk] combined balance ${combined.total:,.2f} -> "
                                          f"${risk_usd:.2f}/trade ({trader.risk_pct:.1f}% of ${account_value:,.2f})")
                            else:
                                print("  [combined ledger] balance fetch failed this pass — "
                                      "guard state unchanged, using last known status")
                else:
                    account_value, withdrawable, open_positions = args.balance, args.balance, []

                if watch:
                    scan_and_report(trader, watch, args.interval, args.htf, account_value,
                                    dry_run=effective_dry_run, withdrawable=withdrawable,
                                    smart_money_direction=sm_direction, smart_money_bullish_count=sm_bullish,
                                    smart_money_bearish_count=sm_bearish)
                elif args.live and open_positions:
                    print(f"[{args.coin}] position already open — skipping scan")
                else:
                    trader.run_once(args.coin, args.interval, args.htf, account_value,
                                    dry_run=effective_dry_run, withdrawable=withdrawable,
                                    smart_money_direction=sm_direction, smart_money_bullish_count=sm_bullish,
                                    smart_money_bearish_count=sm_bearish)
            except Exception as e:
                print(f"[transient error] {type(e).__name__}: {str(e)[:100]} — retrying next pass")

            if not args.loop:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
