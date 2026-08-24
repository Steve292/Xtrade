"""
Knowledge confluence — scoring a live setup against the ingested corpus.

The knowledge daemon (scripts/knowledge_daemon.py) has been transcribing
trading material into /Users/mac/smc-knowledge/knowledge/corpus.json for some
time: ~1000 documents, each tagged with the SMC concepts it discusses and how
often. Until this module, nothing read it. The corpus was write-only.

What it is used for here, and what it is deliberately NOT used for:

  USED FOR  — a prevalence-weighted read of how well a live setup lines up
              with the concepts practitioners actually emphasise. A setup
              resting on the most-discussed concepts (swings, liquidity
              pools, FVGs) scores higher than one resting on fringe ones.

  NOT USED FOR — approving trades, vetoing trades, or setting parameters.
              evaluate_unified()'s `approved` never depends on this. The
              corpus is unvetted third-party commentary of unknown quality;
              treating it as an authority over real orders would be
              indefensible. It is a second opinion, weighted by consensus,
              and nothing more.

The join between corpus and code is already built: every corpus concept
carries a `maps_to` naming a bot module ("bot.smc.fvg"), and Signal.detectors
reports the modules that actually fired. Scoring is the overlap of those two
sets, weighted by corpus mention counts. No new taxonomy, no text matching,
no model in the loop.

Cost: the corpus is ~1000 documents and the trading loop polls every 30s, so
it is parsed once and reduced to a small module -> weight table, cached to
disk and keyed on the corpus's own `saved_at`. The daemon keeps appending;
a restart picks up its work without re-parsing on every pass.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CORPUS_PATH = Path("knowledge/corpus.json")
DEFAULT_CACHE_PATH = Path(".knowledge_weights.json")

# Documents whose audio never transcribed carry no usable concept data.
_SKIP_STATUSES = {"audio_unavailable"}

# Per-detector damping in the noisy-OR combination (see score_signal). At 0.5
# a single maximally-discussed detector reads ~50%, and a broad confluence
# approaches but never reaches 100% — leaving headroom that keeps the top of
# the scale meaningful instead of saturating on any setup touching structure.
_DAMPING = 0.5


@dataclass
class KnowledgeIndex:
    """Reduced corpus: module -> normalised weight in [0, 1]."""

    weights: dict = field(default_factory=dict)
    saved_at: str = ""
    document_count: int = 0

    @property
    def available(self) -> bool:
        return bool(self.weights)

    def weight_for(self, module: str) -> float:
        return float(self.weights.get(module, 0.0))


@dataclass
class KnowledgeResult:
    knowledge_pct: float  # 0-100, corpus-weighted coverage of this setup
    reason: str
    matched: tuple = ()
    available: bool = True


def _empty_result(reason: str) -> KnowledgeResult:
    """A no-corpus read is 0% with available=False. Callers MUST treat
    available=False as 'no opinion' and apply no adjustment — never as a 0%
    score, which would silently penalise every setup the moment the corpus
    file went missing."""
    return KnowledgeResult(0.0, reason, (), available=False)


def build_index(
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> KnowledgeIndex:
    """Load the module->weight table, from cache when the corpus hasn't moved.

    Any failure (missing file, unreadable JSON, unexpected shape) yields an
    empty index rather than raising: the trading loop must not die because a
    knowledge file is malformed.
    """
    corpus_path = Path(corpus_path)
    cache_path = Path(cache_path)

    if not corpus_path.exists():
        return KnowledgeIndex()

    try:
        stamp = str(os.path.getmtime(corpus_path))
    except OSError:
        stamp = ""

    # Cache hit: same corpus generation, no re-parse.
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("stamp") == stamp and cached.get("weights"):
                return KnowledgeIndex(
                    weights=cached["weights"],
                    saved_at=cached.get("saved_at", ""),
                    document_count=int(cached.get("document_count", 0)),
                )
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass  # fall through and rebuild

    try:
        data = json.loads(corpus_path.read_text())
    except (json.JSONDecodeError, OSError):
        return KnowledgeIndex()

    documents = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(documents, list):
        return KnowledgeIndex()

    totals: dict = {}
    used = 0
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        if doc.get("transcript_status") in _SKIP_STATUSES:
            continue
        concepts = doc.get("concepts") or []
        if not isinstance(concepts, list):
            continue
        used += 1
        for concept in concepts:
            if not isinstance(concept, dict):
                continue
            module = concept.get("maps_to")
            # maps_to is null for concepts with no code counterpart
            # (tokenomics, inducement) — nothing to score against.
            if not module:
                continue
            try:
                count = float(concept.get("count", 0) or 0)
            except (TypeError, ValueError):
                continue
            if count > 0:
                totals[module] = totals.get(module, 0.0) + count

    if not totals:
        return KnowledgeIndex()

    # Normalise against the single most-discussed module, so weights are
    # relative prevalence in [0, 1] and independent of corpus size — the
    # corpus grows continuously, and an absolute scale would drift.
    peak = max(totals.values())
    weights = {m: (c / peak) for m, c in totals.items()} if peak > 0 else {}

    index = KnowledgeIndex(
        weights=weights,
        saved_at=str(data.get("saved_at", "")),
        document_count=used,
    )

    try:
        cache_path.write_text(json.dumps({
            "stamp": stamp,
            "saved_at": index.saved_at,
            "document_count": index.document_count,
            "weights": weights,
        }))
    except OSError:
        pass  # cache is an optimisation; failing to write it is not an error

    return index


def score_signal(detectors, index: KnowledgeIndex) -> KnowledgeResult:
    """Corpus-weighted score for the detectors that produced a setup.

    Combined as a damped noisy-OR: ``1 - product(1 - weight * DAMPING)``.

    The obvious formulations are both wrong, and measurably so:

      - SUM is unbounded. It would climb past 100% the moment extended
        detectors were enabled, changing what the number means because an
        unrelated flag moved.
      - MEAN is bounded but not monotone, and inverts the thing being
        measured. Measured against the real corpus, a lone FVG setup scored
        55% while a five-way confluence of structure + smart money + supply/
        demand + order blocks scored 47%, because averaging drags every
        additional corroborating detector toward the mean. A confluence score
        that PENALISES confluence is worse than no score.

    Noisy-OR is bounded below 100, and strictly increasing in corroboration:
    adding a detector can never lower the score, and adding a heavily
    discussed one raises it more than a fringe one. DAMPING keeps any single
    detector from saturating the result — without it the top-weighted module
    (weight 1.0 by construction, since weights are normalised against the
    peak) would pin every setup containing it to exactly 100%.
    """
    if not index.available:
        return _empty_result("no knowledge corpus")
    if not detectors:
        return _empty_result("no detectors reported")

    matched = tuple(d for d in detectors if index.weight_for(d) > 0)
    if not matched:
        return KnowledgeResult(
            0.0, "no corpus coverage for these detectors", (), available=True
        )

    residual = 1.0
    for d in matched:
        residual *= 1.0 - index.weight_for(d) * _DAMPING
    pct = max(0.0, min(100.0, (1.0 - residual) * 100.0))
    top = sorted(matched, key=index.weight_for, reverse=True)[:3]
    short = ", ".join(m.rsplit(".", 1)[-1] for m in top)
    return KnowledgeResult(
        knowledge_pct=pct,
        reason=f"{len(matched)}/{len(detectors)} detectors corpus-backed ({short})",
        matched=matched,
        available=True,
    )
