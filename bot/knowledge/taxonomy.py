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
               ["breaker", "breaker block", "flip zone", "mitigation block"]),
    ConceptDef("mitigation", "mitigation", "zone",
               ["mitigation", "mitigated", "retest", "rebalance"]),
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
    ConceptDef("killzone", "kill zone", "timing",
               ["kill zone", "killzone", "london session", "new york session", "asian session",
                "london open", "ny open", "session open", "power hour"]),
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
# EVERY concept in this group has maps_to=None, and that is a finding, not an
# oversight: a repo-wide search for engulf/doji/hammer/pin bar/harami/marubozu/
# morning star/candlestick returns nothing. bot/indicators.py stops at
# ema/rsi/macd; bot/smc/* reasons about swings, zones and gaps, never about an
# individual candle's body-to-wick geometry.
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
                "engulfs", "engulfing bar"]),
    ConceptDef("pin_bar", "pin bar rejection", "candle",
               ["pin bar", "pinbar", "hammer", "inverted hammer", "shooting star",
                "hanging man", "rejection candle", "rejection wick", "long wick",
                "wick rejection", "spike"]),
    ConceptDef("doji", "doji indecision", "candle",
               ["doji", "dragonfly", "gravestone", "indecision candle", "indecision bar"]),
    ConceptDef("inside_bar", "inside bar", "candle",
               ["inside bar", "inside candle", "harami", "compression bar", "nr7"]),
    ConceptDef("outside_bar", "outside bar", "candle",
               ["outside bar", "outside candle", "expansion candle", "wide range bar"]),
    ConceptDef("marubozu", "marubozu full body", "candle",
               ["marubozu", "full body candle", "full bodied candle", "no wick candle"]),
    ConceptDef("star_pattern", "star reversal pattern", "candle",
               ["morning star", "evening star", "three white soldiers",
                "three black crows", "tweezer top", "tweezer bottom"]),
    ConceptDef("candle_close", "candle close confirmation", "candle",
               ["candle close", "body close", "closes above", "closes below",
                "close confirmation", "wait for the close", "body to wick",
                "candle body", "displacement candle"]),
]

CONCEPTS: List[ConceptDef] = (
    _STRUCTURE + _LIQUIDITY + _ZONES + _LOCATION + _TIMING + _RISK + _CANDLES
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
