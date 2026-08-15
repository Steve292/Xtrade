"""The knowledge corpus: what has been ingested, and what was found in it.

Two-file layout, on purpose:

    knowledge/corpus.json            small index -- metadata + concept rollups
    knowledge/transcripts/<id>.json  the actual text, one file per video

Transcript text is deliberately NOT in the index. A single JSON file holding a
few hundred transcripts runs to hundreds of megabytes, and this repo's whole
read convention is `except (json.JSONDecodeError, OSError): return {}` -- a
fail-safe read is only cheap when the file is small. Keeping text out means
`has()` (called once per video on every run) stays a dict lookup against an
already-loaded index instead of re-parsing the corpus, and a corrupt transcript
for one video can never take down the index for all of them.

Writes go through the tmp-sibling + os.replace dance copied from
bot/pending_trades.py. Ingestion is long-running and the review CLI reads these
files while it runs, so a half-written corpus is a real failure mode, not a
theoretical one.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

DEFAULT_PATH = Path("knowledge/corpus.json")
SCHEMA_VERSION = 1

# Bumping this invalidates every stored extraction WITHOUT invalidating the
# downloaded transcripts, so `--reextract` can rebuild the whole corpus with
# zero network calls. Bump it whenever taxonomy.py or extract.py changes in a
# way that would produce different concepts from the same text.
EXTRACTOR_VERSION = 2


@dataclass
class Segment:
    """One merged span of transcript, with the timestamps a citation needs."""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            text=str(d.get("text", "")),
        )


@dataclass
class Concept:
    """A taxonomy concept as found in one document."""

    key: str
    label: str
    count: int
    maps_to: Optional[str] = None
    # Indices into that document's segment list. This is what lets a rule
    # candidate cite a timestamp instead of just naming the video.
    segment_indices: List[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "count": self.count,
            "maps_to": self.maps_to,
            "segment_indices": self.segment_indices,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Concept":
        return cls(
            key=str(d.get("key", "")),
            label=str(d.get("label", "")),
            count=int(d.get("count", 0)),
            maps_to=d.get("maps_to"),
            segment_indices=list(d.get("segment_indices") or []),
        )


@dataclass
class Document:
    """One ingested video."""

    video_id: str
    channel_id: str = ""
    channel_name: str = ""
    title: str = ""
    url: str = ""
    duration_sec: float = 0.0
    upload_date: str = ""          # "YYYYMMDD" as yt-dlp reports it, or ""
    # captions_manual | captions_auto | whisper | none. Recorded because ASR
    # mangles exactly the tokens this pipeline cares about -- "one to three"
    # for "1:3", "point six one eight" for 0.618 -- so a reviewer has to be
    # able to see how a quote was produced before trusting a number in it.
    transcript_source: str = "none"
    transcript_status: str = "ok"  # ok | no_captions | whisper_unavailable | error:<short>
    ingested_at: float = 0.0
    segment_count: int = 0
    word_count: int = 0
    concepts: List[Concept] = field(default_factory=list)
    extractor_version: int = EXTRACTOR_VERSION
    error: str = ""

    def to_dict(self) -> dict:
        d = {
            "video_id": self.video_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "title": self.title,
            "url": self.url,
            "duration_sec": self.duration_sec,
            "upload_date": self.upload_date,
            "transcript_source": self.transcript_source,
            "transcript_status": self.transcript_status,
            "ingested_at": self.ingested_at,
            "segment_count": self.segment_count,
            "word_count": self.word_count,
            "concepts": [c.to_dict() for c in self.concepts],
            "extractor_version": self.extractor_version,
        }
        if self.error:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Document":
        return cls(
            video_id=str(d.get("video_id", "")),
            channel_id=str(d.get("channel_id", "")),
            channel_name=str(d.get("channel_name", "")),
            title=str(d.get("title", "")),
            url=str(d.get("url", "")),
            duration_sec=float(d.get("duration_sec", 0.0)),
            upload_date=str(d.get("upload_date", "")),
            transcript_source=str(d.get("transcript_source", "none")),
            transcript_status=str(d.get("transcript_status", "ok")),
            ingested_at=float(d.get("ingested_at", 0.0)),
            segment_count=int(d.get("segment_count", 0)),
            word_count=int(d.get("word_count", 0)),
            concepts=[Concept.from_dict(c) for c in (d.get("concepts") or [])],
            extractor_version=int(d.get("extractor_version", 0)),
            error=str(d.get("error", "")),
        )

    @property
    def ok(self) -> bool:
        return self.transcript_status == "ok"


# Files this package must never write, at runtime, whatever it is asked to do.
# The report-only boundary is documented in three docstrings and pinned by
# tests/test_knowledge_boundary.py, but documentation and tests both describe
# intent -- this refuses. Every writer in bot/knowledge/ routes through
# assert_writable, so a future caller that passes config.yaml as a `path=`
# argument (the seam every function here exposes for testing) gets an exception
# instead of silently retuning a live, armed trading account.
FORBIDDEN_WRITE_NAMES = frozenset({"config.yaml", "config.yml", ".env", ".env.local"})


def _refuse(name: str) -> "PermissionError":
    return PermissionError(
        f"bot.knowledge refused to write {name}. This package reports rule "
        "candidates for review; it never edits trading configuration. Make the "
        "edit yourself (see candidates.format_edit)."
    )


def assert_writable(path: Path) -> Path:
    """Raise unless `path` is somewhere this package is allowed to write.

    Checks the symlink TARGET as well as the given name. A file called
    corpus.json that is a symlink to config.yaml passes a name-only check, and
    the only thing that stopped it clobbering config was that every writer here
    happens to use the tmp-file + os.replace dance (which replaces the link
    rather than following it). That is an accident of the atomic-write pattern,
    not a guarantee -- one future writer using a plain write_text() would go
    straight through. So resolve it and check both.
    """
    p = Path(path)
    if p.name.lower() in FORBIDDEN_WRITE_NAMES:
        raise _refuse(p.name)

    # Resolve inside the try, but raise OUTSIDE it. PermissionError subclasses
    # OSError, so raising in here and catching OSError for broken links would
    # swallow the refusal itself -- the guard would silently permit exactly the
    # case it exists to stop.
    target_name = ""
    try:
        if p.is_symlink():
            target_name = p.resolve().name.lower()
    except OSError:
        target_name = ""      # broken link: not a config file, let the write fail normally

    if target_name in FORBIDDEN_WRITE_NAMES:
        raise _refuse(f"{p.name} -> {target_name}")
    return p


def _write_json(payload: object, path: Path) -> None:
    """Atomic: write to a sibling temp file, then os.replace() it into place.

    Straight out of bot/pending_trades.py::_write. A plain write_text() can be
    observed half-written by the review CLI reading while ingestion runs.
    """
    path = assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        # Fail safe, never fail open -- same posture as bot/live_state.py. A
        # corrupt corpus means "nothing ingested yet", which costs a re-ingest;
        # raising here would take down the review CLI too.
        return fallback


class KnowledgeStore:
    def __init__(self, path: Path = DEFAULT_PATH,
                 transcripts_dir: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.transcripts_dir = (
            Path(transcripts_dir) if transcripts_dir is not None
            else self.path.parent / "transcripts"
        )
        self._docs: Dict[str, Document] = {}
        self._loaded = False

    # --- index -----------------------------------------------------------

    def load(self) -> "KnowledgeStore":
        raw = _read_json(self.path, {})
        docs = {}
        for d in (raw.get("documents") or []) if isinstance(raw, dict) else []:
            try:
                doc = Document.from_dict(d)
            except (TypeError, ValueError):
                continue          # one bad record must not lose the rest
            if doc.video_id:
                docs[doc.video_id] = doc
        self._docs = docs
        self._loaded = True
        return self

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        self._ensure()
        _write_json(
            {
                "version": SCHEMA_VERSION,
                "saved_at": time.time(),
                "documents": [d.to_dict() for d in self._docs.values()],
            },
            self.path,
        )

    # --- documents -------------------------------------------------------

    def get(self, video_id: str) -> Optional[Document]:
        self._ensure()
        return self._docs.get(video_id)

    def has(self, video_id: str) -> bool:
        self._ensure()
        return video_id in self._docs

    def is_fresh(self, video_id: str, extractor_version: int = EXTRACTOR_VERSION) -> bool:
        """True when this video needs no work at all.

        A failed video is never fresh: no_captions and error:* are usually
        transient (rate limits, a video that gained captions later), so they
        get retried on the next run rather than being written off forever.
        """
        doc = self.get(video_id)
        if doc is None or not doc.ok:
            return False
        return doc.extractor_version >= extractor_version

    def upsert(self, doc: Document, segments: Optional[Iterable[Segment]] = None) -> None:
        self._ensure()
        if segments is not None:
            segs = list(segments)
            doc.segment_count = len(segs)
            doc.word_count = sum(len(s.text.split()) for s in segs)
            _write_json(
                [s.to_dict() for s in segs],
                self.transcripts_dir / f"{doc.video_id}.json",
            )
        if not doc.ingested_at:
            doc.ingested_at = time.time()
        self._docs[doc.video_id] = doc

    def documents(self, channel_id: Optional[str] = None) -> List[Document]:
        self._ensure()
        docs = list(self._docs.values())
        if channel_id:
            docs = [d for d in docs if d.channel_id == channel_id]
        return sorted(docs, key=lambda d: (d.upload_date, d.video_id), reverse=True)

    def segments(self, video_id: str) -> List[Segment]:
        raw = _read_json(self.transcripts_dir / f"{video_id}.json", [])
        if not isinstance(raw, list):
            return []
        return [Segment.from_dict(s) for s in raw if isinstance(s, dict)]

    # --- rollups ---------------------------------------------------------

    def concept_counts(self, channel_id: Optional[str] = None) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for doc in self.documents(channel_id):
            if not doc.ok:
                continue
            for c in doc.concepts:
                counts[c.key] = counts.get(c.key, 0) + c.count
        return counts

    def documents_for_concept(self, key: str) -> List[Document]:
        return [d for d in self.documents()
                if d.ok and any(c.key == key for c in d.concepts)]

    def stats(self) -> dict:
        self._ensure()
        docs = list(self._docs.values())
        ok = [d for d in docs if d.ok]
        by_source: Dict[str, int] = {}
        for d in ok:
            by_source[d.transcript_source] = by_source.get(d.transcript_source, 0) + 1
        return {
            "documents": len(docs),
            "ok": len(ok),
            "failed": len(docs) - len(ok),
            "channels": len({d.channel_id for d in ok if d.channel_id}),
            "segments": sum(d.segment_count for d in ok),
            "words": sum(d.word_count for d in ok),
            "by_transcript_source": by_source,
        }
