"""Tests for bot/smc/mitigation.py and bot/smc/breaker.py.

No network. Run directly (`python tests/test_smc_zones.py`) or under pytest.

These two live in one file on purpose: they describe the SAME zone at different
points in its life, and the tests are only meaningful as a contrast. The same
order block either holds when price returns (a mitigation, trade with it) or
gets closed through (a breaker, trade the flipped zone from the far side). Split
across two files, the thing that matters -- that identical geometry produces
opposite conclusions -- would not be visible in either.

The polarity assertion in test_breaker_inverts_direction is the load-bearing
one. A breaker's direction is the INVERSE of the order block it came from, and
a detector that got it backwards would enter on the wrong side every time.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc import breaker, mitigation
from bot.smc.order_blocks import raw_order_blocks

COLS = ["open", "high", "low", "close"]


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS)


# A bullish order block forms at index 1 (down candle, then a >0.5% impulse),
# spanning roughly 99.0 - 100.5.
_FORMS = [
    (100.0, 100.6, 99.8, 100.1),
    (100.0, 100.5, 99.0, 99.2),     # <- the order block: bearish candle
    (99.2, 100.5, 99.1, 100.3),
    (100.3, 101.0, 100.2, 100.8),   # impulse confirms it
]

# ...price returns into the zone and bounces: HELD.
HELD = _df(_FORMS + [
    (100.8, 101.0, 99.5, 99.8),     # back inside the zone
    (99.8, 103.0, 99.7, 102.5),     # strong reaction away
])

# ...price returns and closes straight through the low: FAILED.
FAILED = _df(_FORMS + [
    (100.8, 101.0, 98.0, 98.2),     # closes below 99.0 -> broken
    (98.2, 99.6, 98.0, 99.4),       # retests the flipped zone from below
    (99.4, 99.5, 97.0, 97.5),       # rejected, continues down
])


def test_the_fixtures_contain_a_bullish_order_block():
    blocks = raw_order_blocks(HELD)
    assert any(b.direction == "bullish" for b in blocks), blocks


def test_mitigation_detects_a_zone_that_held():
    ms = mitigation.detect_mitigations(HELD)
    respected = [m for m in ms if m.respected]
    assert respected, ms
    m = respected[0]
    assert m.direction == "bullish"
    # Price re-enters on the very next bar (index 2 dips to 99.1, inside the
    # 99.0-100.5 zone). The mitigation is the RETURN, whenever it happens --
    # not something that has to wait several bars.
    assert m.mitigated_at >= 2
    assert m.reaction_pct > 0.003


def test_a_broken_zone_is_not_a_respected_mitigation():
    # Same geometry, opposite outcome -- the contrast this file exists for.
    # Scoped to the zone at index 1 (the bullish one that price closed
    # through). The later down-move legitimately forms its own bearish zones
    # which do get respected; asserting "no mitigations at all" would be
    # asserting something false about a realistic price series.
    ms = mitigation.detect_mitigations(FAILED)
    bullish_zone = [m for m in ms if m.index == 1]
    assert not any(m.respected for m in bullish_zone), bullish_zone


def test_breaker_detects_the_zone_that_failed():
    bs = breaker.detect_breakers(FAILED)
    assert bs, "no breaker found for a zone price closed through"


def test_breaker_inverts_direction():
    # A BULLISH order block that fails becomes a BEARISH breaker. The buyers who
    # defended it are trapped; their stops sit below. Inverting this would enter
    # long into a zone that just failed as support.
    bs = breaker.detect_breakers(FAILED)
    b = bs[0]
    assert b.origin_direction == "bullish"
    assert b.direction == "bearish", f"polarity not inverted: {b}"


def test_breaker_requires_a_close_through_not_a_wick():
    # Wick dips to 98 but the candle closes back above the zone floor: the zone
    # was tested and HELD. Classifying that as a breaker would call every
    # successful defence a failure.
    wicked = _df(_FORMS + [
        (100.8, 101.0, 98.0, 99.9),   # low pierces 99.0, close is above it
        (99.9, 102.0, 99.8, 101.5),
    ])
    assert breaker.detect_breakers(wicked) == []


def test_breaker_records_the_retest():
    bs = breaker.detect_breakers(FAILED)
    assert bs[0].retested is True
    assert bs[0].retest_at is not None


def test_active_helpers_respect_direction():
    m = mitigation.active_mitigation(HELD, price=100.0, direction="long")
    assert m is not None and m.direction == "bullish"
    # Same price, wrong side -> nothing.
    assert mitigation.active_mitigation(HELD, price=100.0, direction="short") is None


def test_active_breaker_only_returns_retested_zones():
    b = breaker.active_breaker(FAILED, price=99.4, direction="short")
    assert b is not None and b.direction == "bearish"
    assert b.origin_direction == "bullish", b
    assert b.retested is True
    # Price far outside every flipped zone matches nothing.
    assert breaker.active_breaker(FAILED, price=50.0, direction="short") is None


def test_empty_and_flat_input_never_raises():
    empty = _df([])
    flat = _df([(100, 100, 100, 100)] * 6)
    assert mitigation.detect_mitigations(empty) == []
    assert breaker.detect_breakers(empty) == []
    mitigation.detect_mitigations(flat)
    breaker.detect_breakers(flat)


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
