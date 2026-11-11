# Autosave — main

- Saved: 2026-11-11 18:20:16 EST
- Machine: MacBook-Pro-2
- Repo path: /Users/mac/Projects/smc-trading-bot
- Branch: main @ 2ecf5fc
- Claude session: 46a4fd47-6f51-4c73-9b5a-879922842c85 (Stop)

## Resume
    git fetch origin && git checkout -B main-resume origin/autosave/main

## Uncommitted changes at save time
```
?? .serena/
```

## Last request
push
shadow report
plugged in, check battery later
Try again
shadow report
shadow report
MONITOR
MONITOR
retest
add watchdog and push

## Last Claude reply
57, about 13.5 hours:** the live bot couldn't read MT5, then crash-looped because the bridge refused connections.
- **Today from 14:39, about 3.5 hours,** until the Mac restarted around 15:00.

The likely cause is the MT5 terminal freezing while its bridge process stays up, so restarting the bridge alone doesn't fix it. I can add a watchdog that restarts the MT5 terminal when this happens, but it's a bigger change and I haven't made it. Want it?

Everything is healthy right now. One commit (`c89eb27`, the monitor) is still waiting for a **"push"**.
Before writing the restart logic, I need to know exactly how MT5 and its bridge are started on this Mac:
I'm blocked from running the live restart test, so I'll do the rest: load the watchdog into the running monitor, then commit and push.
The watchdog is built, running inside the monitor, and pushed to `main` (`2ecf5fc`, which includes the earlier monitor commit `c89eb27`). One thing is unproven: **the restart itself has never actually run**, because I was blocked from triggering a real test restart.

**What it does when MT5 freezes:**
1. Waits until MT5 has been unreadable for **10 minutes**.
2. Probes MT5 directly. If it answers, it doesn't restart.
3. Otherwise it stops the frozen MT5 terminal, restarts the bridge, waits up to about 90 seconds for MT5 to answer, then restarts the bot loop so it reconnects.
4. Sends a notification with the result.
5. Waits at least 30 minutes between restarts. After **3 restarts in 6 hours** it stops and tells you to take a look, so it can't loop forever.

**Tested:** the decision logic (9 tests, 595 in the full suite), and the probe, which reads healthy MT5 correctly in about 1 second.

**Not tested:** stopping and relaunching the MT5 terminal for real. If you want proof now, while the balance is $0 and nothing is open, run this. It takes about 1–2 minutes, then say **"check"** and I'll confirm the terminal, AutoTrading and the bot loop all came back:
```bash
cd ~/Projects/smc-trading-bot && venv/bin/python -c "import sys;sys.path.insert(0,'scripts');import health_monitor as h;print('restarted OK:',h.restart_mt5())"
```

**Before it ever fires live:**
- **It closes the MetaTrader 5 window**, including your manual trading screen, for a minute or two. Stops and targets on open trades stay on Exness's servers, so they're unaffected.
- **AutoTrading:** it has survived restarts so far. If it ever comes back off, the watchdog won't notice, but MT5 would refuse the bot's orders.
