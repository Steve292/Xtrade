"""
Regulatory/legislative news signal — built at the user's explicit request to
reflect "present day... market news like the CLARITY Act and possibilities
of the news pushing a buy."

This is a CRUDE keyword heuristic, not real NLP sentiment analysis: it flags
headlines that mention a curated set of regulatory/legislative topics
(CLARITY Act, GENIUS Act, SEC, CFTC, ETF, Congress, ...), then classifies
each one bullish- or bearish-tilted only if it also contains an explicit
directional verb (approved/cleared/passed = bullish-tilted; sued/warned/
blocked/banned = bearish-tilted). A topic mention with no directional verb
is reported as relevant but NEUTRAL — it is never guessed into a direction.

This matters in practice: verified live while building this, CoinTelegraph's
top headline was "New York AG warns CLARITY Act could weaken state crypto
enforcement" — a naive "mentions CLARITY Act -> bullish" rule would have
gotten this backwards (it's an AG raising a concern, not a positive
development). The verb-based check classifies it correctly: "warns" is a
bearish-tilted verb, so this heuristic reads it as bearish-tilted, not
bullish, even though the topic itself is a real regulatory-clarity subject
often associated with bullish news elsewhere.

Distinct from bot/smart_money.py's narrative_decay_signal(), which is about
social-media mention VOLUME (LunarCrush-style, still genuinely unavailable
for free) — this is about the actual CONTENT of real news headlines, a
different signal entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_TOPIC_KEYWORDS = (
    "clarity act",
    "genius act",
    "sec ",
    "cftc",
    " etf",
    "regulation",
    "regulatory",
    "regulator",
    "legislation",
    "lawmaker",
    "congress",
    "senate",
    "house of representatives",
)

_BULLISH_VERBS = (
    "approve",
    "approved",
    "approves",
    "clear",
    "cleared",
    "clears",
    "pass",
    "passed",
    "passes",
    "win",
    "wins",
    "won",
    "dismiss",
    "dismissed",
    "dismisses",
    "greenlight",
    "greenlights",
)

_BEARISH_VERBS = (
    "sue",
    "sues",
    "sued",
    "charge",
    "charges",
    "charged",
    "warn",
    "warns",
    "warned",
    "block",
    "blocks",
    "blocked",
    "reject",
    "rejects",
    "rejected",
    "ban",
    "bans",
    "banned",
    "crackdown",
    "delay",
    "delays",
    "delayed",
)


@dataclass
class NewsSignalResult:
    signal: str  # "BUY" | "SELL" | "NEUTRAL"
    bullish_count: int
    bearish_count: int
    relevant_headlines: list[dict] = field(default_factory=list)  # [{title, tilt}]


def _tilt(title_lower: str) -> str:
    bullish = any(v in title_lower for v in _BULLISH_VERBS)
    bearish = any(v in title_lower for v in _BEARISH_VERBS)
    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "neutral"  # both, or neither, present -- don't force a guess


def regulatory_news_signal(headlines: list[dict] | None) -> NewsSignalResult:
    """`headlines`: [{"title": ..., "link": ...}, ...] as returned by
    bot/marketdata.py's crypto_news_headlines(). None/empty -> NEUTRAL."""
    if not headlines:
        return NewsSignalResult("NEUTRAL", 0, 0, [])

    relevant = []
    bullish_count = bearish_count = 0
    for h in headlines:
        title = h.get("title", "")
        title_lower = title.lower()
        if not any(kw in title_lower for kw in _TOPIC_KEYWORDS):
            continue
        tilt = _tilt(title_lower)
        if tilt == "bullish":
            bullish_count += 1
        elif tilt == "bearish":
            bearish_count += 1
        relevant.append({"title": title, "tilt": tilt})

    if bullish_count > bearish_count:
        signal = "BUY"
    elif bearish_count > bullish_count:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    return NewsSignalResult(signal, bullish_count, bearish_count, relevant)
