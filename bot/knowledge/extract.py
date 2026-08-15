"""Turn merged transcript segments into concept hits and quoted numbers.

Concept detection is deterministic and stays that way. taxonomy.match_terms is
reproducible, testable without a 9GB model, and already correct; handing that
job to an LLM would trade all three away for nothing. The optional model pass
(llm.py) only ever *enriches* a segment that already scored a concept here --
it never decides that a concept is present.

The numeric pass matters more than it looks. It is what lets the entire feature
work with llm.enabled: false, because "1:3" next to `risk_reward`, or "0.705"
next to `fib`, is already a rule candidate with a concrete proposed value -- no
model required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import taxonomy
from .store import Concept, Segment

QUOTE_MAX_CHARS = 240

# --- numeric patterns -----------------------------------------------------
#
# Each returns (raw_text, value). Ordering matters: ratios are tried before
# bare integers so "1:3" is not read as the number 1.

_RATIO_RE = re.compile(r"\b(\d{1,2})\s*(?::|\s+to\s+)\s*(\d{1,2})\b", re.I)
_R_MULT_RE = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s*R\b")
# The word boundary goes INSIDE the alternation, on "percent" only. A trailing
# \b after "%" can never match: "%" and the space after it are both non-word
# characters, so there is no boundary between them and every "1%" was silently
# dropped -- which would have killed all position-size and stop-loss candidates.
_PCT_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)", re.I)
_FIB_RE = re.compile(r"\b(0?\.\d{3}|\d{3})\b")
_BARS_RE = re.compile(r"\b(\d{1,3})\s*(?:bars?|candles?)\b", re.I)

# Money and years are the two things that most look like a parameter and never
# are. "$65,000" and "2024" would otherwise flood every candidate list.
_MONEY_RE = re.compile(r"[$€£]\s*\d")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_THOUSANDS_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")

_FIB_VALID = {0.236, 0.382, 0.5, 0.618, 0.705, 0.786, 0.886}

# Which numeric families are meaningful for which concept.
_NUMERIC_CONCEPTS = {
    "risk_reward": ("ratio", "r_mult"),
    "position_size": ("pct",),
    "stop_loss": ("pct",),
    "drawdown": ("pct",),
    "overtrading": ("bars",),
    "fib": ("fib",),
    "premium_discount": ("fib",),
    "sweep": ("bars",),
    "swing": ("bars",),
    "order_block": ("bars",),
    "supply_demand": ("bars",),
    "confluence": ("pct",),
}


def _masked(text: str) -> str:
    """Blank out spans that are never parameters, so patterns can't see them."""
    out = text
    for rx in (_MONEY_RE, _THOUSANDS_RE, _YEAR_RE):
        out = rx.sub(lambda m: " " * len(m.group(0)), out)
    return out


PROXIMITY_WINDOW = 60   # characters between the concept phrase and the number


def extract_numbers(text: str, concept_key: str,
                    near: Optional[List[Tuple[int, int]]] = None,
                    window: int = PROXIMITY_WINDOW) -> List[Tuple[str, float, str]]:
    """Numbers in `text` that could plausibly be a value for `concept_key`.

    Returns (raw_text, value, family) triples. The FAMILY is not decoration --
    it is the unit, and dropping it caused the worst bug this pipeline has
    produced. "risk 1% per trade" yields 1.0 from the pct family, and
    max_stop_pct is a FRACTION in this codebase (0.25 means 25%). Without the
    family tag, 1.0 landed inside max_stop_pct's 0.001-1.0 range, passed every
    validation, and surfaced as the single highest-scoring candidate in the
    whole review file: "set max_stop_pct to 1" -- a 100% stop loss, presented
    to a human as the most strongly evidenced recommendation available.

    Carrying the unit is what lets candidates.py convert instead of guess.
    """
    families = _NUMERIC_CONCEPTS.get(concept_key)
    if not families:
        return []
    scrubbed = _masked(text)
    found: List[Tuple[str, float, str, int]] = []   # + match position

    if "ratio" in families:
        for m in _RATIO_RE.finditer(scrubbed):
            risk, reward = float(m.group(1)), float(m.group(2))
            if risk > 0 and reward > 0:
                found.append((m.group(0), reward / risk, "ratio", m.start()))
    if "r_mult" in families:
        for m in _R_MULT_RE.finditer(scrubbed):
            found.append((m.group(0), float(m.group(1)), "ratio", m.start()))
    if "pct" in families:
        for m in _PCT_RE.finditer(scrubbed):
            found.append((m.group(0), float(m.group(1)), "percent", m.start()))
    if "bars" in families:
        for m in _BARS_RE.finditer(scrubbed):
            found.append((m.group(0), float(m.group(1)), "count", m.start()))
    if "fib" in families:
        for m in _FIB_RE.finditer(scrubbed):
            raw = m.group(1)
            val = float(raw) if raw.startswith("0") or "." in raw else float(raw) / 1000.0
            # Only accept recognised retracement levels. Without this, any
            # three-digit number in the sentence becomes a fib "suggestion".
            if any(abs(val - f) < 1e-6 for f in _FIB_VALID):
                found.append((raw, val, "fraction", m.start()))

    if near is not None:
        # PROXIMITY. A number is evidence for a concept only if it sits beside
        # that concept's words. Segment-level co-occurrence alone produced
        # "max_stop_pct -> 0.9" out of someone saying "I was 90% sure" in the
        # same half-minute as the phrase "stop loss". Distance is a crude
        # attribution signal, but it is enormously better than none.
        found = [f for f in found
                 if any(abs(f[3] - s) <= window or abs(f[3] - e) <= window
                        for s, e in near)]

    seen = set()
    unique = []
    for raw, val, fam, _pos in found:
        if (raw, val, fam) not in seen:
            seen.add((raw, val, fam))
            unique.append((raw, val, fam))
    return unique


# --- concept hits ---------------------------------------------------------

@dataclass
class ConceptHit:
    key: str
    segment_index: int
    start: float
    end: float
    count: int
    quote: str
    # (raw_text, value, family) -- family is the unit and must not be dropped.
    numbers: List[Tuple[str, float, str]] = field(default_factory=list)


def extract_concepts(segments: List[Segment]) -> List[ConceptHit]:
    hits: List[ConceptHit] = []
    for i, seg in enumerate(segments):
        spans = taxonomy.match_spans(seg.text)
        if not spans:
            continue
        matched = {k: len(v) for k, v in spans.items()}
        quote = seg.text if len(seg.text) <= QUOTE_MAX_CHARS else (
            seg.text[:QUOTE_MAX_CHARS].rsplit(" ", 1)[0] + "…"
        )
        for key, count in sorted(matched.items()):
            hits.append(ConceptHit(
                key=key, segment_index=i, start=seg.start, end=seg.end,
                count=count, quote=quote,
                numbers=extract_numbers(seg.text, key, near=spans.get(key)),
            ))
    return hits


def concept_summary(hits: List[ConceptHit]) -> List[Concept]:
    """Roll per-segment hits up into the per-document Concept records."""
    agg: Dict[str, Concept] = {}
    for h in hits:
        c = agg.get(h.key)
        if c is None:
            definition = taxonomy.BY_KEY.get(h.key)
            c = Concept(
                key=h.key,
                label=definition.label if definition else h.key,
                count=0,
                maps_to=definition.maps_to if definition else None,
            )
            agg[h.key] = c
        c.count += h.count
        if h.segment_index not in c.segment_indices:
            c.segment_indices.append(h.segment_index)
    return [agg[k] for k in sorted(agg)]


def segments_worth_llm(segments: List[Segment], hits: List[ConceptHit],
                       limit: int) -> List[int]:
    """Indices worth spending a model generation on.

    Prioritises segments that already scored a concept AND carry a number or an
    imperative -- those are where an actual rule lives. At ~3s per generation,
    sending all 40 segments of 50 videos is ~100 minutes; this keeps it to the
    fraction that could plausibly yield a value.
    """
    imperative = re.compile(
        r"\b(never|always|must|should|don'?t|do not|wait for|only|avoid|need to)\b", re.I)
    scored = {}
    for h in hits:
        seg = segments[h.segment_index] if h.segment_index < len(segments) else None
        if seg is None:
            continue
        weight = 2 if h.numbers else 0
        if imperative.search(seg.text):
            weight += 1
        if weight:
            scored[h.segment_index] = max(scored.get(h.segment_index, 0), weight)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [idx for idx, _ in ranked[:max(0, limit)]]
