# MetaTrader 5 venue setup

The bot runs natively on your Mac, but MetaTrader 5's Python API is
Windows-only, so the terminal has to run somewhere else. Two backends handle
this — pick one, set `mt5_backend` in `config.yaml` accordingly:

- **`bridge`** (default) — a small **remote box you provision and manage**
  (Windows VPS, or Wine) runs the actual MT5 terminal + an RPC server
  (`mt5linux`); the bot connects to it over TCP.
- **`metaapi`** — [MetaApi.cloud](https://metaapi.cloud) hosts and manages the
  terminal for you. No box to provision, patch, or keep running — you only
  need an API token. Recommended unless you specifically want full control of
  the box.

Both backends expose the identical interface to the rest of the bot
(`bot/mt5/broker.py` doesn't know or care which one is active), and both go
through the same demo-first verification gate below.

```
  Mac (this repo)                         Where the MT5 terminal actually runs
  ┌─────────────────────┐   rpyc/TCP    ┌──────────────────────────────┐
  │ bot  (venue: mt5)   │◀────────────▶ │ bridge: MT5 terminal on a    │
  │  connects as client │  port 18812   │   box you provision + manage │
  └─────────────────────┘               └──────────────────────────────┘
  ┌─────────────────────┐  HTTPS/token  ┌──────────────────────────────┐
  │ bot  (venue: mt5)   │◀────────────▶ │ metaapi: terminal hosted     │
  │  connects via SDK   │               │   and managed by MetaApi.cloud│
  └─────────────────────┘               └──────────────────────────────┘
```

## 1. Create a broker MT5 **demo** account

Any MT5 broker (or MetaQuotes' own demo). You need three things:

- **login** (account number)
- **password**
- **server** (e.g. `ICMarkets-Demo`, `MetaQuotes-Demo`)

## 2A. Backend `bridge` — stand up your own remote box

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

Linux+Wine works too, same idea — the MT5 terminal and `mt5linux` just need
somewhere Windows-compatible to run.

## 2B. Backend `metaapi` — create a MetaApi.cloud account

1. Sign up at [metaapi.cloud](https://metaapi.cloud) yourself (free tier
   available) and generate an API token from the dashboard.
2. That's it for provisioning — no server to stand up. `MetaApiClient.connect()`
   (`bot/mt5/metaapi_client.py`) adds your MT5 login to MetaApi and deploys the
   cloud terminal automatically on first connect (can take a couple of minutes
   the very first time).

**Known caveat:** MetaApi documents its historical-candle RPC as **G1-only**.
If your account provisions as G2, `copy_rates()` will fail clearly rather than
silently — `scripts/metaapi_smoke_test.py` (step 4 below) checks this
specifically before you rely on it for anything.

## 3. Point the bot at it

In `.env` on your Mac — same login/password/server either way, plus one
backend-specific value:

```
MODE=paper                 # keep paper until the smoke test passes
MT5_LOGIN=<demo login>
MT5_PASSWORD=<demo password>
MT5_SERVER=<broker server>

# bridge only:
MT5_HOST=<remote box IP or tunnel host>
MT5_PORT=18812

# metaapi only:
METAAPI_TOKEN=<your MetaApi API token>
```

In `config.yaml`:

```
venue: mt5
mt5_backend: bridge        # or: metaapi
mt5_symbol: EURUSD         # exact broker symbol, incl. any suffix
mt5_timeframe: 15m
```

## 4. Verify, then trade the demo

```bash
source venv/bin/activate
pip install mt5linux              # bridge backend
pip install metaapi-cloud-sdk     # metaapi backend

# 1) connection + data only, never trades — safe on a live demo account
python scripts/mt5_smoke_test.py       # bridge
python scripts/metaapi_smoke_test.py   # metaapi

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
  `calc_lot_size` in `bot/risk.py`). The `metaapi` backend approximates tick
  value from the live price snapshot's `profitTickValue` — MetaApi doesn't
  expose a static per-lot tick value the way raw MT5's `symbol_info()` does.
- `mt5linux` is community-maintained; if the client/server rpyc versions
  mismatch, pin matching versions on both ends.
- Either backend, this all defaults to a **demo** account — nothing here
  routes to a real-money MT5 account until you deliberately point `MT5_LOGIN`
  at one and set `MODE=live`, same as every other venue in this bot.
