# SMC Trading Bot

Smart Money Concepts (SMC) trading bot with **backtesting**, **EVM wallet/DEX** support, and a native **macOS menu bar app**.

## Features

| Feature | Description |
|---------|-------------|
| **SMC Strategy** | Order blocks, FVG, liquidity sweeps, BOS/CHoCH, premium/discount |
| **Backtesting** | Walk-forward simulation with win rate, Sharpe, drawdown, profit factor |
| **EVM Wallet** | Trade on-chain via Uniswap V2 (Base, Ethereum, Arbitrum) |
| **macOS App** | Native menu bar app — start/stop bot, backtest, scan from the tray |
| **Paper Mode** | Default — no API keys or private keys needed |

## Quick Start (macOS)

```bash
cd ~/Projects/smc-trading-bot
chmod +x scripts/install-macos.sh
./scripts/install-macos.sh
```

Then:

```bash
source venv/bin/activate
python main.py                  # Run live paper bot
python backtest.py --bars 2000  # Backtest on historical data
open macos/SMCBot.app           # Menu bar app
```

## Backtesting

```bash
# Live historical data from Binance
python backtest.py --bars 2000

# Save equity curve
python backtest.py --bars 3000 --output results/equity.csv

# Offline demo (no network)
python backtest_demo.py

# Use local CSV
python backtest.py --csv data/btc_15m.csv
```

**Metrics reported:** total return, win rate, profit factor, max drawdown, Sharpe ratio.

## EVM Wallet (On-Chain DEX)

Set `venue: evm` in `config.yaml` and configure `.env`:

```env
MODE=paper
EVM_CHAIN=base
EVM_PRIVATE_KEY=        # only for live trading
EVM_RPC_URL=            # optional, uses public RPC by default
```

Supported chains: **ethereum**, **base**, **arbitrum**

| Signal | On-Chain Action |
|--------|-----------------|
| Long | Swap USDC → ETH |
| Short | Swap ETH → USDC |

Paper mode simulates swaps with a virtual wallet (1 ETH + $10,000 USDC).

> **Warning:** Live EVM trading sends real transactions. Test in paper mode first. Never commit private keys.

## macOS Menu Bar App

The app lives in your menu bar (top-right):

- **Start Bot** — launches paper trading loop
- **Stop Bot** — terminates the bot
- **Run Backtest** — runs 1500-bar backtest, shows results
- **Scan Market** — one-shot SMC analysis on live data
- **Open Project** — opens project folder in Finder

Launch via:
```bash
open macos/SMCBot.app
# or
python menubar.py
```

Works on **Apple Silicon (M1/M2/M3/M4)** and **Intel** Macs running macOS 12+.

## Configuration

`config.yaml`:

```yaml
symbol: BTC/USDT
timeframe: 15m
venue: cex          # cex | evm
evm_chain: base
risk_per_trade_pct: 1.0
reward_risk_ratio: 2.0
```

## Project Structure

```
smc-trading-bot/
├── main.py              # Bot entry point
├── backtest.py          # Historical backtester
├── backtest_demo.py     # Offline backtest demo
├── menubar.py           # macOS menu bar entry
├── scan.py              # One-shot market scan
├── config.yaml
├── scripts/
│   └── install-macos.sh # macOS setup script
├── macos/
│   └── SMCBot.app       # Native menu bar app (built by installer)
└── bot/
    ├── runner.py        # Main bot loop (CEX + EVM)
    ├── exchange.py      # CCXT + paper trading
    ├── risk.py          # Position sizing
    ├── backtest/
    │   └── engine.py    # Backtest engine + metrics
    ├── evm/
    │   ├── wallet.py    # EVM wallet (Web3)
    │   ├── dex.py       # Uniswap V2 swaps
    │   └── chains.py    # Chain configs
    ├── macos/
    │   └── menubar.py   # rumps menu bar app
    └── smc/             # SMC strategy modules
```

## Disclaimer

For educational purposes only. Trading cryptocurrencies and on-chain assets involves substantial risk. Past backtest performance does not guarantee future results.
