"""
Tests for bot/knowledge.py — the advisory corpus confluence layer.
No network: every corpus is written to a tmp_path.

Run directly (`python tests/test_knowledge.py`) or under pytest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.knowledge import KnowledgeIndex, build_index, score_signal


def _corpus(path: Path, documents) -> Path:
    path.write_text(json.dumps({"documents": documents, "saved_at": "2026-01-01", "version": 1}))
    return path


def _doc(concepts, status="ok"):
    return {
        "transcript_status": status,
        "concepts": [{"key": k, "maps_to": m, "count": c} for k, m, c in concepts],
    }


def _simple(tmp_path: Path) -> KnowledgeIndex:
    corpus = _corpus(tmp_path / "corpus.json", [
        _doc([("bos", "bot.smc.structure", 100), ("fvg", "bot.smc.fvg", 50)]),
        _doc([("bos", "bot.smc.structure", 100), ("ob", "bot.smc.order_blocks", 10)]),
    ])
    return build_index(corpus, tmp_path / "cache.json")


# --- index construction -----------------------------------------------------


def test_weights_are_relative_to_the_most_discussed_module(tmp_path):
    index = _simple(tmp_path)
    assert index.weight_for("bot.smc.structure") == 1.0        # 200, the peak
    assert index.weight_for("bot.smc.fvg") == 50 / 200
    assert index.weight_for("bot.smc.order_blocks") == 10 / 200


def test_untranscribed_documents_are_skipped(tmp_path):
    corpus = _corpus(tmp_path / "c.json", [
        _doc([("bos", "bot.smc.structure", 10)]),
        _doc([("fvg", "bot.smc.fvg", 999)], status="audio_unavailable"),
    ])
    index = build_index(corpus, tmp_path / "cache.json")
    assert index.document_count == 1
    assert index.weight_for("bot.smc.fvg") == 0.0


def test_concepts_with_no_code_counterpart_are_ignored(tmp_path):
    """maps_to is null for corpus concepts like tokenomics/inducement — there
    is no detector to score them against."""
    corpus = _corpus(tmp_path / "c.json", [
        _doc([("bos", "bot.smc.structure", 10), ("tokenomics", None, 900)]),
    ])
    index = build_index(corpus, tmp_path / "cache.json")
    assert index.weights == {"bot.smc.structure": 1.0}


def test_cache_is_reused_and_reproduces_the_same_weights(tmp_path):
    corpus = _corpus(tmp_path / "c.json", [_doc([("bos", "bot.smc.structure", 10)])])
    cache = tmp_path / "cache.json"
    first = build_index(corpus, cache)
    assert cache.exists()
    second = build_index(corpus, cache)
    assert first.weights == second.weights


def test_cache_is_rebuilt_when_the_corpus_changes(tmp_path):
    import os, time
    corpus = _corpus(tmp_path / "c.json", [_doc([("bos", "bot.smc.structure", 10)])])
    cache = tmp_path / "cache.json"
    build_index(corpus, cache)

    _corpus(corpus, [_doc([("bos", "bot.smc.structure", 10), ("fvg", "bot.smc.fvg", 5)])])
    os.utime(corpus, (time.time() + 10, time.time() + 10))  # force a new mtime
    rebuilt = build_index(corpus, cache)
    assert "bot.smc.fvg" in rebuilt.weights, "stale cache served after the corpus changed"


# --- failure modes must degrade to "no opinion", never to a 0% score --------


def test_missing_corpus_is_unavailable_not_zero(tmp_path):
    index = build_index(tmp_path / "nope.json", tmp_path / "cache.json")
    assert not index.available
    result = score_signal(("bot.smc.fvg",), index)
    assert result.available is False, (
        "a missing corpus must read as NO OPINION — available=True with 0% "
        "would silently penalise every setup"
    )


def test_malformed_corpus_does_not_raise(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json at all")
    assert not build_index(bad, tmp_path / "cache.json").available

    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text(json.dumps({"documents": "not a list"}))
    assert not build_index(wrong_shape, tmp_path / "cache.json").available


def test_corrupt_cache_falls_back_to_reparsing(tmp_path):
    corpus = _corpus(tmp_path / "c.json", [_doc([("bos", "bot.smc.structure", 10)])])
    cache = tmp_path / "cache.json"
    cache.write_text("{{{ corrupt")
    assert build_index(corpus, cache).available


# --- scoring contract -------------------------------------------------------


def test_no_detectors_is_no_opinion(tmp_path):
    assert score_signal((), _simple(tmp_path)).available is False


def test_detectors_with_no_corpus_coverage_score_zero_but_stay_available(tmp_path):
    result = score_signal(("bot.nonexistent",), _simple(tmp_path))
    assert result.available is True and result.knowledge_pct == 0.0


def test_score_never_leaves_the_zero_to_hundred_range(tmp_path):
    index = _simple(tmp_path)
    many = ("bot.smc.structure",) * 50
    assert 0.0 <= score_signal(many, index).knowledge_pct <= 100.0


def test_score_is_monotone_in_corroboration(tmp_path):
    """Adding a corroborating detector must never LOWER the score. A mean
    would violate this — and did, measurably, before the noisy-OR: a lone FVG
    setup outscored a five-way confluence. A confluence score that penalises
    confluence is worse than none."""
    index = _simple(tmp_path)
    one = score_signal(("bot.smc.order_blocks",), index).knowledge_pct
    two = score_signal(("bot.smc.order_blocks", "bot.smc.fvg"), index).knowledge_pct
    three = score_signal(("bot.smc.order_blocks", "bot.smc.fvg", "bot.smc.structure"), index).knowledge_pct
    assert one <= two <= three


def test_a_heavier_detector_outscores_a_lighter_one(tmp_path):
    index = _simple(tmp_path)
    heavy = score_signal(("bot.smc.structure",), index).knowledge_pct
    light = score_signal(("bot.smc.order_blocks",), index).knowledge_pct
    assert heavy > light


def test_no_single_detector_saturates_the_scale(tmp_path):
    """The top-weighted module has weight 1.0 by construction. Without damping
    every setup containing it would pin to exactly 100%."""
    index = _simple(tmp_path)
    assert score_signal(("bot.smc.structure",), index).knowledge_pct < 100.0


def test_result_reports_which_detectors_were_corpus_backed(tmp_path):
    result = score_signal(("bot.smc.structure", "bot.unknown"), _simple(tmp_path))
    assert result.matched == ("bot.smc.structure",)
    assert "1/2" in result.reason


# --- grade_book / grade_weight_pct: real win rate blended into corpus weight


class _FakeGrade:
    def __init__(self, win_rate):
        self.win_rate = win_rate


class _FakeGradeBook:
    """Stands in for bot.trade_grades.GradeBook — score_signal only ever
    calls grade_for(module), so that's all this needs to provide."""

    def __init__(self, grades: dict):
        self._grades = grades

    def grade_for(self, module):
        return self._grades.get(module)


def test_grade_weight_pct_zero_reproduces_prior_output_exactly(tmp_path):
    """The default. A grade_book being present must change nothing unless
    grade_weight_pct is actually raised above 0 -- this is the single most
    important property here, same as every other flag in this codebase."""
    index = _simple(tmp_path)
    detectors = ("bot.smc.structure", "bot.smc.fvg")
    baseline = score_signal(detectors, index)
    book = _FakeGradeBook({"bot.smc.structure": _FakeGrade(0.0)})  # would tank it if applied
    with_book_but_off = score_signal(detectors, index, grade_book=book, grade_weight_pct=0.0)
    assert with_book_but_off.knowledge_pct == baseline.knowledge_pct
    assert with_book_but_off.reason == baseline.reason


def test_no_grade_book_reproduces_prior_output_exactly(tmp_path):
    """grade_weight_pct alone, with no book to consult, must also be a no-op
    -- there's no performance data to blend in without a book."""
    index = _simple(tmp_path)
    detectors = ("bot.smc.structure",)
    baseline = score_signal(detectors, index)
    result = score_signal(detectors, index, grade_book=None, grade_weight_pct=1.0)
    assert result.knowledge_pct == baseline.knowledge_pct


def test_an_ungraded_detector_keeps_its_corpus_weight(tmp_path):
    """grade_for() returning None (below MIN_SAMPLE) must leave that
    detector's weight untouched -- "not enough data yet" is not "0% win
    rate", the same invariant trade_grades.py's own MIN_SAMPLE gate protects."""
    index = _simple(tmp_path)
    detectors = ("bot.smc.structure",)
    baseline = score_signal(detectors, index)
    book = _FakeGradeBook({})  # nothing graded
    result = score_signal(detectors, index, grade_book=book, grade_weight_pct=1.0)
    assert result.knowledge_pct == baseline.knowledge_pct
    assert "performance-weighted" not in result.reason


def test_a_high_win_rate_detector_scores_higher_than_pure_corpus(tmp_path):
    index = _simple(tmp_path)
    detectors = ("bot.smc.order_blocks",)  # a LOW corpus-weight module
    baseline = score_signal(detectors, index).knowledge_pct
    book = _FakeGradeBook({"bot.smc.order_blocks": _FakeGrade(1.0)})  # 100% real win rate
    boosted = score_signal(detectors, index, grade_book=book, grade_weight_pct=1.0).knowledge_pct
    assert boosted > baseline
    assert boosted <= 100.0


def test_a_low_win_rate_detector_scores_lower_than_pure_corpus(tmp_path):
    index = _simple(tmp_path)
    detectors = ("bot.smc.structure",)  # the HIGHEST corpus-weight module
    baseline = score_signal(detectors, index).knowledge_pct
    book = _FakeGradeBook({"bot.smc.structure": _FakeGrade(0.0)})  # 0% real win rate
    dampened = score_signal(detectors, index, grade_book=book, grade_weight_pct=1.0).knowledge_pct
    assert dampened < baseline
    assert dampened >= 0.0


def test_grade_weight_pct_is_clamped_to_zero_one(tmp_path):
    """A misconfigured value above 1 or below 0 must not blow past a pure
    performance read in either direction."""
    index = _simple(tmp_path)
    detectors = ("bot.smc.structure",)
    book = _FakeGradeBook({"bot.smc.structure": _FakeGrade(1.0)})
    at_one = score_signal(detectors, index, grade_book=book, grade_weight_pct=1.0).knowledge_pct
    above_one = score_signal(detectors, index, grade_book=book, grade_weight_pct=5.0).knowledge_pct
    assert above_one == at_one


def test_reason_names_how_many_detectors_were_performance_weighted(tmp_path):
    index = _simple(tmp_path)
    detectors = ("bot.smc.structure", "bot.smc.fvg")
    book = _FakeGradeBook({"bot.smc.structure": _FakeGrade(0.8)})  # only one graded
    result = score_signal(detectors, index, grade_book=book, grade_weight_pct=0.5)
    assert "1 performance-weighted" in result.reason


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
