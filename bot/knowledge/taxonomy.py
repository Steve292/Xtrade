"""The controlled vocabulary the corpus is indexed against.

Educators name the same handful of ideas a dozen different ways -- "order
block", "OB", "institutional candle" and "the last down candle before the move"
are one concept, and an index that treats them as four learns nothing. So every
concept here carries its aliases, and matching is alias-first.

The vocabulary is deliberately CLOSED. An open-ended keyword extractor over
trading video transcripts returns "market", "price" and "money" as top terms --
true, useless, and it buries the handful of terms that actually map onto
something bot/smc/ already computes. `MAPS_TO` records that mapping explicitly:
a concept with no mapping is knowledge we can read but cannot yet act on, and
knowing which is which is the entire point of extracting into a taxonomy rather
than a blob of text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ConceptDef:
    key: str
    label: str
    category: str
    aliases: List[str] = field(default_factory=list)
    # Dotted path to the thing in this repo that already implements the idea.
    # None means "we can read about it but nothing consumes it yet".
    maps_to: Optional[str] = None

    def all_terms(self) -> List[str]:
        return [self.label.lower()] + [a.lower() for a in self.aliases]


# --- structure -----------------------------------------------------------

_STRUCTURE = [
    ConceptDef("bos", "break of structure", "structure",
               ["bos", "structure break", "breaks structure", "market structure break", "msb"],
               maps_to="bot.smc.structure"),
    ConceptDef("choch", "change of character", "structure",
               ["choch", "chotch", "character change", "change in character"],
               maps_to="bot.smc.structure"),
    ConceptDef("swing", "swing high low", "structure",
               ["swing high", "swing low", "higher high", "lower low", "hh", "ll", "hl", "lh"],
               maps_to="bot.smc.structure"),
    ConceptDef("trend", "trend direction", "structure",
               ["uptrend", "downtrend", "ranging", "consolidation", "bullish structure",
                "bearish structure"],
               maps_to="bot.regime"),
]

# --- liquidity -----------------------------------------------------------

_LIQUIDITY = [
    ConceptDef("liquidity_pool", "liquidity pool", "liquidity",
               ["liquidity", "buy side liquidity", "sell side liquidity", "bsl", "ssl",
                "resting orders", "equal highs", "equal lows"],
               maps_to="bot.smart_money"),
    ConceptDef("sweep", "liquidity sweep", "liquidity",
               ["sweep", "stop hunt", "liquidity grab", "raid", "purge", "stop run",
                "sweeps liquidity", "grabs liquidity"],
               maps_to="bot.smart_money"),
    ConceptDef("inducement", "inducement", "liquidity",
               ["inducement", "ide", "bait", "trap", "engineered liquidity"]),
]

# --- zones ---------------------------------------------------------------

_ZONES = [
    # Order blocks and supply/demand zones are SEPARATE concepts, not synonyms.
    # bot/smc/supply_demand.py's own docstring draws the line: an order block
    # answers "which candle?" (the last opposing one before the impulse), a S/D
    # zone answers "which structural level?" (anchored to the swing extreme).
    # bot/screening.py runs them as two independent gates, so collapsing them
    # here would make a candidate for one look like evidence for the other.
    ConceptDef("order_block", "order block", "zone",
               ["order block", "ob", "orderblock", "institutional candle", "bullish ob",
                "bearish ob"],
               maps_to="bot.smc.order_blocks"),
    ConceptDef("supply_demand", "supply and demand zone", "zone",
               ["supply zone", "demand zone", "supply and demand", "s/d zone",
                "distribution zone", "accumulation zone"],
               maps_to="bot.smc.supply_demand"),
    ConceptDef("fvg", "fair value gap", "zone",
               ["fvg", "fair value gap", "imbalance", "inefficiency", "gap", "bisi", "sibi"],
               maps_to="bot.smc.fvg"),
    ConceptDef("breaker", "breaker block", "zone",
               ["breaker", "breaker block", "flip zone", "mitigation block"],
               maps_to="bot.smc.breaker"),
    ConceptDef("mitigation", "mitigation", "zone",
               ["mitigation", "mitigated", "retest", "rebalance"],
               maps_to="bot.smc.mitigation"),
]

# --- location / pricing --------------------------------------------------

_LOCATION = [
    # premium/discount lives in structure.py, NOT fibonacci.py -- it is derived
    # from the swing range (premium_discount_zone/is_in_premium/is_in_discount),
    # and bot/smc/strategy.py imports all three from .structure. Nothing in
    # fibonacci.py computes it. The names sound like fib territory; they aren't.
    ConceptDef("premium_discount", "premium and discount", "location",
               ["premium", "discount", "equilibrium", "50% level", "mid range",
                "premium zone", "discount zone"],
               maps_to="bot.smc.structure"),
    ConceptDef("fib", "fibonacci retracement", "location",
               ["fibonacci", "fib", "retracement", "golden pocket", "0.618", "618",
                "0.705", "705", "ote", "optimal trade entry"],
               maps_to="bot.smc.fibonacci"),
    ConceptDef("htf_alignment", "higher timeframe alignment", "location",
               ["higher timeframe", "htf", "top down", "top-down", "multi timeframe",
                "mtf", "daily bias", "weekly bias"],
               maps_to="bot.unified_screen"),
]

# --- timing --------------------------------------------------------------

_TIMING = [
    # Previously listed as a gap. It is not: bot/smart_money.py::session_signal
    # already implements exactly this -- London/NY/Asia windows, the 13:00-16:00
    # UTC overlap as peak liquidity, and a 21:00-24:00 dead zone it refuses to
    # trade. It was simply never mapped, which had it reported as missing
    # alongside concepts that genuinely are.
    ConceptDef("killzone", "kill zone", "timing",
               ["kill zone", "killzone", "london session", "new york session", "asian session",
                "london open", "ny open", "session open", "power hour"],
               maps_to="bot.smart_money"),
    ConceptDef("news", "news event", "timing",
               ["news", "nfp", "cpi", "fomc", "high impact", "red folder", "economic calendar"],
               maps_to="bot.news_signal"),
]

# --- execution / risk ----------------------------------------------------

_RISK = [
    ConceptDef("risk_reward", "risk reward ratio", "risk",
               ["risk reward", "risk to reward", "rr", "r:r", "1:2", "1:3", "reward ratio"],
               maps_to="bot.risk"),
    ConceptDef("position_size", "position sizing", "risk",
               ["position size", "position sizing", "lot size", "risk per trade",
                "percent risk", "1% risk", "2% risk"],
               maps_to="bot.position_sizing"),
    ConceptDef("stop_loss", "stop loss placement", "risk",
               ["stop loss", "sl", "stop placement", "invalidation", "invalidation level"],
               maps_to="bot.risk"),
    ConceptDef("take_profit", "take profit", "risk",
               ["take profit", "tp", "target", "partials", "partial close", "break even",
                "trail stop", "trailing stop"],
               maps_to="bot.risk"),
    ConceptDef("confluence", "confluence", "risk",
               ["confluence", "confirmation", "a+ setup", "a plus setup", "high probability"],
               maps_to="bot.entry_rules"),
    ConceptDef("overtrading", "overtrading discipline", "risk",
               ["overtrading", "over trading", "revenge trading", "patience", "discipline",
                "wait for setup", "no setup", "sit on hands"],
               maps_to="bot.capital_guard"),
    ConceptDef("drawdown", "drawdown management", "risk",
               ["drawdown", "losing streak", "consecutive losses", "risk of ruin",
                "blown account", "capital preservation"],
               maps_to="bot.capital_guard"),
]

# --- candlestick patterns ------------------------------------------------
#
# This group used to be entirely unmapped, and that gap is what prompted
# bot/smc/candles.py: a repo-wide search for engulf/doji/hammer/pin bar/
# harami/marubozu returned nothing, so the seven-gate screen could not see a
# rejection wick or require a close beyond a level. They now map to real
# detectors. star_pattern stays unmapped -- multi-candle star and soldier
# formations are genuinely not implemented, and claiming otherwise would put
# a concept on the 'covered' side of review --unmapped that nothing covers.
#
# So the bot currently screens seven gates and none of them can see a rejection
# wick. Ingesting educator content about candles is therefore guaranteed to
# produce candidates the bot cannot act on -- which is exactly why they are
# listed. unmapped_keys() turns this group into the feature-gap report, and
# after an audit showing a 1.022 profit factor with 28 consecutive losses, "the
# entry trigger has no candle confirmation at all" is a gap worth seeing named.
_CANDLES = [
    ConceptDef("engulfing", "engulfing candle", "candle",
               ["engulfing", "engulfing candle", "bullish engulfing", "bearish engulfing",
                "engulfs", "engulfing bar"],
               maps_to="bot.smc.candles"),
    ConceptDef("pin_bar", "pin bar rejection", "candle",
               ["pin bar", "pinbar", "hammer", "inverted hammer", "shooting star",
                "hanging man", "rejection candle", "rejection wick", "long wick",
                "wick rejection", "spike"],
               maps_to="bot.smc.candles"),
    ConceptDef("doji", "doji indecision", "candle",
               ["doji", "dragonfly", "gravestone", "indecision candle", "indecision bar"],
               maps_to="bot.smc.candles"),
    ConceptDef("inside_bar", "inside bar", "candle",
               ["inside bar", "inside candle", "harami", "compression bar", "nr7"],
               maps_to="bot.smc.candles"),
    ConceptDef("outside_bar", "outside bar", "candle",
               ["outside bar", "outside candle", "expansion candle", "wide range bar"],
               maps_to="bot.smc.candles"),
    ConceptDef("marubozu", "marubozu full body", "candle",
               ["marubozu", "full body candle", "full bodied candle", "no wick candle"],
               maps_to="bot.smc.candles"),
    ConceptDef("star_pattern", "star reversal pattern", "candle",
               ["morning star", "evening star", "three white soldiers",
                "three black crows", "tweezer top", "tweezer bottom"]),
    ConceptDef("candle_close", "candle close confirmation", "candle",
               ["candle close", "body close", "closes above", "closes below",
                "close confirmation", "wait for the close", "body to wick",
                "candle body", "displacement candle"],
               maps_to="bot.smc.candles"),
]

# --- classical indicators -------------------------------------------------
#
# SMC material is usually framed as an alternative to indicator trading, but
# educators reach for them constantly anyway -- for confirmation, for
# volatility, for divergence. Ignoring that vocabulary meant every such passage
# scored nothing and the corpus looked as though indicators were never
# discussed.
#
# The split here is the useful part. rsi/macd/ma/atr/cvd already exist in this
# repo (bot/indicators.py, bot/position_sizing.py::atr,
# bot/smart_money.py::cvd_signal), so mentions of them are TUNING signal. vwap,
# bollinger, stochastic, adx, obv and volume profile do not exist at all, so
# mentions of those are FEATURE signal. Same taxonomy, two very different kinds
# of finding, and only the maps_to field distinguishes them.
_INDICATORS = [
    ConceptDef("rsi", "relative strength index", "indicator",
               ["rsi", "relative strength", "overbought", "oversold"],
               maps_to="bot.indicators"),
    ConceptDef("macd", "macd", "indicator",
               ["macd", "moving average convergence", "signal line cross",
                "macd cross", "histogram"],
               maps_to="bot.indicators"),
    ConceptDef("moving_average", "moving average", "indicator",
               ["moving average", "ema", "sma", "wma", "exponential moving average",
                "simple moving average", "50 ma", "200 ma", "golden cross",
                "death cross", "ma cross"],
               maps_to="bot.indicators"),
    ConceptDef("atr", "average true range", "indicator",
               ["atr", "average true range", "true range", "volatility stop"],
               maps_to="bot.position_sizing"),
    # "order flow" was an alias here and accounted for 101 of the matches. It
    # is generic SMC vocabulary for reading institutional activity, NOT
    # cumulative volume delta -- keeping it would have reported "CVD discussed
    # 195 times, already implemented" about a corpus that barely mentions CVD.
    ConceptDef("cvd", "cumulative volume delta", "indicator",
               ["cvd", "cumulative volume delta", "delta divergence", "footprint chart"],
               maps_to="bot.smart_money"),
    # --- not implemented anywhere in this repo ---
    ConceptDef("vwap", "vwap", "indicator",
               ["vwap", "volume weighted average", "anchored vwap", "avwap"]),
    # "bands" and "squeeze" were aliases here and were almost entirely false
    # positives: 44 of 48 hits across the corpus were the ordinary English verb
    # ("squeeze through the supply zone", "squeeze back up"), which inflated
    # this to 135 mentions across 59 videos and made an indicator nobody uses
    # look like a major gap. Only unambiguous names survive.
    ConceptDef("bollinger", "bollinger bands", "indicator",
               ["bollinger", "bollinger band", "keltner channel"]),
    ConceptDef("stochastic", "stochastic oscillator", "indicator",
               ["stochastic", "stoch", "stoch rsi", "%k", "%d"]),
    ConceptDef("adx", "average directional index", "indicator",
               ["adx", "directional index", "dmi", "trend strength indicator"]),
    ConceptDef("volume_profile", "volume profile", "indicator",
               ["volume profile", "poc", "point of control", "value area",
                "high volume node", "low volume node", "hvn", "lvn"],
               maps_to="bot.smc.volume_profile"),
    ConceptDef("divergence", "indicator divergence", "indicator",
               ["bullish divergence", "bearish divergence", "hidden divergence",
                "rsi divergence", "regular divergence"]),
]

# --- Wyckoff, and wicks ---------------------------------------------------
#
# Two ideas that sound alike out loud and are completely unrelated, so both are
# named explicitly rather than left to collide.
#
# WYCKOFF is the older framework SMC descends from -- what ICT calls "smart
# money", Wyckoff called the composite operator. Accumulation and distribution
# phases, springs, upthrusts, climaxes. Nothing in this repo models any of it.
#
# WICK is the candle's high/low excursion beyond its body, and this one IS
# implemented: bot/smc/candles.py computes upper_wick/lower_wick and reads
# rejection through detect_pin_bar. Kept separate from pin_bar because a wick
# is the raw geometry and a pin bar is one interpretation of it -- transcripts
# say "it wicked into the zone and left" far more often than they name a
# pattern, and folding the two together would attribute plain wick language to
# a pattern that was never claimed.
#
# Alias hygiene: bare "spring" is deliberately NOT a Wyckoff alias. It is an
# ordinary English word, and the same mistake with "squeeze" gave bollinger 135
# phantom mentions across 59 videos. Only "wyckoff spring" is unambiguous.
_WYCKOFF = [
    ConceptDef("wyckoff", "wyckoff method", "wyckoff",
               ["wyckoff", "composite man", "composite operator",
                "wyckoff accumulation", "wyckoff distribution",
                "accumulation phase", "distribution phase", "wyckoff spring",
                "upthrust", "utad", "sign of strength", "sign of weakness",
                "selling climax", "buying climax", "automatic rally",
                "secondary test", "re-accumulation", "redistribution"],
               maps_to="bot.smc.wyckoff"),
    ConceptDef("wick", "candle wick", "candle",
               ["wick", "wicks", "wicked", "candle wick", "upper wick",
                "lower wick", "wick through", "wicked into", "wick fill",
                "wicked below", "wicked above"],
               maps_to="bot.smc.candles"),
]

CONCEPTS: List[ConceptDef] = (
    _STRUCTURE + _LIQUIDITY + _ZONES + _LOCATION + _TIMING + _RISK
    + _CANDLES + _INDICATORS + _WYCKOFF
)

BY_KEY: Dict[str, ConceptDef] = {c.key: c for c in CONCEPTS}

# Longest-first so "liquidity sweep" wins over bare "liquidity", and so a
# two-word alias is never shadowed by one of its own halves.
_TERM_INDEX = sorted(
    ((term, c.key) for c in CONCEPTS for term in c.all_terms()),
    key=lambda kv: -len(kv[0]),
)


def match_spans(text: str) -> Dict[str, List[tuple]]:
    """Concept key -> list of (start, end) character spans in `text`.

    Positions matter, not just counts: a number is only evidence for a concept
    if it sits NEAR that concept's words. Without spans, extract.py could only
    ask "did this 30-second segment mention stop loss, and did it contain a
    percentage" -- which turned "I was 90% sure" into a proposed 90% stop loss.
    Co-occurrence in a segment is not attribution.

    Consumes matched spans so overlapping aliases can't double-count: a
    transcript saying "liquidity sweep" scores `sweep` once, not `sweep` plus
    `liquidity_pool`.
    """
    lowered = text.lower()
    consumed = bytearray(len(lowered))
    spans: Dict[str, List[tuple]] = {}
    for term, key in _TERM_INDEX:
        start = 0
        while True:
            i = lowered.find(term, start)
            if i < 0:
                break
            end = i + len(term)
            if not any(consumed[i:end]):
                # Word-boundary check; "ob" must not match inside "problem".
                before_ok = i == 0 or not lowered[i - 1].isalnum()
                after_ok = end >= len(lowered) or not lowered[end].isalnum()
                if before_ok and after_ok:
                    for j in range(i, end):
                        consumed[j] = 1
                    spans.setdefault(key, []).append((i, end))
            start = end
    return spans


def match_terms(text: str) -> Dict[str, int]:
    """Concept key -> hit count for `text`."""
    return {k: len(v) for k, v in match_spans(text).items()}


def unmapped_keys() -> List[str]:
    """Concepts with nothing in this repo consuming them yet."""
    return [c.key for c in CONCEPTS if not c.maps_to]
