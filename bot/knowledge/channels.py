"""Which channels are allowed to be ingested, and how one gets on that list.

The gate is structural, not a prompt. pipeline.ingest() iterates
list_confirmed() and nothing else; there is no flag and no argument anywhere
that accepts a raw channel URL. You cannot bypass it by typing a URL at the
ingest script, because the ingest script has no such option. A y/N prompt that
a --yes flag skips would not be a gate -- this is.

Finding a channel: yt-dlp has no dependable channel-search extractor, but
`ytsearchN:` for videos is well supported and every flat-playlist entry carries
channel/channel_id/channel_url. So we search for VIDEOS matching the name and
aggregate the hits by channel. "Which channel actually publishes videos matching
this phrase" is both answerable that way and closer to the real question than a
name-similarity match would be.

The record lives at the repo root next to live_state.json and
pending_trades.json rather than under knowledge/, because it is a human
authorization decision, not bulk data. The set of already-seen videos is
deliberately NOT kept here -- that is the corpus's job, keyed by video id.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import ytdlp
from .store import assert_writable

DEFAULT_PATH = Path("knowledge_channels.json")
SCHEMA_VERSION = 1


@dataclass
class ChannelMatch:
    channel_id: str
    channel_name: str
    channel_url: str
    video_count: int = 0          # how many of the top-N video hits it owns
    total_views: int = 0
    sample_titles: List[str] = field(default_factory=list)
    sample_video_ids: List[str] = field(default_factory=list)


@dataclass
class ConfirmedChannel:
    channel_id: str
    channel_name: str
    channel_url: str
    query: str = ""
    confirmed_at: float = 0.0
    enabled: bool = True
    last_ingest_at: Optional[float] = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "channel_url": self.channel_url,
            "query": self.query,
            "confirmed_at": self.confirmed_at,
            "enabled": self.enabled,
            "last_ingest_at": self.last_ingest_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConfirmedChannel":
        return cls(
            channel_id=str(d.get("channel_id", "")),
            channel_name=str(d.get("channel_name", "")),
            channel_url=str(d.get("channel_url", "")),
            query=str(d.get("query", "")),
            confirmed_at=float(d.get("confirmed_at", 0.0)),
            enabled=bool(d.get("enabled", True)),
            last_ingest_at=d.get("last_ingest_at"),
            note=str(d.get("note", "")),
        )


# --- persistence (same conventions as bot/pending_trades.py) --------------

def _write(channels: List[ConfirmedChannel], path: Path) -> None:
    path = assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(
        {"version": SCHEMA_VERSION, "channels": [c.to_dict() for c in channels]},
        indent=2, sort_keys=True))
    os.replace(tmp, path)


def _read(path: Path) -> List[ConfirmedChannel]:
    try:
        raw = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return []                       # fail safe: nothing is authorized
    out = []
    for d in (raw.get("channels") or []) if isinstance(raw, dict) else []:
        if isinstance(d, dict) and d.get("channel_id"):
            out.append(ConfirmedChannel.from_dict(d))
    return out


def list_confirmed(path: Path = DEFAULT_PATH,
                   enabled_only: bool = False) -> List[ConfirmedChannel]:
    chans = _read(path)
    return [c for c in chans if c.enabled] if enabled_only else chans


def get_confirmed(channel_id: str, path: Path = DEFAULT_PATH) -> Optional[ConfirmedChannel]:
    for c in _read(path):
        if c.channel_id == channel_id:
            return c
    return None


def is_confirmed(channel_id: str, path: Path = DEFAULT_PATH) -> bool:
    c = get_confirmed(channel_id, path)
    return bool(c and c.enabled)


def confirm(channel_id: str, channel_name: str, channel_url: str,
            query: str = "", path: Path = DEFAULT_PATH,
            now: Optional[float] = None, note: str = "") -> ConfirmedChannel:
    chans = [c for c in _read(path) if c.channel_id != channel_id]
    rec = ConfirmedChannel(
        channel_id=channel_id, channel_name=channel_name, channel_url=channel_url,
        query=query, confirmed_at=time.time() if now is None else now,
        enabled=True, note=note,
    )
    chans.append(rec)
    _write(chans, path)
    return rec


def confirm_match(match: ChannelMatch, query: str = "",
                  path: Path = DEFAULT_PATH,
                  now: Optional[float] = None) -> ConfirmedChannel:
    return confirm(match.channel_id, match.channel_name, match.channel_url,
                   query=query, path=path, now=now)


def set_enabled(channel_id: str, enabled: bool, path: Path = DEFAULT_PATH) -> bool:
    chans = _read(path)
    hit = False
    for c in chans:
        if c.channel_id == channel_id:
            c.enabled, hit = enabled, True
    if hit:
        _write(chans, path)
    return hit


def revoke(channel_id: str, path: Path = DEFAULT_PATH) -> bool:
    chans = _read(path)
    remaining = [c for c in chans if c.channel_id != channel_id]
    if len(remaining) == len(chans):
        return False
    _write(remaining, path)
    return True


def mark_ingested(channel_id: str, path: Path = DEFAULT_PATH,
                  now: Optional[float] = None) -> None:
    chans = _read(path)
    for c in chans:
        if c.channel_id == channel_id:
            c.last_ingest_at = time.time() if now is None else now
    _write(chans, path)


# --- discovery ------------------------------------------------------------

def search_channels(query: str, limit: int, ytdlp_path: str,
                    runner: ytdlp.Runner = ytdlp.default_runner,
                    timeout: float = 120.0) -> List[ChannelMatch]:
    entries = ytdlp.search_videos(query, limit, ytdlp_path, runner=runner,
                                  timeout=timeout)
    by_id: Dict[str, ChannelMatch] = {}
    for e in entries:
        cid = e.get("channel_id") or e.get("uploader_id") or ""
        if not cid:
            continue
        m = by_id.get(cid)
        if m is None:
            url = (e.get("channel_url") or e.get("uploader_url")
                   or f"https://www.youtube.com/channel/{cid}")
            m = ChannelMatch(cid, e.get("channel") or e.get("uploader") or "", url)
            by_id[cid] = m
        m.video_count += 1
        try:
            m.total_views += int(e.get("view_count") or 0)
        except (TypeError, ValueError):
            pass
        if len(m.sample_titles) < 3 and e.get("title"):
            m.sample_titles.append(str(e["title"]))
            if e.get("id"):
                m.sample_video_ids.append(str(e["id"]))
    return sorted(by_id.values(), key=lambda m: (-m.video_count, -m.total_views))


def is_ambiguous(matches: List[ChannelMatch]) -> bool:
    """True when refusing to guess is the correct behaviour.

    A one-word name like "Lars" is not resolvable by better code, and quietly
    picking the top hit is how you end up with a corpus from the wrong person.
    """
    if not matches:
        return True
    if matches[0].video_count < 3:
        return True
    return len(matches) > 1 and (matches[0].video_count - matches[1].video_count) <= 1


def format_matches(query: str, matches: List[ChannelMatch]) -> str:
    lines = [f'Channels publishing videos that match "{query}":', ""]
    if not matches:
        lines.append("  (nothing found — try a longer, more specific phrase)")
        return "\n".join(lines)
    lines.append(f"  {'#':>2}  {'channel':<32} {'hits':>5}  {'views':>12}  example")
    for i, m in enumerate(matches[:10], 1):
        example = (m.sample_titles[0][:44] + "…") if m.sample_titles else ""
        lines.append(f"  {i:>2}  {m.channel_name[:32]:<32} {m.video_count:>5}  "
                     f"{m.total_views:>12,}  {example}")
        lines.append(f"      {m.channel_id}  {m.channel_url}")
    lines.append("")
    if is_ambiguous(matches):
        lines.append("  AMBIGUOUS — no clear winner. Refine the query or pass an")
        lines.append("  explicit --channel-id. Nothing will be ingested from a guess.")
        lines.append("")
    lines.append("  Nothing has been ingested and nothing is confirmed yet.")
    return "\n".join(lines)
