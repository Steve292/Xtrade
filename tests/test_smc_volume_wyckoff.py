"""Tests for bot/smc/volume_profile.py and bot/smc/wyckoff.py.

No network. Run directly (`python tests/test_smc_volume_wyckoff.py`) or under
pytest.

Both modules were built from corpus findings -- volume profile in 84 of 157
videos, Wyckoff in 61 -- so the tests are written against the definitions those
educators actually use, not against whatever the implementation happened to do.

The load-bearing assertions:

  * the value area is grown OUTWARD FROM THE POC, so it is the narrowest band
    holding 70% of volume. A fixed price percentile would be a different,
    wrong thing that looks similar on a symmetric fixture.
  * a spring requires a CLOSE back inside the range. Detecting on lows alone
    would report every genuine breakdown as a bullish spring -- inverting the
    signal exactly when it matters most.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc import volume_profile as vp
from bot.smc import wyckoff

COLS = ["open", "high", "low", "close", "volume"]


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS)


# Most volume transacts around 100; thin tails above and below.
CONCENTRATED = _df(
    [(99, 101, 99, 100, 10)] * 20        # heavy, tight, around 100
    + [(103, 105, 103, 104, 1)] * 3      # thin excursion up
    + [(95, 97, 95, 96, 1)] * 3          # thin excursion down
)


def test_profile_finds_the_point_of_control_where_volume_concentrated():
    p = vp.build_profile(CONCENTRATED, bins=20)
    assert p is not None
    assert 99 <= p.poc <= 101, f"POC {p.poc} is not in the heavy zone"


def test_value_area_is_narrow_when_volume_is_concentrated():
    p = vp.build_profile(CONCENTRATED, bins=20)
    width = p.value_area_high - p.value_area_low
    full = 105 - 95
    # 70% of volume sits in a small slice of the range, so the value area must
    # be a small slice too. A fixed percentile of PRICE would return ~70% of
    # the full span here and pass a weaker assertion.
    assert width < full * 0.5, f"value area {width} too wide vs range {full}"
    assert p.value_area_low <= p.poc <= p.value_area_high


def test_value_area_contains_roughly_the_requested_share():
    p = vp.build_profile(CONCENTRATED, bins=40, value_area_pct=0.70)
    inside = sum(n.volume for n in p.nodes
                 if n.low >= p.value_area_low and n.high <= p.value_area_high)
    assert 0.60 <= inside / p.total_volume <= 0.95


def test_high_and_low_volume_nodes_separate():
    p = vp.build_profile(CONCENTRATED, bins=20)
    hvn = vp.high_volume_nodes(p)
    lvn = vp.low_volume_nodes(p)
    assert hvn and lvn
    assert max(n.volume for n in lvn) < min(n.volume for n in hvn)


def test_value_area_edge_detection():
    p = vp.build_profile(CONCENTRATED, bins=20)
    assert vp.at_value_area_edge(CONCENTRATED, p.value_area_high, bins=20) == "high"
    assert vp.at_value_area_edge(CONCENTRATED, p.value_area_low, bins=20) == "low"
    # Middle of the value area is fair value -- no edge, nothing to trade.
    assert vp.at_value_area_edge(CONCENTRATED, p.poc, bins=20) is None


def test_profile_handles_missing_volume_and_empty_input():
    no_vol = pd.DataFrame([(1, 2, 0.5, 1.5)], columns=["open", "high", "low", "close"])
    assert vp.build_profile(no_vol) is None
    assert vp.build_profile(_df([])) is None
    assert vp.build_profile(_df([(100, 100, 100, 100, 0)] * 5)) is None


# --- wyckoff ---------------------------------------------------------------

_RANGE = [(100, 101, 99, 100, 10)] * 14

SPRING = _df(_RANGE + [
    (100, 100.5, 96.0, 99.5, 30),    # dips below 99, CLOSES back inside
    (99.5, 102, 99.4, 101.5, 12),
])

BREAKDOWN = _df(_RANGE + [
    (100, 100.5, 96.0, 96.5, 30),    # dips below 99 and CLOSES below it
    (96.5, 97, 94, 94.5, 12),
])

UPTHRUST = _df(_RANGE + [
    (100, 104.0, 99.8, 100.5, 30),   # pokes above 101, CLOSES back inside
    (100.5, 101, 98, 98.5, 12),
])


def test_detects_a_trading_range():
    rng = wyckoff.detect_range(_df(_RANGE))
    assert rng is not None and rng.high >= 101 and rng.low <= 99


def test_a_trending_market_is_not_a_range():
    trending = _df([(100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(40)])
    assert wyckoff.detect_range(trending) is None


def test_spring_requires_a_close_back_inside():
    st = wyckoff.analyse(SPRING)
    kinds = {e.kind for e in st.events}
    assert "spring" in kinds, st.events
    assert st.bias == "accumulation", st.bias


def test_a_real_breakdown_is_not_a_spring():
    # The distinction the whole module turns on. Detecting on lows alone would
    # call this bullish -- precisely inverting the signal on a failing range.
    st = wyckoff.analyse(BREAKDOWN)
    assert "spring" not in {e.kind for e in st.events}, st.events


def test_upthrust_is_bearish():
    st = wyckoff.analyse(UPTHRUST)
    assert "upthrust" in {e.kind for e in st.events}, st.events
    assert st.bias == "distribution"
    assert st.direction == "bearish"


def test_confirms_matches_direction():
    assert wyckoff.confirms(SPRING, "long") is not None
    assert wyckoff.confirms(SPRING, "short") is None
    assert wyckoff.confirms(UPTHRUST, "short") is not None


def test_no_range_means_neutral_not_a_guess():
    trending = _df([(100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(40)])
    st = wyckoff.analyse(trending)
    assert st.trading_range is None and st.bias == "neutral"
    assert wyckoff.confirms(trending, "long") is None


def test_degenerate_input_never_raises():
    assert wyckoff.analyse(_df([])).bias == "neutral"
    wyckoff.analyse(_df([(100, 100, 100, 100, 0)] * 20))


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
