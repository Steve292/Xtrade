"""
Shared regime/hotness/smart-money snapshot computation — the ONE place this
logic lives, so webapp/server.py's dashboard and the read-only MCP server
(mcp_server/server.py) compute it identically instead of drifting apart or
independently double-hitting the same free-tier rate limits (Yahoo,
CoinGecko, Deribit) from two separate processes.

Read-only end to end: every call here queries a venue or a free data
provider, nothing places, closes, or modifies an order. See bot/regime.py,
bot/hotness.py, bot/position_sizing.py, and bot/smart_money.py for the
per-module docstrings on what's real data vs. a documented approximation vs.
genuinely unavailable without a paid key this project doesn't have.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from bot import entry_rules
from bot import hotness as hotness_mod
from bot import marketdata
from bot import news_signal
from bot import position_sizing
from bot import regime as regime_mod
from bot import smart_money
from bot import timeseries
from bot.hyperliquid.client import HyperliquidClient
from bot.marketdata import candles_with_binance_fallback
from bot.mt5.client import MT5Client
from bot.smc.strategy import SMCStrategy
from bot.wallet import DefiWallet

ROOT = Path(__file__).resolve().parents[1]
DOMINANCE_STATE_PATH = ROOT / "dominance_history.json"


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f) or {}


_cache: dict = {"data": None, "fetched_at": 0.0}


def get_cached_snapshot(cfg: dict | None = None, ttl_seconds: float = 900.0) -> dict:
    """Same compute_snapshot(), cached in-process for `ttl_seconds` — for
    callers that poll every ~30s (bot/runner.py, hypertrade.py, via
    bot/unified_screen.py) and would otherwise hit Yahoo/CoinGecko/Deribit
    far more than their free tiers tolerate. A failed refresh falls back to
    the last good cached value (never silently blocks live trading on a
    transient data-provider hiccup) — it only raises if the very first call
    ever fails with nothing cached yet to fall back on."""
    now = time.time()
    if _cache["data"] is None or now - _cache["fetched_at"] > ttl_seconds:
        try:
            _cache["data"] = compute_snapshot(cfg)
            _cache["fetched_at"] = now
        except Exception as e:
            if _cache["data"] is None:
                raise
            # A refresh failure with something cached to fall back on was
            # previously silent -- this feeds the smart-money vote real
            # trades are scored against, so a refresh that has been failing
            # for hours had no trace at all beyond an ever-staler read no one
            # could see was stale. Age is what actually matters here, not
            # just the fact that it happened once.
            age_min = (now - _cache["fetched_at"]) / 60
            print(f"  [snapshot] refresh failed ({type(e).__name__}: {str(e)[:120]}) "
                  f"— serving cache, now {age_min:.0f}min stale")
    return _cache["data"]


def _hl_client(cfg: dict) -> HyperliquidClient:
    hl_cfg = cfg.get("hyperliquid", {})
    wallet = DefiWallet.from_env() or DefiWallet.load()
    return HyperliquidClient.connect(
        private_key=wallet.private_key if wallet else "",
        testnet=hl_cfg.get("testnet", True),
    )


def _mt5_client() -> MT5Client:
    return MT5Client.connect(
        host=os.getenv("MT5_HOST", "127.0.0.1"),
        port=os.getenv("MT5_PORT", "18812"),
        login=os.getenv("MT5_LOGIN", ""),
        password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""),
        terminal_path=os.getenv("MT5_TERMINAL_PATH", ""),
    )


def _analyze_mt5_symbol(
    mt5_client: MT5Client, symbol: str, ltf: str, htf: str,
    strategy: SMCStrategy, regime_label: str, hotness_multiplier: float,
) -> dict:
    """Runs only the smart-money modules that genuinely generalize to any
    instrument's own candles (CVD proxy, liquidation-heatmap proxy, SMC+Fib,
    session timing) — NOT the full Section 4 set. GEX (needs BTC options
    data specifically), stablecoin flow/SSR, narrative decay, and divergence
    are crypto-dominance concepts with no forex/commodity equivalent, and
    Section 5's entry rules (majors/meme/altcoin) all key off funding rate,
    SSR, meme score, or BTC.D — none of which exist for an MT5 CFD. Forcing
    any of those onto EURUSD would be fabricated, not a real signal, so
    they're just skipped here rather than faked.

    Sizing: gold (XAU) maps to the blueprint's "commodity" base-risk bucket;
    every other MT5 symbol here is a forex pair, and the blueprint never
    defines a base-risk bucket for forex at all — sizing is left None
    (documented gap) rather than inventing a number the spec doesn't give."""
    df = mt5_client.copy_rates(symbol, ltf, count=200)
    htf_df = mt5_client.copy_rates(symbol, htf, count=100)
    smc_signal_type = strategy.analyze(df, htf_df).type.value

    modules = {
        "cvd": smart_money.cvd_signal(df),
        "liquidation_heatmap": smart_money.liquidation_heatmap_signal(df),
        "smc_fib": smart_money.smc_fib_signal(smc_signal_type),
        "session": smart_money.session_signal(),
    }
    sm_result = smart_money.aggregate_smart_money(modules)

    asset_class = "commodity" if symbol.upper().startswith("XAU") else None
    sizing = None
    if asset_class:
        vol_adjust = 1.0
        if len(df) > 100:
            atr20 = position_sizing.atr(df, period=20).dropna()
            atr100 = position_sizing.atr(df, period=100).dropna()
            if len(atr20) and len(atr100):
                vol_adjust = position_sizing.volatility_adjust(float(atr20.iloc[-1]), float(atr100.mean()))
        factors = position_sizing.SizingFactors(
            base_risk_pct=position_sizing.asset_class_base_risk_pct(asset_class),
            regime_alloc_weight=position_sizing.regime_alloc_weight(regime_label),
            hotness_multiplier=hotness_multiplier,
            volatility_adjust=vol_adjust,
        )
        sizing = {"asset_class": asset_class, "final_risk_pct": position_sizing.final_risk_pct(factors)}

    return {
        "symbol": symbol,
        "smart_money": {
            "direction": sm_result.direction,
            "bullish_count": sm_result.bullish_count,
            "bearish_count": sm_result.bearish_count,
            "multiplier": sm_result.multiplier,
            "modules": modules,
        },
        "suggested_sizing": sizing,
    }


def compute_snapshot(cfg: dict | None = None) -> dict:
    """One real pass over every free data source this project has access to
    (checked: no Glassnode/CoinGlass/LunarCrush/ETF-flow key exists anywhere
    in .env.example) plus Hyperliquid's own client. Every fetch degrades to
    None independently on failure — the regime/hotness/meme-score/smart-money
    functions all already handle partial data by redistributing weight or
    abstaining, rather than guessing, so one flaky provider never blocks the
    rest of the snapshot."""
    cfg = cfg or load_config()

    vix = marketdata.yahoo_level("^VIX")
    dxy_24h = marketdata.yahoo_change_pct("DX-Y.NYB", bars_back=1, interval="1d", range_="5d")
    yield_10y_4h = marketdata.yahoo_change_pct("^TNX", bars_back=4, interval="60m", range_="5d")
    yield_10y_level = marketdata.yahoo_level("^TNX")
    yield_3m_level = marketdata.yahoo_level("^IRX")
    yield_curve = (
        yield_10y_level - yield_3m_level
        if yield_10y_level is not None and yield_3m_level is not None
        else None
    )
    spx_24h = marketdata.yahoo_change_pct("^GSPC", bars_back=1, interval="1d", range_="5d")

    # Regulatory/legislative news signal (e.g. CLARITY Act coverage) — a
    # crude keyword heuristic over real, current headlines, not NLP
    # sentiment. See bot/news_signal.py's module docstring for exactly how
    # (and why) it avoids guessing a direction from a bare topic mention.
    news_headlines = marketdata.crypto_news_headlines(limit=30)
    news_result = news_signal.regulatory_news_signal(news_headlines)

    btc_24h = btc_price = funding_rate = None
    smc_signal_type = "none"
    hl_client = None
    try:
        hl_client = _hl_client(cfg)
        ticks = hl_client.watchlist_tickers(["BTC"])
        if ticks and "error" not in ticks[0]:
            btc_price = ticks[0].get("mid")
            btc_24h = ticks[0].get("change_24h_pct")
            funding_rate = ticks[0].get("funding_rate")
    except Exception as e:
        # Best-effort by design (this is one of several inputs to the
        # smart-money vote, and the rest still compute without it) -- but
        # silent before this, which made "has this been failing for days"
        # unanswerable from the logs.
        print(f"  [snapshot] Hyperliquid BTC ticker unavailable "
              f"({type(e).__name__}: {str(e)[:120]})")

    # Each candle set is fetched independently, each with its own Binance
    # fallback (see bot.marketdata.candles_with_binance_fallback) -- a
    # rate-limit/connection blip on Hyperliquid for ONE of these must not
    # blank out the other three too, which the previous single try/except
    # around this whole block did.
    btc_candles = candles_with_binance_fallback(hl_client, "BTC", cfg.get("timeframe", "15m"), 48)
    htf_candles = candles_with_binance_fallback(hl_client, "BTC", cfg.get("higher_timeframe", "1h"), 200)
    # Separate from btc_candles/htf_candles above (15m/1h, tuned for the live
    # SMC screener) — Section 5's majors rule specifically wants Daily + 4H
    # confirmation, so it gets its own fetch.
    daily_candles = candles_with_binance_fallback(hl_client, "BTC", "1d", 24 * 220)
    four_h_candles = candles_with_binance_fallback(hl_client, "BTC", "4h", 24 * 20)

    if btc_candles is not None and htf_candles is not None:
        try:
            strategy = SMCStrategy(
                swing_lookback=cfg.get("swing_lookback", 5),
                order_block_lookback=cfg.get("order_block_lookback", 20),
                fvg_min_size_pct=cfg.get("fvg_min_size_pct", 0.001),
                liquidity_tolerance_pct=cfg.get("liquidity_tolerance_pct", 0.0005),
                reward_risk_ratio=cfg.get("reward_risk_ratio", 2.0),
                # extended_detectors/htf_neutral_credit matched to the live
                # loop: this signal.type feeds smc_fib_signal below, which
                # feeds the smart-money vote evaluate_unified scores real
                # trades against -- htf_neutral_credit in particular can
                # change signal.type (NONE -> LONG/SHORT) at the confluence
                # margin, not just confidence, so drift here would make the
                # "second opinion" disagree with the primary signal for
                # reasons that are really just config skew.
                extended_detectors=cfg.get("smc", {}).get("extended_detectors", False),
                extended_max_adjust=cfg.get("smc", {}).get("extended_max_adjust", 0.10),
                htf_neutral_credit=cfg.get("smc", {}).get("htf_neutral_credit", 0.0),
            )
            smc_signal_type = strategy.analyze(btc_candles, htf_candles).type.value
        except Exception as e:
            # Feeds smc_fib_signal -> the smart-money vote evaluate_unified
            # scores REAL trades against. A silent failure here meant the
            # vote defaulted to NEUTRAL with no indication why -- identical
            # to a genuine neutral read from the caller's side.
            print(f"  [snapshot] SMC+Fib signal computation failed "
                  f"({type(e).__name__}: {str(e)[:120]})")

    cg_global = marketdata.coingecko_global()
    stablecoin_cap = marketdata.coingecko_category_cap("stablecoins")
    meme_cap = marketdata.coingecko_category_cap("meme-token")
    meme_change_24h = marketdata.coingecko_category_change_24h("meme-token")
    meme_top10_return_7d = marketdata.coingecko_top_movers_avg_7d_pct("meme-token", top_n=10)

    btc_d = total_cap = stable_c = meme_share = others_d = ssr = None
    if cg_global:
        pct = cg_global["market_cap_percentage"]
        total_cap = cg_global["total_market_cap_usd"]
        btc_d = pct.get("btc")
        if total_cap and stablecoin_cap is not None:
            stable_c = stablecoin_cap / total_cap * 100
            if btc_d is not None and stablecoin_cap > 0:
                ssr = (total_cap * btc_d / 100) / stablecoin_cap
        if total_cap and meme_cap is not None:
            meme_share = meme_cap / total_cap * 100
        if btc_d is not None and stable_c is not None:
            others_d = max(0.0, 100 - btc_d - pct.get("eth", 0.0) - stable_c)

    now = time.time()
    for key, value in (("btc_d", btc_d), ("meme_share", meme_share), ("others_d", others_d), ("stable_c", stable_c)):
        if value is not None:
            timeseries.record_sample(DOMINANCE_STATE_PATH, key, value, now=now)

    dominance_trend = hotness_mod.DominanceTrend(
        btc_d=timeseries.trend(DOMINANCE_STATE_PATH, "btc_d", now=now),
        meme_share=timeseries.trend(DOMINANCE_STATE_PATH, "meme_share", now=now),
        others_d=timeseries.trend(DOMINANCE_STATE_PATH, "others_d", now=now),
        stable_c=timeseries.trend(DOMINANCE_STATE_PATH, "stable_c", now=now),
    )
    hotness_result = hotness_mod.detect_hotness(dominance_trend)

    meme_score_result = hotness_mod.meme_season_score(
        hotness_mod.MemeScoreInputs(
            meme_dominance_change_24h_pct=meme_change_24h,
            meme_top10_avg_return_7d_pct=meme_top10_return_7d,
            btc_dominance_pct=btc_d,
            others_dominance_change_24h_pct=None,  # no free source distinct from others_d's own level
            stablecoin_dominance_pct=stable_c,
        )
    )

    regime_result = regime_mod.score_regime(
        regime_mod.RegimeInputs(
            yield_10y_4h_change_pct=yield_10y_4h,
            dxy_24h_change_pct=dxy_24h,
            vix_level=vix,
            yield_curve_10y_3m=yield_curve,
            btc_24h_change_pct=btc_24h,
            etf_7d_net_flow_usd=None,  # no free source (Glassnode/ETF-flow provider needed)
            stablecoin_ssr=ssr,
            exchange_reserve_7d_change_pct=None,  # no free source (Glassnode/CryptoQuant needed)
        )
    )

    oi_by_strike = marketdata.deribit_option_oi_by_strike("BTC")
    modules = {
        "cvd": smart_money.cvd_signal(btc_candles) if btc_candles is not None else {"signal": "NEUTRAL", "strength": 0.0},
        "gex": smart_money.gex_signal(btc_price or 0.0, oi_by_strike),
        "stablecoin_flow": smart_money.stablecoin_flow_signal(ssr),
        "liquidation_heatmap": (
            smart_money.liquidation_heatmap_signal(btc_candles)
            if btc_candles is not None
            else {"signal": "NEUTRAL", "level": None, "kind": None}
        ),
        "narrative_decay": smart_money.narrative_decay_signal(),
        "divergence": smart_money.divergence_signal(btc_24h, spx_24h),
        "smc_fib": smart_money.smc_fib_signal(smc_signal_type),
        "session": smart_money.session_signal(),
        # A 9th input beyond the blueprint's original 8 — added at explicit
        # user request to let real regulatory/legislative news (e.g. CLARITY
        # Act coverage) count toward the vote, same BUY/SELL/NEUTRAL
        # vocabulary aggregate_smart_money() already expects.
        "regulatory_news": {"signal": news_result.signal},
    }
    smart_money_result = smart_money.aggregate_smart_money(modules)

    # Section 5 (majors entry rule) — BTC only, same convention as the smart-
    # money modules above. Uses the SAME cvd_signal() proxy as Section 4, but
    # fed hourly candles specifically ("CVD 1H" per the blueprint) rather
    # than the 15m series smart_money's own aggregate uses.
    cvd_1h = smart_money.cvd_signal(htf_candles)["signal"] if htf_candles is not None else None
    if daily_candles is not None and four_h_candles is not None:
        major_result = entry_rules.evaluate_major(
            daily_candles, four_h_candles,
            funding_rate=funding_rate, cvd_1h_signal=cvd_1h, ssr=ssr,
        )
    else:
        major_result = entry_rules.EntryRuleResult(
            "major", "INCOMPLETE", [], entry_rules.MAJOR_SL_PCT, entry_rules.MAJOR_TP_PLAN, entry_rules.MAJOR_TRAIL_ATR_MULT
        )

    # Section 6 (suggested sizing) — informational: NOT consumed anywhere in
    # bot/runner.py's live execute() path (see bot/position_sizing.py's
    # module docstring for why that's a deliberately separate step).
    vol_adjust = 1.0
    if btc_candles is not None and len(btc_candles) > 100:
        atr20 = position_sizing.atr(btc_candles, period=20).dropna()
        atr100 = position_sizing.atr(btc_candles, period=100).dropna()
        if len(atr20) and len(atr100):
            vol_adjust = position_sizing.volatility_adjust(float(atr20.iloc[-1]), float(atr100.mean()))
    sizing_factors = position_sizing.SizingFactors(
        base_risk_pct=position_sizing.asset_class_base_risk_pct("btc"),
        regime_alloc_weight=position_sizing.regime_alloc_weight(regime_result.regime),
        hotness_multiplier=hotness_result.multiplier,
        volatility_adjust=vol_adjust,
        confidence_multiplier=1.0,
    )

    # MT5 watchlist — run through the same modules that actually generalize
    # to a non-crypto instrument (see _analyze_mt5_symbol's docstring for
    # exactly what's skipped and why). One symbol's failure (bridge hiccup,
    # bad symbol name) never blocks the rest of the watchlist or the
    # crypto-side snapshot above.
    mt5_watchlist_analysis = []
    try:
        mt5_client = _mt5_client()
        mt5_ltf = cfg.get("mt5_timeframe", cfg.get("timeframe", "15m"))
        mt5_htf = cfg.get("higher_timeframe", "1h")
        mt5_strategy = SMCStrategy(
            swing_lookback=cfg.get("swing_lookback", 5),
            order_block_lookback=cfg.get("order_block_lookback", 20),
            fvg_min_size_pct=cfg.get("fvg_min_size_pct", 0.001),
            liquidity_tolerance_pct=cfg.get("liquidity_tolerance_pct", 0.0005),
            reward_risk_ratio=cfg.get("reward_risk_ratio", 2.0),
            extended_detectors=cfg.get("smc", {}).get("extended_detectors", False),
            extended_max_adjust=cfg.get("smc", {}).get("extended_max_adjust", 0.10),
            htf_neutral_credit=cfg.get("smc", {}).get("htf_neutral_credit", 0.0),
        )
        for symbol in cfg.get("mt5_watchlist") or []:
            try:
                mt5_watchlist_analysis.append(
                    _analyze_mt5_symbol(
                        mt5_client, symbol, mt5_ltf, mt5_htf, mt5_strategy,
                        regime_result.regime, hotness_result.multiplier,
                    )
                )
            except Exception as e:
                mt5_watchlist_analysis.append({"symbol": symbol, "error": f"{type(e).__name__}: {str(e)[:120]}"})
    except Exception as e:
        mt5_watchlist_analysis = [{"symbol": None, "error": f"MT5 unreachable: {type(e).__name__}: {str(e)[:120]}"}]

    return {
        "regime": {
            "score": regime_result.score,
            "label": regime_result.regime,
            "factors": regime_result.factors,
            "missing": regime_result.missing,
            "inputs": {
                "vix": vix,
                "dxy_24h_change_pct": dxy_24h,
                "yield_10y_4h_change_pct": yield_10y_4h,
                "yield_curve_10y_3m": yield_curve,
                "btc_24h_change_pct": btc_24h,
                "stablecoin_ssr": ssr,
            },
        },
        "hotness": {
            "signal": hotness_result.signal,
            "multiplier": hotness_result.multiplier,
            "confidence": hotness_result.confidence,
            "trend": {
                "btc_d": dominance_trend.btc_d,
                "meme_share": dominance_trend.meme_share,
                "others_d": dominance_trend.others_d,
                "stable_c": dominance_trend.stable_c,
            },
        },
        "meme_score": {
            "score": meme_score_result.score,
            "zone": meme_score_result.zone,
            "action": meme_score_result.action,
            "size_multiplier": meme_score_result.size_multiplier,
            "missing": meme_score_result.missing,
        },
        "smart_money": {
            "direction": smart_money_result.direction,
            "bullish_count": smart_money_result.bullish_count,
            "bearish_count": smart_money_result.bearish_count,
            "multiplier": smart_money_result.multiplier,
            "modules": modules,
        },
        "dominance": {"btc_d": btc_d, "meme_share": meme_share, "others_d": others_d, "stable_c": stable_c},
        "entry_rules_major": {
            "asset_class": major_result.asset_class,
            "verdict": major_result.verdict,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in major_result.checks],
            "stop_loss_pct": major_result.stop_loss_pct,
            "tp_plan": major_result.tp_plan,
            "trail_atr_mult": major_result.trail_atr_mult,
        },
        "suggested_sizing": {
            "asset_class": "btc",
            "base_risk_pct": sizing_factors.base_risk_pct,
            "regime_alloc_weight": sizing_factors.regime_alloc_weight,
            "hotness_multiplier": sizing_factors.hotness_multiplier,
            "volatility_adjust": sizing_factors.volatility_adjust,
            "final_risk_pct": position_sizing.final_risk_pct(sizing_factors),
        },
        "mt5_watchlist_analysis": mt5_watchlist_analysis,
        "regulatory_news": {
            "signal": news_result.signal,
            "bullish_count": news_result.bullish_count,
            "bearish_count": news_result.bearish_count,
            "relevant_headlines": news_result.relevant_headlines,
        },
        "fetched_at": now,
    }
