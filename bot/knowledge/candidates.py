"""Ranked, cited rule candidates — the thing a human actually reads.

NOTHING IN THIS PACKAGE WRITES config.yaml. `accept` records a decision and
prints the edit; you make it yourself. That is the same boundary
bot/capital_guard.py draws for profit-lock and position-flush ("only ever
detects and reports them; a human acts on the report") and the same reason
bot/entry_rules.py sits outside the live execute path. An --apply flag is
deliberately absent: the moment it exists someone runs it, and unreviewed
YouTube transcripts start moving sizing thresholds on a real armed account.

`current` values are read off the live dataclasses at import time rather than
copied in as literals, so this table cannot silently drift away from the code
it claims to describe. A test asserts exactly that.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .store import Document, KnowledgeStore, assert_writable
from .transcripts import citation_url, format_timestamp

DEFAULT_PATH = Path("knowledge_candidates.json")
SCHEMA_VERSION = 1


def _screen_defaults() -> Dict[str, Any]:
    from bot.screening import ScreenConfig
    cfg = ScreenConfig()
    return {f: getattr(cfg, f) for f in ScreenConfig.__dataclass_fields__}


def _strategy_defaults() -> Dict[str, Any]:
    import inspect
    from bot.smc.strategy import SMCStrategy
    sig = inspect.signature(SMCStrategy.__init__)
    return {name: p.default for name, p in sig.parameters.items()
            if p.default is not inspect.Parameter.empty}


@dataclass(frozen=True)
class ParamTarget:
    name: str
    owner: str            # where it lives, for the printed edit
    config_path: str      # dotted path inside config.yaml
    current: Any
    lo: float
    hi: float


def _build_param_targets() -> Dict[str, ParamTarget]:
    s = _screen_defaults()
    t = _strategy_defaults()
    SC, ST = "bot.screening.ScreenConfig", "bot.smc.strategy.SMCStrategy"
    rows = [
        ("min_rr", SC, "screening.min_rr", s.get("min_rr"), 0.5, 10.0),
        ("min_confidence", SC, "screening.min_confidence", s.get("min_confidence"), 0.0, 1.0),
        ("sniper_confidence", SC, "screening.sniper_confidence", s.get("sniper_confidence"), 0.0, 1.0),
        ("max_stop_pct", SC, "screening.max_stop_pct", s.get("max_stop_pct"), 0.001, 1.0),
        ("ote_low", SC, "screening.ote_low", s.get("ote_low"), 0.1, 0.95),
        ("ote_high", SC, "screening.ote_high", s.get("ote_high"), 0.1, 0.99),
        ("swing_lookback", SC, "screening.swing_lookback", s.get("swing_lookback"), 2, 100),
        ("sweep_bars", SC, "screening.sweep_bars", s.get("sweep_bars"), 1, 200),
        ("liquidity_tolerance_pct", SC, "screening.liquidity_tolerance_pct",
         s.get("liquidity_tolerance_pct"), 0.00001, 0.05),
        ("reward_risk_ratio", ST, "reward_risk_ratio", t.get("reward_risk_ratio"), 0.5, 10.0),
        ("order_block_lookback", ST, "order_block_lookback", t.get("order_block_lookback"), 2, 200),
        ("fvg_min_size_pct", ST, "fvg_min_size_pct", t.get("fvg_min_size_pct"), 0.00001, 0.05),
    ]
    return {name: ParamTarget(name, owner, path, cur, lo, hi)
            for name, owner, path, cur, lo, hi in rows if cur is not None}


PARAM_TARGETS: Dict[str, ParamTarget] = _build_param_targets()

# Which taxonomy concept can propose which knob. A concept absent from here
# still produces candidates -- with param=None. Those are the feature-gap
# report, and for the candle concepts (nothing in this repo detects a candle)
# they are the whole point.
CONCEPT_PARAMS: Dict[str, List[str]] = {
    "risk_reward": ["min_rr", "reward_risk_ratio"],
    "fib": ["ote_low", "ote_high"],
    "premium_discount": ["ote_low"],
    "confluence": ["min_confidence", "sniper_confidence"],
    "stop_loss": ["max_stop_pct"],
    "swing": ["swing_lookback"],
    "sweep": ["sweep_bars"],
    "liquidity_pool": ["liquidity_tolerance_pct"],
    "order_block": ["order_block_lookback"],
    "fvg": ["fvg_min_size_pct"],
}


@dataclass
class Citation:
    video_id: str
    channel_name: str
    title: str
    start: float
    quote: str
    transcript_source: str = ""

    @property
    def url(self) -> str:
        return citation_url(self.video_id, self.start)

    def to_dict(self) -> dict:
        d = {"video_id": self.video_id, "channel_name": self.channel_name,
             "title": self.title, "start": self.start, "quote": self.quote,
             "transcript_source": self.transcript_source, "url": self.url}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Citation":
        return cls(str(d.get("video_id", "")), str(d.get("channel_name", "")),
                   str(d.get("title", "")), float(d.get("start", 0.0)),
                   str(d.get("quote", "")), str(d.get("transcript_source", "")))


@dataclass
class RuleCandidate:
    id: str
    concept_key: str
    statement: str
    param: Optional[str] = None
    config_path: Optional[str] = None
    owner: Optional[str] = None
    current_value: Any = None
    proposed_value: Optional[float] = None
    direction: str = "none"          # increase | decrease | set | none
    source: str = "deterministic"
    citations: List[Citation] = field(default_factory=list)
    support_videos: int = 0
    support_channels: int = 0
    mention_count: int = 0
    score: float = 0.0
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    status: str = "new"              # new | accepted | rejected | deferred
    reviewed_at: Optional[float] = None
    reviewer_note: str = ""
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    stale: bool = False

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["citations"] = [c.to_dict() for c in self.citations]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RuleCandidate":
        kwargs = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        kwargs["citations"] = [Citation.from_dict(c) for c in (d.get("citations") or [])]
        return cls(**kwargs)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", "", text.lower())).strip()


def candidate_id(concept_key: str, param: Optional[str], statement: str) -> str:
    raw = f"{concept_key}|{param or ''}|{_norm(statement)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _score(cand: RuleCandidate, total_channels: int) -> None:
    """Five weighted components, each in [0,1]. Breakdown is always kept.

    support counts DISTINCT VIDEOS, never mentions: an educator saying "order
    block" ninety times in one video is one opinion, not ninety.

    breadth divides by min(3, total_channels) rather than a fixed 3, or the
    component would be permanently dead for anyone who has confirmed a single
    channel -- which is the starting state for every user.

    deviation is last and lightest and hard-capped, because a huge proposed
    change is also the most likely extraction error. A garbage 100x value must
    not outrank a sane 2x one.
    """
    support = min(1.0, math.log1p(cand.support_videos) / math.log1p(10))
    breadth = (cand.support_channels / max(1, min(3, total_channels))
               if total_channels else 0.0)
    breadth = min(1.0, breadth)
    actionable = 1.0 if cand.param else 0.0
    if cand.proposed_value is not None:
        specificity = 1.0
    elif any(c.quote and re.search(r"\d", c.quote) for c in cand.citations):
        specificity = 0.3
    else:
        specificity = 0.0
    deviation = 0.0
    if cand.param and cand.proposed_value is not None:
        cur = PARAM_TARGETS[cand.param].current
        try:
            if cur:
                deviation = min(1.0, abs(float(cand.proposed_value) - float(cur)) / abs(float(cur)))
        except (TypeError, ValueError, ZeroDivisionError):
            deviation = 0.0
    parts = {"support": support, "breadth": breadth, "actionable": actionable,
             "specificity": specificity, "deviation": deviation}
    weights = {"support": 0.30, "breadth": 0.20, "actionable": 0.25,
               "specificity": 0.15, "deviation": 0.10}
    cand.score_breakdown = {k: round(v, 4) for k, v in parts.items()}
    cand.score = round(100.0 * sum(weights[k] * parts[k] for k in parts), 2)


def build_candidates(store: KnowledgeStore,
                     now: Optional[float] = None) -> List[RuleCandidate]:
    """Aggregate every ingested document into ranked candidates."""
    from . import extract, taxonomy

    now = time.time() if now is None else now
    docs: List[Document] = [d for d in store.documents() if d.ok]
    total_channels = len({d.channel_id for d in docs if d.channel_id})

    # (concept, param, value-bucket) -> accumulating candidate
    bucket: Dict[str, RuleCandidate] = {}
    # Evidence is counted here, NOT off cand.citations. Citations are capped
    # for display (nobody reads 50 quotes), and deriving support from them
    # silently capped every candidate's evidence at that same number -- so a
    # concept taught in 50 videos reported the same support as one taught in 8,
    # and the top of the ranking curve was unreachable. Counting is a separate
    # concern from quoting.
    seen_videos: Dict[str, set] = {}
    seen_channels: Dict[str, set] = {}

    for doc in docs:
        segments = store.segments(doc.video_id)
        if not segments:
            continue
        for hit in extract.extract_concepts(segments):
            definition = taxonomy.BY_KEY.get(hit.key)
            label = definition.label if definition else hit.key
            params = CONCEPT_PARAMS.get(hit.key) or [None]
            for param in params:
                target = PARAM_TARGETS.get(param) if param else None
                proposed = None
                if target and hit.numbers:
                    for _raw, val in hit.numbers:
                        if target.lo <= val <= target.hi:
                            proposed = float(val)
                            break
                if param and proposed is None:
                    # A knob with no quoted value in range is not actionable;
                    # fold it into the knob-less form rather than inventing one.
                    param, target = None, None
                statement = (
                    f"{label}: use {proposed:g} for {param}" if param and proposed is not None
                    else f"{label} is emphasised as part of the entry decision"
                )
                cid = candidate_id(hit.key, param, statement)
                cand = bucket.get(cid)
                if cand is None:
                    cand = RuleCandidate(
                        id=cid, concept_key=hit.key, statement=statement,
                        param=param,
                        config_path=target.config_path if target else None,
                        owner=target.owner if target else None,
                        current_value=target.current if target else None,
                        proposed_value=proposed,
                        direction=("none" if proposed is None or not target else
                                   "increase" if float(proposed) > float(target.current)
                                   else "decrease" if float(proposed) < float(target.current)
                                   else "set"),
                        first_seen_at=now, last_seen_at=now,
                    )
                    bucket[cid] = cand
                cand.mention_count += hit.count
                cand.last_seen_at = now
                seen_videos.setdefault(cid, set()).add(doc.video_id)
                seen_channels.setdefault(cid, set()).add(
                    doc.channel_id or doc.channel_name)
                if not any(c.video_id == doc.video_id and abs(c.start - hit.start) < 0.5
                           for c in cand.citations):
                    if len(cand.citations) < 8:
                        cand.citations.append(Citation(
                            video_id=doc.video_id, channel_name=doc.channel_name,
                            title=doc.title, start=hit.start, quote=hit.quote,
                            transcript_source=doc.transcript_source,
                        ))

    out = []
    for cand in bucket.values():
        cand.support_videos = len(seen_videos.get(cand.id, ()))
        cand.support_channels = len(seen_channels.get(cand.id, ()))
        if not cand.citations:
            continue                    # invariant: never emit an uncited claim
        _score(cand, total_channels)
        out.append(cand)
    out.sort(key=lambda c: (-c.score, -c.mention_count, c.id))
    return out


# --- persistence, preserving human decisions ------------------------------

def _write(cands: List[RuleCandidate], path: Path) -> None:
    path = assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(
        {"version": SCHEMA_VERSION, "saved_at": time.time(),
         "candidates": [c.to_dict() for c in cands]}, indent=2, sort_keys=True))
    os.replace(tmp, path)


def load(path: Path = DEFAULT_PATH) -> List[RuleCandidate]:
    try:
        raw = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    out = []
    for d in (raw.get("candidates") or []) if isinstance(raw, dict) else []:
        try:
            out.append(RuleCandidate.from_dict(d))
        except (TypeError, ValueError):
            continue
    return out


def merge(existing: List[RuleCandidate],
          fresh: List[RuleCandidate]) -> List[RuleCandidate]:
    """Refresh scores and citations while preserving every human decision.

    A candidate you already rejected must never reappear as "new" just because
    you re-ingested. Candidates that fall out of the corpus are marked stale
    rather than deleted -- deleting the record of a decision is worse than
    keeping a row that says "no longer supported".
    """
    by_id = {c.id: c for c in existing}
    fresh_ids = {c.id for c in fresh}
    out: List[RuleCandidate] = []
    for cand in fresh:
        prior = by_id.get(cand.id)
        if prior is not None:
            cand.status = prior.status
            cand.reviewer_note = prior.reviewer_note
            cand.reviewed_at = prior.reviewed_at
            cand.first_seen_at = prior.first_seen_at or cand.first_seen_at
        cand.stale = False
        out.append(cand)
    for cand in existing:
        if cand.id not in fresh_ids:
            cand.stale = True
            out.append(cand)
    out.sort(key=lambda c: (c.stale, -c.score, c.id))
    return out


def save(cands: List[RuleCandidate], path: Path = DEFAULT_PATH) -> None:
    _write(cands, path)


def set_status(cid: str, status: str, note: str = "",
               path: Path = DEFAULT_PATH,
               now: Optional[float] = None) -> Optional[RuleCandidate]:
    cands = load(path)
    hit = None
    for c in cands:
        if c.id == cid:
            c.status = status
            c.reviewer_note = note or c.reviewer_note
            c.reviewed_at = time.time() if now is None else now
            hit = c
    if hit is not None:
        _write(cands, path)
    return hit


def format_edit(cand: RuleCandidate) -> str:
    """The edit the human must make themselves. This package will not make it."""
    if not cand.param or cand.proposed_value is None:
        return ("This candidate maps to no existing parameter — there is nothing\n"
                "to edit. It is a feature gap, not a tuning change.")
    section, _, key = cand.config_path.rpartition(".")
    indent = "    " if section else "  "
    head = f"  {section}:\n" if section else ""
    return (
        "Nothing in this pipeline writes config.yaml. If you agree, edit it yourself:\n\n"
        f"    config.yaml\n{head}{indent}{key}: {cand.proposed_value:g}"
        f"      # was {cand.current_value}\n\n"
        "Then run scripts/walk_forward_optimize.py against it before going live."
    )


def format_table(cands: List[RuleCandidate], limit: int = 30) -> str:
    lines = [f"  {'score':>6}  {'status':<9} {'concept':<16} {'param':<22} "
             f"{'change':<22} {'support':<9} statement"]
    for c in cands[:limit]:
        change = (f"{c.current_value} -> {c.proposed_value:g}"
                  if c.param and c.proposed_value is not None else "—")
        param = c.param or "(no knob)"
        lines.append(
            f"  {c.score:>6.1f}  {c.status:<9} {c.concept_key:<16} {param:<22} "
            f"{change:<22} {c.support_videos}v/{c.support_channels}c   {c.statement[:60]}")
    return "\n".join(lines)


def format_detail(cand: RuleCandidate) -> str:
    lines = [
        f"  id         {cand.id}",
        f"  concept    {cand.concept_key}",
        f"  statement  {cand.statement}",
        f"  param      {cand.param or '(none — no code implements this yet)'}",
    ]
    if cand.param:
        lines.append(f"  change     {cand.current_value} -> {cand.proposed_value:g} "
                     f"({cand.direction}) in {cand.owner}")
    lines += [
        f"  score      {cand.score}  {cand.score_breakdown}",
        f"  support    {cand.support_videos} videos / {cand.support_channels} channels,"
        f" {cand.mention_count} mentions",
        f"  status     {cand.status}" + (f"  ({cand.reviewer_note})" if cand.reviewer_note else ""),
        "  citations:",
    ]
    for c in cand.citations:
        lines.append(f"    [{format_timestamp(c.start)}] {c.title[:52]} "
                     f"({c.transcript_source})")
        lines.append(f"      {c.url}")
        lines.append(f"      \"{c.quote[:150]}\"")
    return "\n".join(lines)
