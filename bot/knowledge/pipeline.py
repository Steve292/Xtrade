"""Orchestration: confirmed channels -> transcripts -> concepts -> candidates.

The gate lives here and nowhere else: ingest() iterates channels.list_confirmed()
and there is no parameter, flag or branch that accepts an arbitrary channel URL.

Everything is incremental. A video already in the corpus at the current
EXTRACTOR_VERSION is skipped before any network call; raw captions are cached so
`--reextract` rebuilds the whole corpus offline; and the store is saved after
every video, because a forty-video run that dies on video thirty-nine must not
throw away thirty-nine downloads.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import candidates as candidates_mod
from . import channels as channels_mod
from . import extract, transcripts, whisper, ytdlp
from .config import KnowledgeConfig
from .store import EXTRACTOR_VERSION, Document, KnowledgeStore


@dataclass
class IngestReport:
    considered: int = 0
    ingested: int = 0
    skipped_fresh: int = 0
    skipped_short: int = 0
    failed: List[str] = field(default_factory=list)
    no_captions: List[str] = field(default_factory=list)
    whisper_used: int = 0
    channels: int = 0
    aborted: str = ""

    def summary(self) -> str:
        return (f"channels={self.channels} considered={self.considered} "
                f"ingested={self.ingested} fresh={self.skipped_fresh} "
                f"short={self.skipped_short} no_captions={len(self.no_captions)} "
                f"whisper={self.whisper_used} failed={len(self.failed)}"
                + (f"\n  ABORTED: {self.aborted}" if self.aborted else ""))


def ingest(cfg: KnowledgeConfig,
           store: KnowledgeStore,
           ytdlp_path: str,
           channels_path: Path = channels_mod.DEFAULT_PATH,
           runner: ytdlp.Runner = ytdlp.default_runner,
           transcriber: Callable[..., Optional[list]] = whisper.transcribe,
           limit: Optional[int] = None,
           only_channel: Optional[str] = None,
           dry_run: bool = False,
           force: bool = False,
           log: Callable[[str], None] = print) -> IngestReport:
    report = IngestReport()
    store.load()
    cache_dir = Path(cfg.data_dir) / "raw"
    audio_dir = Path(cfg.data_dir) / "audio"

    confirmed = channels_mod.list_confirmed(channels_path, enabled_only=True)
    if only_channel:
        confirmed = [c for c in confirmed if c.channel_id == only_channel]
    report.channels = len(confirmed)
    if not confirmed:
        log("No confirmed channels. Nothing will be ingested.\n"
            "  python scripts/knowledge_ingest.py channels search \"<name>\"")
        return report

    whisper_budget = cfg.whisper.max_videos_per_run if cfg.whisper.enabled else 0
    consecutive_failures = 0

    for chan in confirmed:
        entries = ytdlp.list_channel_videos(
            chan.channel_url, cfg.max_videos_per_channel, ytdlp_path,
            runner=runner, timeout=cfg.request_timeout_sec)
        log(f"{chan.channel_name}: {len(entries)} videos listed")
        for entry in entries:
            if limit is not None and report.ingested >= limit:
                break
            vid = str(entry.get("id") or "")
            if not vid:
                continue
            report.considered += 1

            if not force and store.is_fresh(vid, EXTRACTOR_VERSION):
                report.skipped_fresh += 1
                continue

            duration = float(entry.get("duration") or 0.0)
            # Skip Shorts before downloading anything. Nobody teaches a rule
            # worth extracting in forty-five seconds.
            if duration and not (cfg.min_video_seconds <= duration <= cfg.max_video_seconds):
                report.skipped_short += 1
                continue

            title = str(entry.get("title") or "")
            if dry_run:
                log(f"  would ingest {vid}  {title[:60]}")
                continue

            doc = Document(
                video_id=vid, channel_id=chan.channel_id,
                channel_name=chan.channel_name, title=title,
                url=ytdlp.WATCH_URL.format(vid=vid), duration_sec=duration,
                upload_date=str(entry.get("upload_date") or ""),
                ingested_at=time.time(), extractor_version=EXTRACTOR_VERSION,
            )

            segments = []
            try:
                cap = ytdlp.fetch_captions(vid, cache_dir, ytdlp_path,
                                           runner=runner,
                                           timeout=cfg.request_timeout_sec)
                if cap.vtt_path is not None:
                    segments = transcripts.prepare(
                        cap.vtt_path.read_text(errors="replace"),
                        max_seconds=cfg.segment_seconds,
                        max_chars=cfg.segment_max_chars)
                    doc.transcript_source = cap.source
                elif cfg.whisper.enabled and whisper_budget > 0:
                    whisper_budget -= 1
                    audio = ytdlp.download_audio(vid, audio_dir, ytdlp_path,
                                                 runner=runner)
                    # These two failures are NOT the same thing and must not
                    # share a status. Reporting a failed audio download as
                    # "whisper_unavailable" hid a YouTube bot-block behind a
                    # message about a local dependency, and the run churned
                    # through a hundred videos before anyone could tell that
                    # Whisper was working fine and the DOWNLOADS were blocked.
                    if audio is None:
                        doc.transcript_status = "audio_unavailable"
                        raw = None
                    else:
                        raw = transcriber(audio, model=cfg.whisper.model,
                                          backend=cfg.whisper.backend,
                                          runner=runner)
                    if raw:
                        segments = transcripts.merge_segments(
                            list(raw), max_seconds=cfg.segment_seconds,
                            max_chars=cfg.segment_max_chars)
                        doc.transcript_source = "whisper"
                        report.whisper_used += 1
                    elif audio is not None:
                        doc.transcript_status = "whisper_unavailable"
                    if audio and not cfg.whisper.keep_audio:
                        try:
                            Path(audio).unlink()
                        except OSError:
                            pass
                else:
                    doc.transcript_status = "no_captions"
            except Exception as exc:          # one bad video must not abort the run
                doc.transcript_status = f"error:{type(exc).__name__}"
                doc.error = str(exc)[:200]

            if segments:
                doc.concepts = extract.concept_summary(extract.extract_concepts(segments))
                doc.transcript_status = "ok"
                report.ingested += 1
            elif doc.transcript_status == "no_captions":
                report.no_captions.append(vid)
            elif doc.transcript_status != "ok":
                report.failed.append(f"{vid}:{doc.transcript_status}")
            else:
                doc.transcript_status = "no_captions"
                report.no_captions.append(vid)

            store.upsert(doc, segments if segments else None)
            store.save()          # crash safety: never lose N-1 downloads

            # Circuit breaker. A long unbroken failure run is never "some bad
            # videos" -- it is one systemic cause (YouTube bot-check, network
            # down, expired binary), and continuing makes a rate-limit worse
            # while producing nothing. Observed for real: `seen` climbed from
            # 38 to 131 while `ok` stayed frozen at 28, because nothing was
            # watching for a streak. Stop and say why.
            if doc.ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= cfg.max_consecutive_failures:
                    report.aborted = (
                        f"{consecutive_failures} consecutive failures "
                        f"(last: {doc.transcript_status}) — stopping rather "
                        f"than hammering the source. If this is "
                        f"'audio_unavailable', YouTube is most likely "
                        f"bot-checking this IP; wait it out and re-run, the "
                        f"corpus is incremental."
                    )
                    log(f"ABORTED: {report.aborted}")
                    return report

    return report


def rebuild_candidates(store: KnowledgeStore,
                       path: Path = candidates_mod.DEFAULT_PATH,
                       now: Optional[float] = None) -> List:
    """Re-derive candidates from the stored corpus. Makes zero network calls."""
    store.load()
    fresh = candidates_mod.build_candidates(store, now=now)
    merged = candidates_mod.merge(candidates_mod.load(path), fresh)
    candidates_mod.save(merged, path)
    return merged
