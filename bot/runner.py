from __future__ import annotations

import os
import time

import yaml
from dotenv import load_dotenv

from bot.exchange import Exchange
from bot.evm.dex import EVMDex
from bot.evm.wallet import EVMWallet
from bot.mt5.broker import MT5Broker
from bot.mt5.client import MT5Client
from bot.risk import calc_lot_size, calc_position_size
from bot.smc.strategy import SMCStrategy, SignalType


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


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
    )

    symbol = config["symbol"]
    timeframe = config["timeframe"]
    htf = config.get("higher_timeframe", "1h")
    poll = config.get("poll_interval_sec", 30)
    risk_pct = config.get("risk_per_trade_pct", 1.0)
    max_open_trades = config.get("max_open_trades", 1)
    initial_balance = config.get("initial_balance", 10000.0)

    exchange: Exchange | None = None
    evm_dex: EVMDex | None = None
    broker: MT5Broker | None = None

    if venue == "mt5":
        # MT5 supplies both market data and execution via the remote bridge.
        symbol = config.get("mt5_symbol", symbol)
        timeframe = config.get("mt5_timeframe", timeframe)
        host = os.getenv("MT5_HOST", "127.0.0.1")
        port = os.getenv("MT5_PORT", "18812")
        client = MT5Client.connect(
            host=host,
            port=port,
            login=os.getenv("MT5_LOGIN", ""),
            password=os.getenv("MT5_PASSWORD", ""),
            server=os.getenv("MT5_SERVER", ""),
        )
        broker = MT5Broker(client, symbol=symbol, mode=mode, initial_balance=initial_balance)
        data = broker
        executor = broker
        print(f"  MT5 Bridge: {host}:{port}")
    else:
        # CEX exchange supplies OHLCV data (and execution for the cex venue).
        exchange = Exchange(
            exchange_id=os.getenv("EXCHANGE", "binance"),
            api_key=os.getenv("API_KEY", ""),
            api_secret=os.getenv("API_SECRET", ""),
            mode=mode,
            initial_balance=initial_balance,
        )
        data = exchange
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

    price_decimals = 5 if venue == "mt5" else 2

    print("=" * 60)
    print("  SMC Trading Bot — Smart Money Concepts")
    print("=" * 60)
    print(f"  Venue:     {venue.upper()}")
    print(f"  Symbol:    {symbol}")
    print(f"  Timeframe: {timeframe} (HTF: {htf})")
    print(f"  Mode:      {mode.upper()}")
    if venue in ("cex", "mt5"):
        print(f"  Balance:   ${executor.get_balance():,.2f}")
    print("=" * 60)
    print("  Scanning for SMC confluence...\n")

    while True:
        try:
            df = data.fetch_ohlcv(symbol, timeframe, limit=200)
            htf_df = data.fetch_ohlcv(symbol, htf, limit=100)
            current_price = float(df.iloc[-1]["close"])

            executor.check_exit(current_price)
            open_count = 1 if executor.position is not None else 0

            if open_count < max_open_trades:
                signal = strategy.analyze(df, htf_df)

                if signal.type != SignalType.NONE:
                    if venue == "evm" and evm_dex:
                        balance = evm_dex.get_usdc_balance()
                    else:
                        balance = executor.get_balance()

                    if venue == "mt5" and broker:
                        size = calc_lot_size(
                            broker.get_symbol_info(),
                            balance,
                            signal.entry,
                            signal.stop_loss,
                            risk_pct,
                        )
                    else:
                        size = calc_position_size(
                            balance, signal.entry, signal.stop_loss, risk_pct
                        )

                    if size > 0:
                        side = "long" if signal.type == SignalType.LONG else "short"
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
                                symbol=symbol,
                            )
                        print(f"        Confidence: {signal.confidence:.0%}")
                else:
                    ts = df.iloc[-1]["timestamp"]
                    print(f"[{ts}] No signal — {signal.reason} | "
                          f"Price: {current_price:.{price_decimals}f}")

            time.sleep(poll)

        except KeyboardInterrupt:
            print("\nBot stopped.")
            log = executor.trade_log
            if log:
                print(f"\nTrade log ({len(log)} events):")
                for t in log:
                    print(f"  {t}")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(poll)
