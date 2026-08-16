"""Tests for the crypto-narrative vocabulary and content-type segregation.

No network. Run directly (`python tests/test_knowledge_narrative.py`) or under
pytest.

WHY THIS EXISTS. Ingesting crashiusclay69 produced 3.0 concept matches per
document against 11.6 across the six price-action channels, with mitigation,
order_block, supply_demand, sweep, stop_loss, risk_reward, confluence, bos,
htf_alignment and volume_profile all at exactly ZERO. That is a vocabulary
mismatch, not an empty channel: those six answer "where do I enter" and this
one answers "which token, and why now".

The segregation half matters as much as the words. consensus weights are
channel-breadth x document-share, so folding memecoin documents into the same
denominator would drag down concepts measured across price-action channels --
not because entries became less important, but because a channel that never
discusses entries got counted as evidence about entries.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.knowledge import taxonomy


def test_narrative_group_exists_and_is_distinct():
    keys = {c.key for c in taxonomy.CONCEPTS if c.category == "narrative"}
    assert keys == {"meme_season", "narrative", "catalyst", "tokenomics",
                    "social_sentiment"}, keys


def test_narrative_terms_match_real_crypto_phrasing():
    hits = taxonomy.match_terms(
        "this narrative is rotating, watch the binance listing and the fdv")
    assert "narrative" in hits and "catalyst" in hits and "tokenomics" in hits


def test_meme_season_maps_to_real_code():
    # bot/hotness.py genuinely implements dominance-based meme-season scoring.
    assert taxonomy.BY_KEY["meme_season"].maps_to == "bot.hotness"


def test_stub_backed_concepts_stay_unmapped():
    # bot/smart_money.py::narrative_decay_signal exists but always returns
    # {"available": False} with no data source. Mapping to a function that
    # never returns a reading would claim coverage this repo does not have --
    # exactly the kind of green-looking gap report the unmapped view exists to
    # prevent.
    for key in ("narrative", "catalyst", "tokenomics", "social_sentiment"):
        assert taxonomy.BY_KEY[key].maps_to is None, key


def test_price_action_density_separates_content_types():
    price_action = ["mitigation", "order_block", "sweep", "fvg"]
    narrative = ["narrative", "catalyst", "tokenomics"]
    assert taxonomy.price_action_density(price_action) == 1.0
    assert taxonomy.price_action_density(narrative) == 0.0
    mixed = price_action + narrative
    d = taxonomy.price_action_density(mixed)
    assert 0.4 < d < 0.7, d


def test_density_of_nothing_is_zero_not_an_error():
    assert taxonomy.price_action_density([]) == 0.0
    assert taxonomy.price_action_density(["not_a_concept"]) == 0.0


def test_narrative_aliases_do_not_collide_with_price_action():
    # The uniqueness test in test_knowledge_taxonomy covers all concepts, but
    # this group was written against a vocabulary that already owned "liquidity",
    # "target", "trap", "premium" and "gap" -- the collisions were live risks,
    # not hypothetical.
    owner = {}
    for c in taxonomy.CONCEPTS:
        for term in c.all_terms():
            assert term not in owner or owner[term] == c.key, (
                f"{term!r} claimed by {owner.get(term)} and {c.key}")
            owner[term] = c.key


def test_every_narrative_maps_to_importable_code():
    import importlib.util
    for c in taxonomy.CONCEPTS:
        if c.category == "narrative" and c.maps_to:
            assert importlib.util.find_spec(c.maps_to) is not None, c.maps_to


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
