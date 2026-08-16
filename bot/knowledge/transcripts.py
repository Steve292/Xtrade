"""WebVTT parsing, rolling-caption de-duplication, and segment merging.

Pure functions, stdlib only, no network -- so all of it is testable without
yt-dlp, a model, or a video.

THE ROLLING-CAPTION PROBLEM, which is the reason this module exists:

YouTube's automatic captions are not a transcript, they are a scrolling
teleprompter. Each cue repeats the tail of the previous cue and adds a few new
words, so raw text looks like:

    00:00:01.000 --> 00:00:03.000   the market is
    00:00:03.000 --> 00:00:05.000   the market is going to
    00:00:05.000 --> 00:00:07.000   is going to sweep that low

Concatenated naively, "the market is going to" appears three times. Every
concept in taxonomy.py would then be counted three or four times, and since
rule candidates are ranked by how often a concept appears, the ranking would be
measuring caption mechanics rather than what anyone actually said. Dropping
*identical* lines does not fix it, because almost every repeat is partial.

The fix is token-level overlap removal against a rolling tail of everything
already emitted: find the longest suffix of what we have that is also a prefix
of the incoming cue, and keep only what is genuinely new. The surviving words
keep the timestamp of the cue where they FIRST appear, not where they last
repeat -- that is what makes a citation link land on the moment the point was
made instead of two cues later.
"""

from __future__ import annotations

import html
import re
from typing import List, Optional

from .store import Segment

# "HH:MM:SS.mmm --> HH:MM:SS.mmm" with optional cue settings after it.
# Hours are optional: YouTube emits MM:SS.mmm for short videos.
_TIMING_RE = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)"
    r"\s*-->\s*"
    r"(?P<end>(?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)"
)
# <c>, </c>, <00:00:03.520>, <v Speaker Name> -- all noise for our purposes.
_TAG_RE = re.compile(r"<[^>]*>")
_SKIP_BLOCK_PREFIXES = ("WEBVTT", "NOTE", "STYLE", "REGION")

DEFAULT_MAX_OVERLAP_TOKENS = 40
DEFAULT_MERGE_SECONDS = 30.0
DEFAULT_MERGE_CHARS = 600


def parse_timestamp(value: str) -> Optional[float]:
    """'01:02:03.500' or '02:03.500' -> seconds. None if unparseable."""
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    else:
        h, (m, s) = 0.0, nums
    return h * 3600.0 + m * 60.0 + s


def format_timestamp(seconds: float) -> str:
    """Seconds -> '1:04:12' / '4:12', for human-readable citations."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def citation_url(video_id: str, start: float) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={max(0, int(start))}s"


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", line))).strip()


def parse_vtt(text: str) -> List[Segment]:
    """Parse WebVTT into cues. Never raises; returns what it can understand."""
    if not text:
        return []
    segments: List[Segment] = []
    # ﻿: yt-dlp's files are UTF-8 and can carry a BOM.
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if any(lines[0].upper().startswith(p) for p in _SKIP_BLOCK_PREFIXES):
            continue
        timing_idx = None
        for i, line in enumerate(lines):
            if "-->" in line and _TIMING_RE.match(line):
                timing_idx = i
                break
        if timing_idx is None:
            continue
        m = _TIMING_RE.match(lines[timing_idx])
        start = parse_timestamp(m.group("start"))
        end = parse_timestamp(m.group("end"))
        if start is None:
            continue
        payload = " ".join(_clean(ln) for ln in lines[timing_idx + 1:])
        payload = re.sub(r"\s+", " ", payload).strip()
        if payload:
            segments.append(Segment(start=start, end=end if end is not None else start,
                                    text=payload))
    return segments


def dedup_segments(segments: List[Segment],
                   max_overlap_tokens: int = DEFAULT_MAX_OVERLAP_TOKENS) -> List[Segment]:
    """Strip the rolling-window repetition out of auto-captions.

    Compares each incoming cue against a rolling tail of everything already
    emitted (not merely the previous cue), because YouTube's window can span
    several cues. Idempotent: dedup(dedup(x)) == dedup(x).
    """
    out: List[Segment] = []
    tail: List[str] = []
    for seg in segments:
        tokens = seg.text.split()
        if not tokens:
            continue
        # Longest k where the tail's last k tokens are the cue's first k.
        limit = min(max_overlap_tokens, len(tail), len(tokens))
        overlap = 0
        for k in range(limit, 0, -1):
            if [t.lower() for t in tail[-k:]] == [t.lower() for t in tokens[:k]]:
                overlap = k
                break
        fresh = tokens[overlap:]
        if not fresh:
            # Entirely contained in what we already have -- an exact duplicate
            # cue, or a window that advanced by zero words.
            continue
        out.append(Segment(start=seg.start, end=seg.end, text=" ".join(fresh)))
        tail.extend(fresh)
        if len(tail) > max_overlap_tokens:
            tail = tail[-max_overlap_tokens:]
    return out


def merge_segments(segments: List[Segment],
                   max_seconds: float = DEFAULT_MERGE_SECONDS,
                   max_chars: int = DEFAULT_MERGE_CHARS) -> List[Segment]:
    """Group cues into ~30s chunks, never splitting a cue.

    Concept matching and quoting both happen on these merged chunks. A raw
    2-second cue is too small to contain a whole rule and too fragmentary to
    quote back to a human in a review file.
    """
    out: List[Segment] = []
    cur: Optional[Segment] = None
    for seg in segments:
        if cur is None:
            cur = Segment(start=seg.start, end=seg.end, text=seg.text)
            continue
        would_span = seg.end - cur.start
        would_len = len(cur.text) + 1 + len(seg.text)
        if would_span > max_seconds or would_len > max_chars:
            out.append(cur)
            cur = Segment(start=seg.start, end=seg.end, text=seg.text)
        else:
            cur = Segment(start=cur.start, end=seg.end, text=f"{cur.text} {seg.text}")
    if cur is not None:
        out.append(cur)
    return out


def prepare(vtt_text: str,
            max_seconds: float = DEFAULT_MERGE_SECONDS,
            max_chars: int = DEFAULT_MERGE_CHARS) -> List[Segment]:
    """parse -> dedup -> merge, the order everything downstream assumes."""
    return merge_segments(dedup_segments(parse_vtt(vtt_text)),
                          max_seconds=max_seconds, max_chars=max_chars)
