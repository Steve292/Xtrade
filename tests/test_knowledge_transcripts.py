"""Tests for bot/knowledge/transcripts.py — VTT parsing and caption dedup.

No network, no yt-dlp, no model. Run directly
(`python tests/test_knowledge_transcripts.py`) or under pytest.

The dedup tests carry the weight here. Rule candidates are ranked by how many
distinct videos mention a concept, and concept counts come off this text, so if
the rolling auto-caption window is not collapsed correctly every count is
inflated 3-4x and the whole ranking measures caption mechanics instead of what
educators actually said.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.knowledge import transcripts
from bot.knowledge.store import Segment

# A manual (human) caption file: clean, punctuated, no repetition.
MANUAL_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
Wait for the candle close above the order block.

2
00:00:04.000 --> 00:00:08.000
Anything less than one to three risk reward, I skip.
"""

# A real-shaped YouTube auto-caption file: no punctuation, inline <c> and
# per-word timestamp tags, and the scrolling window that repeats the tail of
# each cue at the head of the next.
AUTO_VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000 align:start position:0%
the market is

00:00:03.000 --> 00:00:05.000 align:start position:0%
the market is<c> going</c><c> to</c>

00:00:05.000 --> 00:00:07.000 align:start position:0%
<00:00:05.100>going<00:00:05.400> to<00:00:05.900> sweep that low

00:00:07.000 --> 00:00:09.000 align:start position:0%
sweep that low and then reverse
"""


def test_parse_manual_vtt():
    segs = transcripts.parse_vtt(MANUAL_VTT)
    assert len(segs) == 2
    assert segs[0].start == 1.0 and segs[0].end == 4.0
    assert "order block" in segs[0].text
    assert "WEBVTT" not in " ".join(s.text for s in segs)


def test_parse_strips_inline_tags_and_cue_settings():
    segs = transcripts.parse_vtt(AUTO_VTT)
    joined = " ".join(s.text for s in segs)
    assert "<c>" not in joined and "</c>" not in joined
    assert "00:00:05.100" not in joined       # per-word timestamp tags gone
    assert "align:start" not in joined        # cue settings are not payload
    assert "Kind: captions" not in joined     # header block skipped


def test_dedup_collapses_the_rolling_window():
    segs = transcripts.dedup_segments(transcripts.parse_vtt(AUTO_VTT))
    text = " ".join(s.text for s in segs)
    # Each phrase must survive exactly once, in order.
    assert text == "the market is going to sweep that low and then reverse", text


def test_dedup_keeps_the_first_occurrence_timestamp():
    # "going to" is first spoken in the cue starting at 3.0 and repeated in the
    # cue at 5.0. A citation must point at 3.0 — the moment it was said — not
    # at the later cue where the scrolling window happened to repeat it.
    segs = transcripts.dedup_segments(transcripts.parse_vtt(AUTO_VTT))
    owner = [s for s in segs if "going" in s.text][0]
    assert owner.start == 3.0, f"expected first occurrence at 3.0, got {owner.start}"


def test_dedup_is_idempotent():
    once = transcripts.dedup_segments(transcripts.parse_vtt(AUTO_VTT))
    twice = transcripts.dedup_segments(once)
    assert [(s.start, s.text) for s in once] == [(s.start, s.text) for s in twice]


def test_exact_duplicate_cue_is_dropped():
    segs = [
        Segment(0.0, 2.0, "liquidity sweep into the order block"),
        Segment(2.0, 4.0, "liquidity sweep into the order block"),
    ]
    out = transcripts.dedup_segments(segs)
    assert len(out) == 1


def test_partial_overlap_loses_no_words_and_duplicates_none():
    segs = [
        Segment(0.0, 2.0, "one two three four"),
        Segment(2.0, 4.0, "three four five six"),
    ]
    out = transcripts.dedup_segments(segs)
    assert " ".join(s.text for s in out) == "one two three four five six"


def test_non_overlapping_cues_are_untouched():
    segs = [Segment(0.0, 2.0, "alpha beta"), Segment(2.0, 4.0, "gamma delta")]
    out = transcripts.dedup_segments(segs)
    assert [s.text for s in out] == ["alpha beta", "gamma delta"]


def test_merge_respects_both_bounds_and_never_splits_a_cue():
    segs = [Segment(float(i), float(i + 1), f"word{i}") for i in range(40)]
    merged = transcripts.merge_segments(segs, max_seconds=10.0, max_chars=10_000)
    assert len(merged) > 1
    for m in merged:
        assert m.end - m.start <= 10.0
    # Every original token survives exactly once, in order.
    assert " ".join(m.text for m in merged) == " ".join(s.text for s in segs)


def test_merge_splits_on_char_budget():
    segs = [Segment(float(i), float(i) + 0.1, "x" * 100) for i in range(6)]
    merged = transcripts.merge_segments(segs, max_seconds=1000.0, max_chars=250)
    assert len(merged) >= 3
    for m in merged:
        assert len(m.text) <= 250 + 100   # never splits a cue, so one may overhang


def test_malformed_vtt_returns_what_it_can():
    broken = """WEBVTT

garbage line with no timing

00:00:02.000 --> 00:00:04.000
a real cue survives
"""
    segs = transcripts.parse_vtt(broken)
    assert len(segs) == 1 and segs[0].text == "a real cue survives"


def test_empty_and_junk_input_never_raise():
    assert transcripts.parse_vtt("") == []
    assert transcripts.parse_vtt("not a vtt file at all") == []
    assert transcripts.dedup_segments([]) == []
    assert transcripts.merge_segments([]) == []


def test_timestamps_without_hours_parse():
    assert transcripts.parse_timestamp("02:03.500") == 123.5
    assert transcripts.parse_timestamp("01:02:03.500") == 3723.5
    assert transcripts.parse_timestamp("nonsense") is None


def test_citation_url_and_format():
    assert transcripts.citation_url("abc123", 93.4).endswith("watch?v=abc123&t=93s")
    assert transcripts.format_timestamp(93.4) == "1:33"
    assert transcripts.format_timestamp(3852) == "1:04:12"


def test_prepare_runs_the_whole_chain():
    segs = transcripts.prepare(AUTO_VTT)
    assert len(segs) >= 1
    joined = " ".join(s.text for s in segs)
    assert joined.count("sweep that low") == 1


def _run_all() -> bool:
    ok = True
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                ok = False
                print(f"  FAIL {name}: {exc}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
