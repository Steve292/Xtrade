"""Tests for the VWAP half of bot/smc/volume_profile.py.

No network. Run directly (`python tests/test_smc_vwap.py`) or under pytest.

VWAP was added because of a corpus comparison, not a hunch: it appeared in 3%
of the original 329 documents and 32% of the 101 documents from two channels
chosen for methodological similarity. It was invisible until the sample was
widened, which is the same lesson Wyckoff taught in the opposite direction.

The assertion that carries this file is test_vwap_is_volume_weighted_not_a_mean.
A VWAP that ignores volume is just a moving average with extra steps, and on
symmetric fixture data the two are numerically identical -- so the test uses
deliberately lopsided volume, where an unweighted mean gives a visibly wrong
answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc import volume_profile as vp

COLS = ["open", "high", "low", "close", "volume"]


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS)


def test_vwap_is_volume_weighted_not_a_mean():
    # Two bars: one at 100 on huge volume, one at 200 on tiny volume.
    # Unweighted mean would be ~150. VWAP must sit close to 100.
    df = _df([(100, 100, 100, 100, 1000), (200, 200, 200, 200, 1)])
    st = vp.vwap_state(df)
    assert st is not None
    assert st.vwap < 101, f"VWAP {st.vwap} ignored volume (a mean would give 150)"


def test_typical_price_uses_hlc_not_close():
    df = _df([(100, 120, 80, 100, 10)])
    tp = float(vp.typical_price(df).iloc[0])
    assert abs(tp - 100.0) < 1e-9      # (120+80+100)/3
    # A close-only implementation would also give 100 here, so use an
    # asymmetric bar to actually distinguish them.
    df2 = _df([(100, 130, 100, 100, 10)])
    tp2 = float(vp.typical_price(df2).iloc[0])
    assert tp2 > 100.0, "typical price collapsed to the close"


def test_anchoring_changes_the_result():
    # A VWAP anchored to whatever data happened to be fetched is an artifact of
    # the fetch window. Different anchors must give different levels, or the
    # anchor argument is decorative.
    rows = [(100 + i, 101 + i, 99 + i, 100 + i, 10) for i in range(60)]
    df = _df(rows)
    a = vp.vwap_state(df, 0)
    b = vp.vwap_state(df, 50)
    assert a is not None and b is not None
    assert abs(a.vwap - b.vwap) > 1.0, (a.vwap, b.vwap)


def test_bands_widen_with_dispersion():
    tight = _df([(100, 100.2, 99.8, 100, 10)] * 40)
    wide = _df([(100 + (i % 2) * 20, 121, 99, 100 + (i % 2) * 20, 10)
                for i in range(40)])
    st_t = vp.vwap_state(tight)
    st_w = vp.vwap_state(wide)
    assert st_w.stdev > st_t.stdev


def test_side_reports_position_relative_to_vwap():
    df = _df([(100, 100, 100, 100, 10)] * 30)
    st = vp.vwap_state(df)
    assert st.side(120.0) == "above"
    assert st.side(80.0) == "below"
    assert st.side(100.0) == "at"


def test_at_vwap_wants_long_below_and_short_above():
    # Buying well ABOVE the volume-weighted consensus price is paying more than
    # the average participant -- the entry this material warns against. Checked
    # with an explicit anchor so the asymmetric default anchoring (long->swing
    # low, short->swing high) cannot mask the direction logic.
    df = _df([(100, 100, 100, 100, 10)] * 30)
    assert vp.at_vwap(df, 90.0, "long", anchor_index=0) is True
    assert vp.at_vwap(df, 110.0, "long", anchor_index=0) is False
    assert vp.at_vwap(df, 110.0, "short", anchor_index=0) is True
    assert vp.at_vwap(df, 90.0, "short", anchor_index=0) is False


def test_price_at_vwap_satisfies_both_directions():
    df = _df([(100, 100, 100, 100, 10)] * 30)
    assert vp.at_vwap(df, 100.0, "long", anchor_index=0) is True
    assert vp.at_vwap(df, 100.0, "short", anchor_index=0) is True


def test_anchor_to_recent_extreme_finds_the_extreme():
    rows = [(100, 101, 99, 100, 10)] * 20
    rows[7] = (100, 101, 80, 100, 10)          # the low
    df = _df(rows)
    assert vp.anchor_to_recent_extreme(df, kind="low") == 7


def test_missing_or_zero_volume_returns_none_not_a_wrong_number():
    no_vol = pd.DataFrame([(1, 2, 0.5, 1.5)], columns=["open", "high", "low", "close"])
    assert vp.vwap_series(no_vol) is None
    assert vp.vwap_state(no_vol) is None
    assert vp.vwap_state(_df([(100, 100, 100, 100, 0)] * 5)) is None
    assert vp.vwap_state(_df([])) is None
    # A gate must fail closed when it cannot compute, never pass vacuously.
    assert vp.at_vwap(_df([]), 100.0, "long") is False


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
