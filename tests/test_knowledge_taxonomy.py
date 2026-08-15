"""Tests for bot/knowledge/taxonomy.py — the controlled vocabulary.

No network. Run directly (`python tests/test_knowledge_taxonomy.py`) or under
pytest.

The important test here is test_every_maps_to_is_importable. `maps_to` is the
only thing tying an ingested transcript concept back to code that could act on
it, and it is a dotted string, so nothing catches a typo at import time. Four of
these were wrong when the taxonomy was first written (`bot.smc.zones` and
`bot.smc.fib`, neither of which has ever existed), and the mistake is invisible
until someone reads a rule candidate pointing at a module that isn't there.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.knowledge import taxonomy


def test_every_maps_to_is_importable():
    broken = []
    for concept in taxonomy.CONCEPTS:
        if not concept.maps_to:
            continue
        try:
            if importlib.util.find_spec(concept.maps_to) is None:
                broken.append((concept.key, concept.maps_to))
        except (ImportError, ModuleNotFoundError, ValueError):
            broken.append((concept.key, concept.maps_to))
    assert not broken, f"maps_to points at modules that do not exist: {broken}"


def test_premium_discount_maps_to_structure_not_fibonacci():
    # Reads like fib territory, isn't. premium_discount_zone/is_in_premium/
    # is_in_discount are all defined in bot/smc/structure.py and imported from
    # there by bot/smc/strategy.py; fibonacci.py computes retracement levels and
    # the OTE band, and never touches premium/discount.
    assert taxonomy.BY_KEY["premium_discount"].maps_to == "bot.smc.structure"
    assert taxonomy.BY_KEY["fib"].maps_to == "bot.smc.fibonacci"


def test_order_block_and_supply_demand_are_separate_concepts():
    # bot/screening.py runs these as two independent gates. If one concept
    # owned both vocabularies, evidence for one would silently count as
    # evidence for the other in candidate ranking.
    assert taxonomy.BY_KEY["order_block"].maps_to == "bot.smc.order_blocks"
    assert taxonomy.BY_KEY["supply_demand"].maps_to == "bot.smc.supply_demand"
    hits = taxonomy.match_terms("price tapped the demand zone")
    assert hits == {"supply_demand": 1}


def test_keys_are_unique():
    keys = [c.key for c in taxonomy.CONCEPTS]
    assert len(keys) == len(set(keys)), "duplicate concept keys"


def test_no_alias_is_owned_by_two_concepts():
    # A shared alias makes match_terms' winner an accident of list order.
    owner = {}
    clashes = []
    for concept in taxonomy.CONCEPTS:
        for term in concept.all_terms():
            if term in owner and owner[term] != concept.key:
                clashes.append((term, owner[term], concept.key))
            owner[term] = concept.key
    assert not clashes, f"aliases claimed by two concepts: {clashes}"


def test_longest_alias_wins_and_spans_are_consumed():
    # "liquidity sweep" must score sweep ONCE and must not also score
    # liquidity_pool off the word "liquidity" sitting inside it.
    assert taxonomy.match_terms("a clean liquidity sweep") == {"sweep": 1}


def test_word_boundaries_are_respected():
    # "ob" is an order-block alias; it must not fire inside "problem".
    assert "order_block" not in taxonomy.match_terms("that is the problem here")
    assert taxonomy.match_terms("watch the ob") == {"order_block": 1}


def test_counts_accumulate_across_occurrences():
    hits = taxonomy.match_terms("fair value gap ... another fair value gap")
    assert hits["fvg"] == 2


def test_matching_is_case_insensitive():
    assert taxonomy.match_terms("BREAK OF STRUCTURE") == {"bos": 1}


def test_candle_patterns_are_present_and_unmapped():
    # The whole point of the candle group: the repo has no candlestick
    # detection at all, so these must surface as gaps rather than look wired.
    unmapped = set(taxonomy.unmapped_keys())
    for key in ("engulfing", "pin_bar", "doji", "inside_bar", "candle_close"):
        assert key in taxonomy.BY_KEY, f"missing candle concept {key}"
        assert key in unmapped, f"{key} claims a maps_to but no candle code exists"


def test_candle_aliases_match_real_phrasing():
    assert taxonomy.match_terms("a bullish engulfing off the low") == {"engulfing": 1}
    assert taxonomy.match_terms("big rejection wick there") == {"pin_bar": 1}
    hits = taxonomy.match_terms("wait for the close above")
    assert "candle_close" in hits


def test_empty_text_matches_nothing():
    assert taxonomy.match_terms("") == {}


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
