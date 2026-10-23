# Autosave — capital-guard-and-deposit-funding

- Saved: 2026-10-23 07:12:08 EDT
- Machine: MacBook-Pro-2
- Repo path: /Users/mac/Projects/smc-trading-bot
- Branch: capital-guard-and-deposit-funding @ d17f02a
- Claude session: 46a4fd47-6f51-4c73-9b5a-879922842c85 (Stop)

## Resume
    git fetch origin && git checkout -B capital-guard-and-deposit-funding-resume origin/autosave/capital-guard-and-deposit-funding

## Uncommitted changes at save time
```
(none)
```

## Last request


capital_guard:
  max_daily_loss_pct: 3.0
  max_drawdown_pct: 30.0
  max_concurrent_trades: 3

saved


But there's no need to guess sides.

Just be patient.

Wait for weekly candle to play out.

> If you want, I can add this “lower high + momentum decay” read as a BTC bias in the bot: block BTC longs while price is below the 4h 200 SMA after a failed retest of the high, and allow only shorts. I’d backtest it on MT5 data first, like everything else

back test before adding


enforce
> One thing to know: the bot isn’t waiting. It’s armed and will auto-fire a BTC trade between 80k and 84k if its 15m gates line up, which is the chop zone your plan says to sit out.
> 
> If you want the bot to wait too, I can pause BTC only until the weekly close, leaving gold running. After Sunday 00:00 UTC, you set the direction from how the candle closed.
> 
> Want me to pause BTC, or leave the bot as is?

backtest and add a patience system with a risk ready parameters


enforce

## Last Claude reply
s the same 335. Enabling it for BTC only, through the per-symbol profile:
It's live, and the loop is already judging a real BTC long. Checking that the profile line printed at startup, since it appears just before the banner I filtered on:
The BTC profile loaded at startup. Committing and pushing to `main`:
I built the patience system, it's live on BTC, and it's pushed to `main` (`d17f02a`, verified on the remote). It cuts losses sharply but isn't a proven money-maker, and right now it's actually allowing BTC longs.

**What the backtest showed** (7 months of the bot's real BTC signals, 433 trades, live spread):

| | Trades | Net (R = amount risked per trade) |
|---|---|---|
| No patience gate | 433 | −50.0R |
| Trades the gate blocks | 335 | −57.2R |
| Trades the gate keeps | 98 (38% win rate) | **+7.2R** |
| "Wait for a breakout close, then trade that direction" | — | negative in every setting, so not built |

The trades it blocks are where almost all the losses came from. What's left is about break-even, so treat it as a loss filter rather than an edge. The gate as built reproduced the backtest exactly: same 98 kept, same 335 blocked.

**How it works:** it looks at the high and low of the last 30 completed daily candles.
- Longs only in the bottom 25% of that range; shorts only in the top 25%.
- Anything in the middle waits.
- If the daily data can't be fetched, it waits rather than trading.
- It only ever blocks trades, never adds any. It's on for BTC only, because gold wasn't tested.

**Settings you can change** (in `config.yaml` under `symbol_profiles: BTCUSDc:`):
- `patience_enabled` turns the gate on or off.
- `patience_lookback` is how many days the range covers (30).
- `patience_extreme` is how close to the edge a trade must be (0.25 means the outer 25%).

**The catch — it allows longs right now.** BTC's 30-day range is 79,677–87,284.
- BTC at 81,534 sits 24% above the low, just inside the long zone, which ends at 81,579.
- Shorts are only allowed above 85,382.
- If a long fires, it would be a range-low buy, the kind of entry your best 81k trades used. That conflicts with your plan to wait for the weekly close.
- The latest BTC signal (a long scoring 84%) was still rejected by another gate: there was no recent structure shift.
- If you want the bot to wait for Sunday's close as well, say **"pause BTC until weekly close"**.

The commit also includes your capital-guard change (10% daily loss / 30% drawdown) and the 573 tests, all passing.
