"""
Asset-specific entry rules — blueprint Section 5. Report-only, like every
other module built for this blueprint this session: NOT wired into
bot/runner.py's live execute() path. The existing seven-gate SMC/Fibonacci
screener (bot/screening.py, bot/smc/strategy.py) remains the actual live
entry logic for both real-money accounts; this is a second, independent
opinion using a completely different (classic-TA) methodology, surfaced for
information only.

Every check is reported as True / False / None — None means "no data
available to evaluate this," never silently treated as pass or fail. Four
of the blueprint's checks have no free data source at all (checked: no
Glassnode/CoinGlass/LunarCrush/whale-tracking/exchange-flow API key exists
anywhere in this project):
  - ETF 7-day net flow (majors)
  - Whale netflow 24h (memecoins)
  - Social mentions change (memecoins)
  - Exchange outflow / accumulation (altcoins)
Since the blueprint requires ALL checks to pass for a BUY, and these four
can never be evaluated without a paid key, the majors/meme/alt verdicts
will realistically cap out at INCOMPLETE rather than ever reaching BUY —
that is the honest, correct behavior given what's actually available, not a
bug. Wire in a real provider for any of these four and the verdict can
reach BUY like the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from bot.indicators import ema, is_sloping_up, macd, macd_bullish_crossover, rsi

MAJOR_SL_PCT = 7.0
MAJOR_TP_PLAN = [(1.5, 0.5), (3.0, 0.3)]  # (R multiple, fraction of position exited); remaining 20% trails
MAJOR_TRAIL_ATR_MULT = 2.0

MEME_SL_PCT = 15.0
MEME_TP_PLAN = [(1.5, 0.5), (3.0, 0.3)]
MEME_TRAIL_ATR_MULT = 3.0

ALT_SL_PCT = 10.0
ALT_TP_PLAN = [(1.5, 0.5), (3.0, 0.5)]  # blueprint doesn't specify a trailing leg for alts


@dataclass
class RuleCheck:
    name: str
    passed: bool | None  # None = no data available to evaluate this check
    detail: str = ""


@dataclass
class EntryRuleResult:
    asset_class: str  # "major" | "meme" | "alt"
    verdict: str  # "BUY" | "NO_SETUP" | "INCOMPLETE"
    checks: list[RuleCheck] = field(default_factory=list)
    stop_loss_pct: float = 0.0
    tp_plan: list[tuple[float, float]] = field(default_factory=list)
    trail_atr_mult: float | None = None


def _verdict_from_checks(checks: list[RuleCheck]) -> str:
    if any(c.passed is False for c in checks):
        return "NO_SETUP"
    if any(c.passed is None for c in checks):
        return "INCOMPLETE"
    return "BUY"


def _last_or_none(series: pd.Series) -> float | None:
    if series.empty or pd.isna(series.iloc[-1]):
        return None
    return float(series.iloc[-1])


def _ema_last(series: pd.Series, period: int) -> tuple[pd.Series | None, float | None]:
    """EMA is only treated as meaningful once there's at least `period` bars
    of real history. bot/indicators.py's ema() itself doesn't enforce this
    (a legitimate, common convention — it's defined recursively from the
    very first sample, unlike an SMA-seeded average), but for an actual
    entry decision a "200 EMA" computed from 10 bars is noise, not signal —
    so this module gates on it explicitly rather than reporting false
    confidence from an under-warmed value."""
    if len(series) < period:
        return None, None
    s = ema(series, period)
    return s, _last_or_none(s)


def _check(name: str, value, predicate) -> RuleCheck:
    if value is None:
        return RuleCheck(name, None, "no data available for this check")
    return RuleCheck(name, bool(predicate(value)))


def evaluate_major(
    df_daily: pd.DataFrame,
    df_4h: pd.DataFrame,
    funding_rate: float | None,
    cvd_1h_signal: str | None,
    ssr: float | None,
    etf_7d_flow_usd: float | None = None,  # no free source — always None unless a caller wires one in
) -> EntryRuleResult:
    """BTC/ETH majors rule. `cvd_1h_signal` is the "signal" field from
    bot/smart_money.cvd_signal() (a candle-derived proxy — see that module's
    docstring for why it isn't real tick-level CVD)."""
    price = _last_or_none(df_daily["close"])
    ema200_series, ema200 = _ema_last(df_daily["close"], 200)
    ema50_series, ema50 = _ema_last(df_daily["close"], 50)
    rsi14 = _last_or_none(rsi(df_daily["close"], 14))
    macd_daily_cross = macd_bullish_crossover(macd(df_daily["close"]))
    macd_4h_cross = macd_bullish_crossover(macd(df_4h["close"]))

    checks = [
        RuleCheck(
            "price_above_rising_200ema",
            (price > ema200 and is_sloping_up(ema200_series)) if price is not None and ema200 is not None else None,
        ),
        RuleCheck("golden_cross_50_over_200ema", (ema50 > ema200) if ema50 is not None and ema200 is not None else None),
        RuleCheck("rsi14_between_50_and_80", (50 < rsi14 < 80) if rsi14 is not None else None),
        RuleCheck("macd_bullish_crossover_daily_and_4h", macd_daily_cross and macd_4h_cross),
        _check("funding_rate_below_0.01pct", funding_rate, lambda v: v < 0.0001),
        _check("cvd_1h_positive", cvd_1h_signal, lambda v: v == "BUY"),
        _check("etf_7d_flow_positive", etf_7d_flow_usd, lambda v: v > 0),
        _check("ssr_below_4", ssr, lambda v: v < 4.0),
    ]
    return EntryRuleResult("major", _verdict_from_checks(checks), checks, MAJOR_SL_PCT, MAJOR_TP_PLAN, MAJOR_TRAIL_ATR_MULT)


def evaluate_meme(
    df_4h: pd.DataFrame,
    meme_score: float | None,
    btc_24h_change_pct: float | None,
    volume_30d_avg: float | None,
    whale_netflow_24h_usd: float | None = None,  # no free source
    social_mentions_change_pct: float | None = None,  # no free source
) -> EntryRuleResult:
    price = _last_or_none(df_4h["close"])
    ema50_series, ema50 = _ema_last(df_4h["close"], 50)
    current_volume = _last_or_none(df_4h["volume"])

    checks = [
        _check("meme_score_above_60", meme_score, lambda v: v > 60),
        _check("btc_24h_change_above_1.5pct", btc_24h_change_pct, lambda v: v > 1.5),
        RuleCheck(
            "price_above_rising_50ema_4h",
            (price > ema50 and is_sloping_up(ema50_series)) if price is not None and ema50 is not None else None,
        ),
        RuleCheck(
            "volume_above_1.2x_30d_avg",
            (current_volume > volume_30d_avg * 1.2) if current_volume is not None and volume_30d_avg else None,
        ),
        _check("whale_netflow_24h_above_500k", whale_netflow_24h_usd, lambda v: v > 500_000),
        _check("social_mentions_up_20pct_organic", social_mentions_change_pct, lambda v: v > 20),
    ]
    return EntryRuleResult("meme", _verdict_from_checks(checks), checks, MEME_SL_PCT, MEME_TP_PLAN, MEME_TRAIL_ATR_MULT)


def evaluate_altcoin(
    df_alt: pd.DataFrame,
    df_alt_btc: pd.DataFrame,
    regime_score: float | None,
    btc_d_trend: str | None,  # "falling" | "flat" | "rising" | None -- see bot/hotness.DominanceTrend
    exchange_outflow: bool | None = None,  # no free source
) -> EntryRuleResult:
    price = _last_or_none(df_alt["close"])
    ema50_series, ema50 = _ema_last(df_alt["close"], 50)
    alt_btc_close = _last_or_none(df_alt_btc["close"])
    alt_btc_ma20 = _last_or_none(df_alt_btc["close"].rolling(20).mean())

    checks = [
        RuleCheck(
            "btc_bullish_regime_and_dominance_not_rising",
            (regime_score > 60 and btc_d_trend in ("falling", "flat"))
            if regime_score is not None and btc_d_trend is not None
            else None,
        ),
        RuleCheck(
            "price_above_rising_50ema",
            (price > ema50 and is_sloping_up(ema50_series)) if price is not None and ema50 is not None else None,
        ),
        RuleCheck(
            "alt_btc_pair_above_20ma",
            (alt_btc_close > alt_btc_ma20) if alt_btc_close is not None and alt_btc_ma20 is not None else None,
        ),
        _check("exchange_outflow_accumulation", exchange_outflow, lambda v: v is True),
    ]
    return EntryRuleResult("alt", _verdict_from_checks(checks), checks, ALT_SL_PCT, ALT_TP_PLAN, None)
