# Autosave — capital-guard-and-deposit-funding

- Saved: 2026-10-23 07:04:43 EDT
- Machine: MacBook-Pro-2
- Repo path: /Users/mac/Projects/smc-trading-bot
- Branch: capital-guard-and-deposit-funding @ 4c6d28a
- Claude session: 46a4fd47-6f51-4c73-9b5a-879922842c85 (Stop)

## Resume
    git fetch origin && git checkout -B capital-guard-and-deposit-funding-resume origin/autosave/capital-guard-and-deposit-funding

## Uncommitted changes at save time
```
 M config.yaml
```

## Last request
deleted the old one from local
scan and check skills again
thats the new skill saved
save and push to git main trader x
ready for auto trading ?
3
verify guard


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

## Last Claude reply
at fails (old support becomes resistance), with the stop above about 82k. After a close above 84k, buy the dip back to 84k, with the stop under about 82.5k.
2. **The downside has better math.** From the trigger, the downside offers about 1.5x the room of the upside, and it agrees with the 22% → 6% momentum decay. Even a bullish close only buys you a move back to a resistance that has already failed twice.

## Putting it into TraderX

I can turn this into a **weekly-bias gate for BTC**, driven by config:
- weekly close **below 80k** → BTC **shorts only**;
- weekly close **above 84k** → **longs only**;
- **between** → **no BTC trades.**

The levels would live in `config.yaml`, so you update them as the structure changes. The bot's own 15m triggers would still pick the exact entry.

I can also **alert you at the weekly close** with the closing price and which scenario triggered.

Want the bias gate, the alert, or both?
Agreed. Patience is the edge here. A range at a decision point is where guessing loses money, and you already have clear triggers either way.

One thing to know: **the bot isn't waiting.** It's armed and will auto-fire a BTC trade between 80k and 84k if its 15m gates line up, which is the chop zone your plan says to sit out.

If you want the bot to wait too, I can **pause BTC only until the weekly close**, leaving gold running. After Sunday 00:00 UTC, you set the direction from how the candle closed.

Want me to pause BTC, or leave the bot as is?
Backtesting first, on your real MT5 BTCUSDc data with the live spread. The test runs the bot's actual signals (its SMCStrategy on 15m entries, 4h trend) over as much history as MT5 provides, then compares the outcomes with and without the bias filter.

**Bias definition:** bearish = 4h close below the 4h SMA200 **and** a lower high (the last ~5 days' high is below the high of the ~2 weeks before that). Bullish is the mirror: above the SMA200 with a higher low.
I recorded the result in the repo's TraderX skill and pushed it to `main` (`4c6d28a`, verified on the remote). Your claude.ai account copy of the skill doesn't have this row yet; I can repackage it if you want the account version updated too.

**The bot is unchanged.** It's live and armed: 15m entries, the original gates, $0.50 risk per trade, the 10% daily / 30% drawdown guard, auto-fire at 50%, and no bias filter. For BTC, the weekly close is still your call: below 80k means wait for a short retest, above 84k means wait to buy the dip.
