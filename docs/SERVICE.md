# Running the auto-trader 24/7 (macOS launchd service)

A `nohup` process dies when the terminal/session ends. To keep the screened
live auto-trader running around the clock, install it as a launchd
**LaunchAgent** — it starts at login, restarts itself if it crashes, and runs
the `--watchlist --live --loop` screener continuously.

## Install / uninstall

```bash
scripts/install-service.sh     # generate the LaunchAgent, load it, start it
scripts/uninstall-service.sh   # stop and remove it
```

The install script writes `~/Library/LaunchAgents/com.smc.autotrader.plist`
(pointing at this project's venv + `hypertrade.py`) and boots it.

## Control & inspect

```bash
# status (running? pid?)
launchctl print gui/$(id -u)/com.smc.autotrader | grep -E 'state|pid'

# live log
tail -f logs/autotrader.log

# restart now
launchctl kickstart -k gui/$(id -u)/com.smc.autotrader

# stop for good
scripts/uninstall-service.sh
```

## What it does

- **RunAtLoad** — starts as soon as it's loaded, and at every login.
- **KeepAlive** — if the process exits (crash), launchd restarts it after a
  30s throttle. Combined with the loop's own transient-error handling, a venue
  502 is retried in-loop and a hard crash is recovered by launchd.
- Logs stdout/stderr to `logs/autotrader.log` (gitignored).

## Honest limits of "24/7" on a laptop

- A LaunchAgent runs **while you're logged in and the Mac is awake**. If the Mac
  **sleeps**, the bot pauses. For real always-on, either keep it plugged in with
  sleep disabled (`caffeinate -s`, or System Settings → Battery/Energy), or —
  the robust answer — run it on a cheap always-on **cloud VPS** instead of the
  laptop.
- It trades only once the wallet is funded; unfunded, it screens 24/7 and holds.
- It's pointed at **testnet**. Do not repoint it at mainnet without a funded,
  audited setup and a hard think about the capital guard limits.
