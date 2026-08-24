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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
