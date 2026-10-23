---
name: traderx
description: "The real TraderX trading bot (/Users/mac/Projects/smc-trading-bot) -- live SMC bot trading real money on MT5 (Exness cent account, BTCUSDc/XAUUSDc) and Hyperliquid. Load before ANY TraderX task: scanning, gates, scalping, auto-fire, sizing, adding to positions, or \"why didn't it trade\". Holds the architecture, the risk/psychology rules distilled from \"Master The Market\" (Hedgeye), and the MEASURED results of every strategy idea already tested -- so dead ends are not re-tested and losers are never automated."
---

# TraderX

## Hard rules (never break)
- Never place, modify or close a real order yourself. Screening, analysis, config and code are fine; orders are the operator's or the armed bot's.
- Never enable a strategy to auto-fire without a holdout test on the REAL instrument (MT5 data, real spread). A proxy (Binance PAXG for gold) is not enough -- see results below.
- Every config/numeric value lives in `config.yaml` / `live_state.json`. Read them fresh; numbers here are history, not the current setting.

## Architecture (where to look)
- MT5 loop: `main.py` -> `bot/runner.py` (launchd `com.smc.mt5autotrader`). Hyperliquid: `hypertrade.py` (`com.smc.autotrader`). Dashboard: `webapp/server.py`, port 8420 (`com.smc.webapp`). Bridge: `com.smc.mt5bridge` (port 18812).
- Signal: `bot/smc/strategy.py` `SMCStrategy` -- internal floor `min_confidence` (wired to `screening.min_confidence`). `htf_neutral_credit` is PRE-gate (changes which bars signal); `extended_detectors` is post-gate (confidence only).
- Gates: `bot/screening.py` `TradeScreener` -- SMC confluence, top-down, liquidity sweep, (opt) structure shift, (opt) range location, R:R, sniper, supply/demand (opt-out), Fibonacci OTE (opt-out).
- Per-symbol gate overrides: `config.yaml` `symbol_profiles: {SYM: {reward_risk_ratio, screening: {...}}}` (none configured).
- Blend: `bot/unified_screen.py` `final_pct`; auto-fire when `final_pct >= live_state.auto_fire_pct`, else queued for dashboard approval.
- Sizing: `fixed_risk_usd` (dollars/trade) -> `bot/position_sizing.py` chain -> dollar ceiling. Capital guard: `bot/capital_guard.py`.
- Health check first: if every service is crash-looping with `ModuleNotFoundError: bot`, the working tree lost `bot/`/`webapp/` -- `git restore -- bot webapp engine` (happened 2026-09-16, ~5 weeks down unnoticed).

## Process & psychology (from "Master The Market", K. McCullough -- a risk-management book, not SMC)
1. **Hope is not a risk-management process.** Every entry, add and exit follows a written rule.
2. **Protect the pile first.** -20% needs +25% to recover; -50% needs +100%. Survival beats any single win.
3. **Avoid "resulting".** Judge the decision, not the outcome. A winner closed by a broker stop-out is not a good trade.
4. **Pros are right 55-65%.** Expect ~40% losers; size so a losing streak is survivable.
5. **Buy low in the range, only when TRADE and TREND both agree** (fractal: on 5m entries, TRADE = 1h, TREND = 4h). Never fade a dip while the trend is broken -- moves cluster (Mandelbrot), "cheap gets cheaper".
6. **Exit on process, not hope:** lower highs -> reduce; break TRADE -> minimum size; break TRADE + TREND -> out.
7. **Add only in increments, only on a confirmed set-up** (holding TRADE and TREND support). Never add while TRADE is breaking, never double down on a mistake.
8. **Position limits by volatility.** Crypto is sized like commodities (smallest bucket).
9. **Change your mind when the data changes.** Don't fall in love with a position or a strategy.

## Operator-trade lessons (2026-10-23, BTC around 81k)
- The 4 winning longs were bought at **32-41% of the 24h range**; the 4 losing longs at **89-98%** (chasing the breakout). Location alone separated all nine.
- The best entries came right after a **sweep of the prior low and a close back above it**.
- Winners were held 2.5-8h and closed by a **broker margin stop-out** caused by withdrawing 2,480 USC and adding gold longs with exposure open. Never withdraw or add with positions open.

## Measured results -- do not re-test without new data
All on 5m, take-profit/stop-loss-first, spread included unless noted. "R" = units of risk.
| Idea | Result | Verdict |
|---|---|---|
| Loosen SMC gates for chop (MSS off, RR1.5, tight stop, OTE/S-D off) | BTC -11R, gold -18R over 3 weeks | Rejected |
| + range-location gate (<=40%) | BTC -1.5R, gold -5R | Cuts losses, no edge; shipped OFF |
| Raw sweep-and-reclaim scalp (48 configs) | 0/48 profitable in both halves on BTC or gold | Rejected |
| Book rules: 1h+4h trend agree + volume-weighted volatility range + sweep/higher-low | Binance BTC: recent +101R but previous month -37R. Binance PAXG: 12/12 positive. **Real MT5 XAUUSDc, 0.24 spread: 8/12 negative (~-62R)** | Rejected -- the PAXG edge did not survive the real instrument |

| BTC bias filter: block longs when 4h close < SMA200 AND lower high (MT5 BTCUSDc 15m, 7 months, 433 raw signals, live spread) | Blocked longs were the BEST group (+1.4R, 35% win); filtered result -51.4R vs -50.0R baseline | Rejected -- momentum-decay read is for discretionary weekly calls, not a 15m filter |
| BTC patience gate (30-day daily range, longs only bottom 25%, shorts only top 25%) | Blocked -57.2R, kept 98 at +7.2R (38% win) vs -50.0R; production replay identical | **LIVE for BTCUSDc** via symbol_profiles -- a loss filter, ~breakeven |
| "Wait for a breakout close, then trade that direction" (D1/W1 boxes) | Negative in every variant | Rejected |
| Support/resistance targets (TP just before next 1h swing level, skip < 1.5R; also stop beyond S/R) | Alone -51.7R -> -2.4R (by skipping trades into nearby resistance). WITH patience: fixed 2R +5.8R beats S/R TP +2.1R / -0.5R | Rejected -- patience already encodes the operator's S/R principle |
| (context) the bot's raw SMC signals, ungated | -0.12R/trade over 7 months; gates approved 2/433 | The gates are the edge -- never loosen them to "get trades" |

Takeaway: on the operator's Exness account the 5m spread (BTC ~15, XAU ~0.24) consumes small scalp edges. Look for edges on 15m+ or in sizing/exits, and validate on MT5 data first.

## Known tooling bug
`scripts/validate_confluence.py` `fetch()` steps back one bar per page, so `--bars` above ~1000 silently returns ~4 days. Fetch forward from `now - bars*tf` instead.
