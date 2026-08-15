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


def test_candle_patterns_now_map_to_real_detectors():
    # This assertion is INVERTED from how it started. The candle group was
    # added entirely unmapped, and that gap -- 49 of 157 corpus videos using
    # candle-close confirmation against a repo with no candle code at all --
    # is what prompted bot/smc/candles.py. Now that the detectors exist, the
    # taxonomy must say so, or `review --unmapped` keeps reporting a gap that
    # has been closed.
    for key in ("engulfing", "pin_bar", "doji", "inside_bar", "outside_bar",
                "marubozu", "candle_close"):
        assert key in taxonomy.BY_KEY, f"missing candle concept {key}"
        assert taxonomy.BY_KEY[key].maps_to == "bot.smc.candles", key


def test_newly_built_zone_detectors_are_mapped():
    assert taxonomy.BY_KEY["mitigation"].maps_to == "bot.smc.mitigation"
    assert taxonomy.BY_KEY["breaker"].maps_to == "bot.smc.breaker"


def test_genuinely_unbuilt_concepts_stay_unmapped():
    """Honesty check: things with no detector must not claim one.

    Asserts MEMBERSHIP, not an exact set. The exact-set form broke twice for
    entirely correct reasons -- once when VWAP gained an implementation and
    once when the crypto-narrative group was added -- and a test that fails
    every time the taxonomy legitimately grows trains people to edit it without
    reading it, which is worse than not having it.
    """
    unmapped = set(taxonomy.unmapped_keys())
    # No detector exists for any of these.
    for key in ("inducement", "star_pattern", "bollinger", "stochastic", "adx",
                "divergence"):
        assert key in unmapped, f"{key} claims code that does not exist"
    # These are backed by real modules and must NOT appear as gaps.
    for key in ("mitigation", "breaker", "volume_profile", "vwap", "wyckoff",
                "killzone", "meme_season"):
        assert key not in unmapped, f"{key} is implemented but reported as a gap"


def test_stub_backed_concepts_are_not_treated_as_implemented():
    # bot/smart_money.py::narrative_decay_signal exists but always returns
    # {"available": False}. A concept mapping to it would look covered while
    # never producing a reading.
    unmapped = set(taxonomy.unmapped_keys())
    for key in ("narrative", "catalyst", "tokenomics", "social_sentiment"):
        assert key in unmapped, key


def test_wick_is_separate_from_pin_bar():
    # A wick is raw geometry; a pin bar is one interpretation of it. Transcripts
    # say "it wicked into the zone and left" far more often than they name a
    # pattern, and folding them together would attribute plain wick language to
    # a pattern nobody claimed.
    assert taxonomy.match_terms("price wicked into the zone") == {"wick": 1}
    assert "pin_bar" in taxonomy.match_terms("big rejection wick there")
    assert taxonomy.BY_KEY["wick"].maps_to == "bot.smc.candles"


def test_wyckoff_avoids_the_bare_word_spring():
    # Same failure mode as bollinger's "squeeze": an ordinary English word as
    # an alias measures the language, not the idea.
    assert "wyckoff" not in taxonomy.match_terms("prices will spring back quickly")
    assert "wyckoff" in taxonomy.match_terms("a textbook wyckoff spring here")
    assert "wyckoff" in taxonomy.match_terms("the composite man is accumulating")


def test_indicator_group_splits_implemented_from_missing():
    # The split is the whole value of this group: mentions of rsi/macd/ma/atr
    # are TUNING signal (the code exists), mentions of vwap/volume profile are
    # FEATURE signal (it does not).
    for key in ("rsi", "macd", "moving_average"):
        assert taxonomy.BY_KEY[key].maps_to == "bot.indicators"
    assert taxonomy.BY_KEY["atr"].maps_to == "bot.position_sizing"
    assert taxonomy.BY_KEY["volume_profile"].maps_to == "bot.smc.volume_profile"
    assert taxonomy.BY_KEY["wyckoff"].maps_to == "bot.smc.wyckoff"


def test_indicator_aliases_do_not_match_ordinary_english():
    # Both of these shipped and were caught only by reading the matches. The
    # word "squeeze" gave bollinger 135 mentions across 59 videos, and "order
    # flow" gave cvd 195 -- neither indicator is meaningfully discussed at all.
    # A concept whose alias is a common verb measures the language, not the idea.
    assert "bollinger" not in taxonomy.match_terms(
        "price will squeeze through the supply zone")
    assert "cvd" not in taxonomy.match_terms(
        "reading the order flow into that level")
    # ...while the real names still match.
    assert "bollinger" in taxonomy.match_terms("the bollinger bands are tight")
    assert "volume_profile" in taxonomy.match_terms("check the volume profile")


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
