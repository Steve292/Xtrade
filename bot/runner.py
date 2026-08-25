from __future__ import annotations

import os
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from bot import live_state
from bot.capital_guard import CapitalGuard, trading_day
from bot.combined_ledger import fetch_combined_balance, reconnect_if_needed
from bot.exchange import Exchange
from bot.evm.dex import EVMDex
from bot.evm.wallet import EVMWallet
from bot.hyperliquid.client import HyperliquidClient
from bot.market_snapshot import get_cached_snapshot
from bot.mt5.broker import MT5Broker
from bot.mt5.client import MT5Client
from bot.mt5.metaapi_client import MetaApiClient
from bot import pending_trades
from bot.position_sizing import (
    SizingFactors,
    apply_risk_ceiling,
    atr,
    confidence_multiplier,
    final_risk_pct,
    regime_alloc_weight,
    risk_pct_for_fixed_usd,
    staged_fixed_risk_usd,
    volatility_adjust,
)
from bot import knowledge as knowledge_mod
from bot.risk import calc_lot_size, calc_position_size
from bot.screening import ScreenConfig, TradeScreener
from bot.smc.strategy import SMCStrategy, SignalType
from bot.unified_screen import evaluate_unified
from bot.wallet import DefiWallet


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _connect_hl_ledger(config: dict) -> HyperliquidClient:
    """Raises on failure -- caller decides how to handle it. Split out so the
    main loop can retry this every pass (see the note on hl_ledger_client
    below) instead of only ever trying once at startup."""
    hl_cfg = config.get("hyperliquid", {})
    hl_wallet = DefiWallet.from_env() or DefiWallet.load()
    return HyperliquidClient.connect(
        private_key=hl_wallet.private_key if hl_wallet else "",
        testnet=hl_cfg.get("testnet", True),
    )


def run_bot(config_path: str = "config.yaml") -> None:
    load_dotenv()
    config = load_config(config_path)

    venue = config.get("venue", "cex")  # cex | evm | mt5
    mode = os.getenv("MODE", "paper")

    strategy = SMCStrategy(
        swing_lookback=config.get("swing_lookback", 5),
        order_block_lookback=config.get("order_block_lookback", 20),
        fvg_min_size_pct=config.get("fvg_min_size_pct", 0.001),
        liquidity_tolerance_pct=config.get("liquidity_tolerance_pct", 0.0005),
        reward_risk_ratio=config.get("reward_risk_ratio", 2.0),
        stop_loss_pct=config.get("stop_loss_pct"),
        extended_detectors=config.get("smc", {}).get("extended_detectors", False),
        extended_max_adjust=config.get("smc", {}).get("extended_max_adjust", 0.10),
    )
    # Same seven-gate screen the Hyperliquid path (hypertrade.py) has always
    # required — this loop previously entered on the raw SMC signal alone
    # (signal.type != NONE), a real gap where MT5/CEX/EVM traded on a
    # materially weaker bar than Hyperliquid. Screening only ever ADDS
    # scrutiny on top of the existing arm-gate/confidence-floor checks below,
    # it never loosens anything.
    screener = TradeScreener(ScreenConfig.from_dict(config.get("screening", {})))

    symbol = config["symbol"]
    timeframe = config["timeframe"]
    htf = config.get("higher_timeframe", "1h")
    poll = config.get("poll_interval_sec", 30)
    risk_pct = config.get("risk_per_trade_pct", 1.0)
    max_open_trades = config.get("max_open_trades", 1)
    initial_balance = config.get("initial_balance", 10000.0)
    # Staged fixed-dollar risk (config.yaml's fixed_risk_usd) overrides
    # risk_pct above when enabled — recomputed every pass from the live
    # combined balance below, not applied here since that balance isn't
    # known yet at startup.
    fixed_risk_cfg = config.get("fixed_risk_usd", {})
    # Adaptive sizing (bot/position_sizing.py's multiplier chain) and the
    # knowledge confluence layer. Both OFF unless config.yaml turns them on;
    # with both off this loop behaves exactly as it did before they existed.
    sizing_cfg = config.get("position_sizing", {})
    # Whether positions this bot did NOT place (manual trades, another EA, a
    # copy-trade feed) constrain it. See the main loop for what it enforces.
    respect_manual = config.get("respect_manual_positions", True)
    knowledge_cfg = config.get("knowledge", {})
    knowledge_index = knowledge_mod.KnowledgeIndex()
    if knowledge_cfg.get("enabled"):
        knowledge_index = knowledge_mod.build_index(
            knowledge_cfg.get("corpus_path", knowledge_mod.DEFAULT_CORPUS_PATH),
            knowledge_cfg.get("cache_path", knowledge_mod.DEFAULT_CACHE_PATH),
        )
        if knowledge_index.available:
            print(f"  Knowledge: {len(knowledge_index.weights)} modules weighted "
                  f"from {knowledge_index.document_count} documents")
        else:
            print("  Knowledge: ENABLED but no usable corpus found — "
                  "scoring will be skipped (no adjustment applied)")

    evm_dex: EVMDex | None = None
    # The MT5 client, kept at this scope so the main loop can ask the account
    # what is actually open. None for every other venue.
    mt5_client = None
    # One entry per scanned symbol: its data source, executor, and (mt5 only)
    # the MT5Broker for lot-sizing off that symbol's own tick economics. Every
    # non-mt5 venue just runs a single-item watchlist — same loop either way.
    watchlist: list[dict] = []
    # Combined-ledger guard: only ever set up for venue == "mt5" (see below),
    # but declared here so the main loop can safely check it for every venue.
    combined_guard: CapitalGuard | None = None
    hl_ledger_client = None

    if venue == "mt5":
        # MT5 supplies both market data and execution, via either backend —
        # both expose the identical MT5Client interface, so MT5Broker below
        # is unchanged regardless of which one is selected.
        timeframe = config.get("mt5_timeframe", timeframe)
        backend = config.get("mt5_backend", "bridge")  # bridge | metaapi
        if backend == "metaapi":
            client = MetaApiClient.connect(
                token=os.getenv("METAAPI_TOKEN", ""),
                login=os.getenv("MT5_LOGIN", ""),
                password=os.getenv("MT5_PASSWORD", ""),
                server=os.getenv("MT5_SERVER", ""),
            )
            print("  MT5 backend: MetaApi.cloud")
        else:
            host = os.getenv("MT5_HOST", "127.0.0.1")
            port = os.getenv("MT5_PORT", "18812")
            client = MT5Client.connect(
                host=host,
                port=port,
                login=os.getenv("MT5_LOGIN", ""),
                password=os.getenv("MT5_PASSWORD", ""),
                server=os.getenv("MT5_SERVER", ""),
                terminal_path=os.getenv("MT5_TERMINAL_PATH", ""),
            )
            print(f"  MT5 Bridge: {host}:{port}")
        mt5_client = client
        cent_divisor = 100.0 if config.get("mt5_cent_account") else 1.0
        mt5_symbols = config.get("mt5_watchlist") or [config.get("mt5_symbol", symbol)]
        for sym in mt5_symbols:
            sym_broker = MT5Broker(
                client, symbol=sym, mode=mode,
                initial_balance=initial_balance, cent_divisor=cent_divisor,
            )
            watchlist.append(
                {"symbol": sym, "data": sym_broker, "executor": sym_broker, "broker": sym_broker}
            )
        symbol = mt5_symbols[0]

        # Combined ledger: a second, independent guard fed the TOTAL across
        # both live venues (MT5 + Hyperliquid), so a bad day on Hyperliquid
        # can halt new MT5 entries too. Same thresholds as capital_guard,
        # own state file — mirrors the identical setup in hypertrade.py.
        # Best-effort — if Hyperliquid isn't reachable at startup, the main
        # loop retries the connection every pass (see below) rather than
        # giving up for the rest of this (often days-long) process's life;
        # a stuck None here previously made the combined guard silently
        # treat Hyperliquid as "contributing $0" instead of skipping the
        # update, which could fire a false drawdown halt off a connectivity
        # blip rather than a real loss.
        if mode == "live":
            guard_cfg = config.get("capital_guard", {})
            guard_thresholds = {
                k: guard_cfg[k] for k in CapitalGuard.__dataclass_fields__ if k in guard_cfg
            }
            combined_guard = CapitalGuard.load(Path("combined_guard_state.json"), **guard_thresholds)
            try:
                hl_ledger_client = _connect_hl_ledger(config)
                print("  Combined ledger: Hyperliquid connected")
            except Exception as e:
                print(f"  Combined ledger: Hyperliquid unavailable at startup ({type(e).__name__}) "
                      f"— guard runs on MT5 balance alone, retrying each pass")
    else:
        # CEX exchange supplies OHLCV data (and execution for the cex venue).
        exchange = Exchange(
            exchange_id=os.getenv("EXCHANGE", "binance"),
            api_key=os.getenv("API_KEY", ""),
            api_secret=os.getenv("API_SECRET", ""),
            mode=mode,
            initial_balance=initial_balance,
        )
        executor = exchange
        if venue == "evm":
            wallet = EVMWallet.from_env()
            evm_dex = EVMDex(wallet, slippage_pct=config.get("evm_slippage_pct", 0.5))
            executor = evm_dex
            bal = wallet.get_balance()
            print(f"  EVM Chain:  {wallet.chain_name}")
            print(f"  Wallet:     {bal.address[:10]}...{bal.address[-4:]}")
            print(f"  ETH:        {bal.eth:.4f}")
            print(f"  USDC:       ${bal.usdc:,.2f}")
            print(f"  RPC:        {'connected' if wallet.is_connected else 'offline (paper)'}")
        watchlist.append({"symbol": symbol, "data": exchange, "executor": executor, "broker": None})

    price_decimals = 5 if venue == "mt5" else 2
    multi = len(watchlist) > 1

    print("=" * 60)
    print("  TraderX — Smart Money Concepts")
    print("=" * 60)
    print(f"  Venue:     {venue.upper()}")
    if multi:
        print(f"  Watchlist: {', '.join(item['symbol'] for item in watchlist)}")
    else:
        print(f"  Symbol:    {symbol}")
    print(f"  Timeframe: {timeframe} (HTF: {htf})")
    print(f"  Mode:      {mode.upper()}")
    if venue == "mt5":
        first_broker = watchlist[0]["broker"]
        raw_balance = first_broker.get_balance()
        print(f"  Balance:   ${first_broker.get_display_balance():,.2f}"
              + (f" ({raw_balance:,.2f} account units)" if first_broker.cent_divisor != 1 else ""))
    elif venue == "cex":
        print(f"  Balance:   ${watchlist[0]['executor'].get_balance():,.2f}")
    if multi:
        print(f"  Max open:  {max_open_trades} (shared across the whole watchlist)")
    if mode == "live":
        print(f"  Armed:     {'YES — will place REAL orders' if live_state.is_armed() else 'NO — screening only until armed via control panel'}")
    print("=" * 60)
    print("  Scanning for SMC confluence...\n")

    while True:
        try:
            open_count = sum(1 for item in watchlist if item["executor"].position is not None)

            # What is open on the ACCOUNT, not just what this bot is tracking.
            # Without this the bot is blind to trades it did not place: on an
            # account also traded by hand, max_open_trades means "one BOT
            # trade" rather than one trade, and the bot happily opens a second
            # position on a symbol the operator is already in -- doubling the
            # real exposure with neither side aware of the other.
            #
            # Positions are separated by magic (bot/mt5/client.py's BOT_MAGIC),
            # so the bot still manages only its own; the manual ones constrain
            # it without ever being touched by it.
            manual_symbols: set = set()
            if respect_manual and mt5_client is not None:
                try:
                    split = mt5_client.positions_split()
                    manual = split["manual"]
                    if manual:
                        manual_symbols = {p["symbol"] for p in manual}
                        # Manual positions count toward max_open_trades: the
                        # limit is about total account exposure, not about how
                        # many of them this bot happens to own.
                        open_count += len(manual)
                        desc = ", ".join(
                            f"{p['symbol']} {p['side']} {p['volume']}" for p in manual
                        )
                        print(f"  [manual] {len(manual)} position(s) not placed by this bot: {desc}"
                              f" — counted toward max_open_trades ({max_open_trades})")
                except Exception as e:
                    # Never trade MORE freely because this lookup failed. Treat
                    # an unreadable account as "something may be open" and sit
                    # the pass out rather than assume it is flat.
                    print(f"  [manual] position lookup failed ({type(e).__name__}) — "
                          f"skipping entries this pass rather than assuming a flat account")
                    open_count = max(open_count, max_open_trades)

            # Global smart-money read (bot/unified_screen.py's second gate,
            # alongside the seven-gate structure screen below) — cached
            # 15 minutes (bot/market_snapshot.py) since it's market-wide, not
            # per-symbol, and this loop polls far more often than that data
            # actually changes. A failed fetch degrades to NEUTRAL (never
            # blocks on its own) rather than halting live trading over a
            # transient Yahoo/CoinGecko/Deribit hiccup.
            try:
                _snapshot = get_cached_snapshot(config)
                sm_direction = _snapshot["smart_money"]["direction"]
                sm_bullish = _snapshot["smart_money"]["bullish_count"]
                sm_bearish = _snapshot["smart_money"]["bearish_count"]
                # Same snapshot already carries the regime label and hotness
                # multiplier adaptive sizing needs — reuse it rather than
                # recompute, which would double this pass's API calls against
                # free tiers the snapshot cache exists to protect.
                regime_label = _snapshot["regime"]["label"]
                hotness_mult = float(_snapshot["hotness"]["multiplier"])
            except Exception as e:
                print(f"  [smart money] snapshot unavailable this pass ({type(e).__name__}) — treating as NEUTRAL")
                sm_direction, sm_bullish, sm_bearish = "NEUTRAL", 0, 0
                # NEUTRAL/1.0 = "no opinion": regime_alloc_weight("NEUTRAL")
                # is 1.0, so a failed snapshot leaves adaptive sizing resting
                # on volatility and confidence alone rather than silently
                # sizing off a stale or fabricated regime.
                regime_label, hotness_mult = "NEUTRAL", 1.0

            # risk_usd is None unless fixed_risk_usd is enabled AND this pass's
            # combined-balance fetch succeeds — None means "use the configured
            # static risk_per_trade_pct" (below), the same fallback as a failed
            # fetch, rather than sizing on stale or missing data. Only decides
            # WHICH STAGE ($3 vs $6) applies here — it's converted to an actual
            # risk_pct per-symbol below, against THAT symbol's own balance, so
            # the realized dollar risk is risk_usd regardless of how the
            # combined total (used only for staging) differs from any one
            # venue's own balance.
            risk_usd = None

            if combined_guard is not None:
                was_down = hl_ledger_client is None
                hl_ledger_client = reconnect_if_needed(hl_ledger_client, lambda: _connect_hl_ledger(config))
                if was_down and hl_ledger_client is not None:
                    print("  Combined ledger: Hyperliquid reconnected")

                if hl_ledger_client is None:
                    # Deliberately skip fetch_combined_balance entirely here
                    # rather than call it with client=None: that function's
                    # None-means-"not tracked, contributes $0" contract is
                    # correct for a deployment that never tracks this venue,
                    # but WRONG here -- this run always tries to track
                    # Hyperliquid, so None only ever means "unreachable right
                    # now." Feeding it in as $0 would look like a real loss
                    # and could fire a false drawdown halt off a connectivity
                    # blip; skipping preserves the guard's last known state.
                    print("  [combined ledger] Hyperliquid unreachable this pass — "
                          "guard state unchanged, using last known status")
                else:
                    combined = fetch_combined_balance(hl_ledger_client, client, cent_divisor)
                    if combined is not None:
                        combined_guard.update(combined.total, trading_day())
                        if fixed_risk_cfg.get("enabled"):
                            risk_usd = staged_fixed_risk_usd(
                                combined.total,
                                low_risk_usd=fixed_risk_cfg.get("low", 3.0),
                                high_risk_usd=fixed_risk_cfg.get("high", 6.0),
                                threshold_usd=fixed_risk_cfg.get("threshold_usd", 100.0),
                            )
                            print(f"  [fixed risk] combined balance ${combined.total:,.2f} -> ${risk_usd:.2f}/trade")
                    else:
                        print("  [combined ledger] balance fetch failed this pass — "
                              "guard state unchanged, using last known status")

            for item in watchlist:
                sym = item["symbol"]
                data = item["data"]
                executor = item["executor"]
                broker = item["broker"]
                try:
                    df = data.fetch_ohlcv(sym, timeframe, limit=200)
                    htf_df = data.fetch_ohlcv(sym, htf, limit=100)
                    current_price = float(df.iloc[-1]["close"])

                    executor.check_exit(current_price)

                    if executor.position is not None or open_count >= max_open_trades:
                        continue
                    if sym in manual_symbols:
                        print(f"[{sym}] skipped — a position not placed by this bot is open "
                              f"on this symbol; not stacking on top of it")
                        continue
                    if combined_guard is not None and combined_guard.halted:
                        print(f"[{sym}] blocked by combined ledger: {combined_guard.halt_reason}")
                        continue

                    signal = strategy.analyze(df, htf_df)
                    screen_result = screener.screen(signal, df, htf_df) if signal.type != SignalType.NONE else None
                    knowledge_result = (
                        knowledge_mod.score_signal(signal.detectors, knowledge_index)
                        if signal.type != SignalType.NONE and knowledge_index.available
                        else None
                    )
                    unified = (
                        evaluate_unified(
                            signal, screen_result, sm_direction, sm_bullish, sm_bearish,
                            knowledge_result=knowledge_result,
                            knowledge_max_adjust_pct=knowledge_cfg.get("max_adjust_pct", 5.0),
                        )
                        if signal.type != SignalType.NONE
                        else None
                    )

                    if signal.type != SignalType.NONE and unified.approved:
                        if venue == "evm" and evm_dex:
                            balance = evm_dex.get_usdc_balance()
                        else:
                            balance = executor.get_balance()

                        # Fixed-dollar risk is converted to a risk_pct against
                        # THIS symbol's own balance (not the combined total,
                        # which only decided the $3-vs-$6 stage above) — that's
                        # what makes the realized dollar risk actually risk_usd
                        # regardless of how the combined figure differs from
                        # any one venue's own balance. cent-account balances
                        # (raw MT5 units) are fine here unconverted: risk_pct is
                        # a ratio, and calc_lot_size's tick_value is in the same
                        # raw units, so the result is unit-consistent either way
                        # — EXCEPT this per-symbol balance is real USD (evm) or
                        # raw cent units (mt5), never combined.total's already-
                        # converted USD, so recompute risk_usd -> risk_pct fresh
                        # per symbol against the units actually in play here.
                        if risk_usd is not None:
                            symbol_balance_usd = balance / cent_divisor if broker is not None else balance
                            effective_risk_pct = risk_pct_for_fixed_usd(risk_usd, symbol_balance_usd)
                        else:
                            effective_risk_pct = risk_pct

                        # Adaptive sizing. The multipliers modulate the risk
                        # already decided above; they do NOT substitute a
                        # base_risk_pct from position_sizing.BASE_RISK_PCT.
                        # That table has no forex bucket at all (see
                        # market_snapshot._analyze_mt5_symbol, which documents
                        # the gap and declines to invent a number), and four of
                        # the seven watchlist symbols are forex pairs. Treating
                        # the staged fixed-dollar risk as the base keeps the
                        # user's own staged rule as the anchor and sidesteps
                        # the missing bucket entirely.
                        if sizing_cfg.get("enabled") and effective_risk_pct > 0:
                            sized_balance = (
                                balance / cent_divisor if broker is not None else balance
                            )
                            vol_adjust = 1.0
                            if len(df) > 100:
                                atr20 = atr(df, period=20).dropna()
                                atr100 = atr(df, period=100).dropna()
                                if len(atr20) and len(atr100):
                                    vol_adjust = volatility_adjust(
                                        float(atr20.iloc[-1]), float(atr100.mean())
                                    )
                            factors = SizingFactors(
                                base_risk_pct=effective_risk_pct,
                                regime_alloc_weight=regime_alloc_weight(regime_label),
                                hotness_multiplier=hotness_mult,
                                volatility_adjust=vol_adjust,
                                confidence_multiplier=confidence_multiplier(signal.confidence),
                            )
                            adapted_pct = final_risk_pct(factors)

                            # Absolute dollar ceiling. final_risk_pct's own 3x
                            # clamp is RELATIVE and cannot express "never risk
                            # more than N dollars" — at a $6 balance a 3x stack
                            # on $3 staged risk authorises more than the whole
                            # account. See position_sizing.apply_risk_ceiling.
                            base_usd = effective_risk_pct / 100 * sized_balance
                            ceiling_usd = min(
                                base_usd * float(sizing_cfg.get("max_multiple", 1.5)),
                                float(sizing_cfg.get("max_risk_usd", 5.0)),
                            )
                            capped_pct = apply_risk_ceiling(
                                adapted_pct, sized_balance, ceiling_usd
                            )
                            print(
                                f"  [sizing] {sym} base {effective_risk_pct:.2f}% "
                                f"(${base_usd:.2f}) -> adapted {adapted_pct:.2f}% "
                                f"-> capped {capped_pct:.2f}% "
                                f"(${capped_pct / 100 * sized_balance:.2f}, "
                                f"ceiling ${ceiling_usd:.2f}) "
                                f"[regime {regime_label} x{regime_alloc_weight(regime_label):.2f}, "
                                f"hot x{hotness_mult:.2f}, vol x{vol_adjust:.2f}, "
                                f"conf x{confidence_multiplier(signal.confidence):.2f}]"
                            )
                            effective_risk_pct = capped_pct

                        if broker is not None:
                            size = calc_lot_size(
                                broker.get_symbol_info(),
                                balance,
                                signal.entry,
                                signal.stop_loss,
                                effective_risk_pct,
                            )
                        else:
                            size = calc_position_size(
                                balance, signal.entry, signal.stop_loss, effective_risk_pct
                            )

                        if size > 0:
                            # Arm gate: in live mode, place a real order ONLY when
                            # armed (the shared control-panel toggle, bot/live_state.py).
                            # Disarmed live mode screens and reports but never sends
                            # an order — the same safety default the Hyperliquid path
                            # (hypertrade.py) uses. Paper mode is unaffected.
                            if mode == "live" and not live_state.is_armed():
                                ts = df.iloc[-1]["timestamp"]
                                prefix = f"[{sym}] " if multi else ""
                                print(f"{prefix}[{ts}] SIGNAL {signal.type.value.upper()} "
                                      f"conf {signal.confidence:.0%} — DISARMED, no live order "
                                      f"(arm via the control panel's Activate toggle to enable)")
                                continue
                            # Runtime authorization floor (control panel): setups
                            # below it aren't worth considering at all — not even
                            # worth queueing for review.
                            floor = live_state.get_min_confidence()
                            if mode == "live" and signal.confidence < floor:
                                ts = df.iloc[-1]["timestamp"]
                                prefix = f"[{sym}] " if multi else ""
                                print(f"{prefix}[{ts}] SIGNAL {signal.type.value.upper()} "
                                      f"conf {signal.confidence:.0%} below authorization floor "
                                      f"{floor:.0%} — no live order")
                                continue

                            side = "long" if signal.type == SignalType.LONG else "short"

                            # Hands-off threshold, measured against the unified
                            # gate's BLENDED final_pct (not raw confidence). At or
                            # above it this fires unattended; below it the setup is
                            # queued for Approve/Cancel on the control panel rather
                            # than dropped, so a decent-but-not-automatic setup is
                            # still actionable instead of vanishing into the log.
                            auto_fire_pct = live_state.get_auto_fire_pct()
                            if mode == "live" and unified.final_pct < auto_fire_pct:
                                pending_trades.add(
                                    venue=venue, symbol=sym, side=side,
                                    entry_price=signal.entry, stop_loss=signal.stop_loss,
                                    take_profit=signal.take_profit, confidence=signal.confidence,
                                    final_pct=unified.final_pct,
                                    smart_money_direction=unified.smart_money_direction,
                                    smart_money_agreement=unified.smart_money_agreement_count,
                                    size=size,
                                )
                                ts = df.iloc[-1]["timestamp"]
                                prefix = f"[{sym}] " if multi else ""
                                print(f"{prefix}[{ts}] SIGNAL {signal.type.value.upper()} "
                                      f"final {unified.final_pct:.0f}% below auto-fire {auto_fire_pct:.0f}% "
                                      f"— QUEUED for approval on the control panel")
                                continue
                            if venue == "evm" and evm_dex:
                                evm_dex.open_position(
                                    side=side,
                                    entry=signal.entry,
                                    size_usd=size * signal.entry,
                                    sl=signal.stop_loss,
                                    tp=signal.take_profit,
                                    reason=signal.reason,
                                )
                            else:
                                executor.open_position(
                                    side=side,
                                    entry=signal.entry,
                                    size=size,
                                    sl=signal.stop_loss,
                                    tp=signal.take_profit,
                                    reason=signal.reason,
                                    symbol=sym,
                                )
                            # Explicit, unambiguous marker for a real fire — a dedicated,
                            # greppable line rather than folding it into the Confidence
                            # print below, so an external watcher (log tail / alert) can
                            # key on "LIVE ORDER FIRED" without parsing every line.
                            prefix = f"[{sym}] " if multi else ""
                            tag = "LIVE ORDER FIRED" if mode == "live" else "PAPER FILL"
                            print(f"{prefix}>>> {tag}: {side.upper()} {sym} size={size:.6g} "
                                  f"entry={signal.entry:.6g} SL={signal.stop_loss:.6g} TP={signal.take_profit:.6g}")
                            print(f"        Confidence: {signal.confidence:.0%} | Final: {unified.final_pct:.0f}%")
                            open_count += 1
                    elif signal.type != SignalType.NONE:
                        # SMC found a candidate but the unified gate (structure +
                        # smart money) rejected it — report whichever layer blocked.
                        ts = df.iloc[-1]["timestamp"]
                        prefix = f"[{sym}] " if multi else ""
                        if not unified.structure_ok:
                            failed = next((c.name for c in screen_result.checks if not c.passed), "?")
                            detail = f"rejected at screen: {failed}"
                        else:
                            detail = f"rejected: {unified.reason}"
                        print(f"{prefix}[{ts}] SIGNAL {signal.type.value.upper()} "
                              f"conf {signal.confidence:.0%} final {unified.final_pct:.0f}% {detail}")
                    else:
                        ts = df.iloc[-1]["timestamp"]
                        prefix = f"[{sym}] " if multi else ""
                        print(f"{prefix}[{ts}] No signal — {signal.reason} | "
                              f"Price: {current_price:.{price_decimals}f}")
                except Exception as e:
                    print(f"[{sym}] Error: {e}")

            time.sleep(poll)

        except KeyboardInterrupt:
            print("\nBot stopped.")
            for item in watchlist:
                log = item["executor"].trade_log
                if log:
                    print(f"\n[{item['symbol']}] Trade log ({len(log)} events):")
                    for t in log:
                        print(f"  {t}")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(poll)
