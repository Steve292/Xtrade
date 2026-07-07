# MetaTrader 5 venue setup (remote bridge)

The bot runs natively on your Mac, but MetaTrader 5's Python API is
Windows-only. So the MT5 terminal and a small RPC server run on a **remote
box**, and the bot connects to it over the network. This keeps your Mac clean
and runs the terminal where it's actually supported.

```
  Mac (this repo)                         Remote box you provision
  ┌─────────────────────┐   rpyc/TCP    ┌──────────────────────────────┐
  │ bot  (venue: mt5)   │◀────────────▶ │ MT5 terminal (broker DEMO)   │
  │  connects as client │  port 18812   │ + mt5linux RPC server        │
  └─────────────────────┘               └──────────────────────────────┘
```

## 1. Create a broker MT5 **demo** account

Any MT5 broker (or MetaQuotes' own demo). You need three things:

- **login** (account number)
- **password**
- **server** (e.g. `ICMarkets-Demo`, `MetaQuotes-Demo`)

## 2. Stand up the remote box

Cheapest reliable option is a small **Windows VPS** (~$6–15/mo). On it:

1. Install the MetaTrader 5 terminal and log into your demo account.
2. Install Python (Windows) and the packages:
   ```
   pip install MetaTrader5 mt5linux
   ```
3. Start the RPC server (default port 18812):
   ```
   python -m mt5linux "C:\\path\\to\\python.exe"
   ```
   Leave the terminal **and** this server running.
4. Allow inbound TCP on the server port (firewall / security group), or keep it
   private and reach it over a VPN / SSH tunnel (recommended — don't expose the
   port to the open internet).

> Linux+Wine and MetaApi.cloud are alternatives — see the plan file. The steps
> below are identical from the Mac's side regardless of which you pick.

## 3. Point the bot at it

In `.env` on your Mac:

```
MODE=paper                 # keep paper until the smoke test passes
MT5_HOST=<remote box IP or tunnel host>
MT5_PORT=18812
MT5_LOGIN=<demo login>
MT5_PASSWORD=<demo password>
MT5_SERVER=<broker server>
```

In `config.yaml`:

```
venue: mt5
mt5_symbol: EURUSD         # exact broker symbol, incl. any suffix
mt5_timeframe: 15m
```

## 4. Verify, then trade the demo

```bash
source venv/bin/activate
pip install mt5linux

# 1) connection + data only, never trades — safe on a live demo account
python scripts/mt5_smoke_test.py

# 2) run the strategy on real MT5 candles, simulated fills (MODE=paper)
python main.py

# 3) when you're satisfied, set MODE=live in .env to route real orders
#    to the DEMO account, and run again
python main.py
```

Start at step 1 and only advance once each step looks right. `MODE=paper` pulls
real MT5 candles but simulates fills, so you can watch the strategy behave on
live forex/CFD data before any order is sent.

## Notes

- `mt5_symbol` must match your broker **exactly**. Many brokers add suffixes
  (`EURUSD.r`, `XAUUSD.m`); the smoke test tells you if the name doesn't
  resolve.
- Position sizing is in **lots**, computed from the symbol's tick value and
  your `risk_per_trade_pct`, floored to the broker's volume step (see
  `calc_lot_size` in `bot/risk.py`).
- `mt5linux` is community-maintained; if the client/server rpyc versions
  mismatch, pin matching versions on both ends.
