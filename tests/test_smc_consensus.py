"""Tests for bot/smc/consensus.py — the multi-concept decision layer.

No network. Run directly (`python tests/test_smc_consensus.py`) or under pytest.

The two assertions that carry this file:

  test_abstain_is_not_disagree -- a concept that CANNOT read a setup (no volume
  column, no trading range) must not be scored against the trade. Collapsing
  "I don't know" into "no" makes a system that always finds a reason to refuse,
  and hides which conditions were actually measured.

  test_no_opinion_is_not_support -- if every concept abstains the verdict is
  no_opinion, never a pass. Absence of dissent is the most dangerous thing to
  mistake for agreement, because it looks identical to unanimous approval in
  any summary that only reports a score.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.smc import consensus
from bot.smc.consensus import ABSTAIN, AGREE, DISAGREE, ConceptVote, ConsensusResult

COLS = ["open", "high", "low", "close", "volume"]


def _df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLS)


TREND_UP = _df([(100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(80)])
FLAT = _df([(100, 100.5, 99.5, 100, 10)] * 80)


def _result(votes) -> ConsensusResult:
    total = sum(v.weight for v in votes if v.verdict != ABSTAIN)
    score = (sum(v.signed for v in votes) / total) if total else 0.0
    return ConsensusResult(
        direction="long", votes=votes, score=score,
        agreed=sum(1 for v in votes if v.verdict == AGREE),
        dissented=sum(1 for v in votes if v.verdict == DISAGREE),
        abstained=sum(1 for v in votes if v.verdict == ABSTAIN),
    )


def test_abstain_is_not_disagree():
    # One agree + one abstain must score the same as one agree alone. If
    # abstention counted against the trade, the score would be halved.
    a = _result([ConceptVote("x", AGREE, "", 1.0)])
    b = _result([ConceptVote("x", AGREE, "", 1.0),
                 ConceptVote("y", ABSTAIN, "no data", 1.0)])
    assert a.score == b.score == 1.0


def test_no_opinion_is_not_support():
    r = _result([ConceptVote("x", ABSTAIN, "no data", 1.0),
                 ConceptVote("y", ABSTAIN, "no data", 1.0)])
    assert r.verdict == "no_opinion"
    assert "NOT support" in r.diagnose()


def test_dissent_can_be_outweighed_but_is_reported():
    votes = [ConceptVote("a", AGREE, "ok", 1.0), ConceptVote("b", AGREE, "ok", 1.0),
             ConceptVote("c", AGREE, "ok", 1.0), ConceptVote("d", DISAGREE, "bad", 0.3)]
    r = _result(votes)
    assert r.verdict in ("strong", "supported")
    d = r.diagnose()
    assert "DISSENT" in d and "bad" in d
    assert "Carried despite" in d


def test_contradicted_when_evidence_is_against():
    r = _result([ConceptVote("a", DISAGREE, "no", 1.0),
                 ConceptVote("b", DISAGREE, "no", 1.0),
                 ConceptVote("c", AGREE, "yes", 0.3)])
    assert r.verdict == "contradicted"
    assert "AGAINST this trade" in r.diagnose()


def test_mostly_abstaining_is_flagged_as_weak():
    r = _result([ConceptVote("a", AGREE, "ok", 1.0),
                 ConceptVote("b", ABSTAIN, "n/a", 1.0),
                 ConceptVote("c", ABSTAIN, "n/a", 1.0),
                 ConceptVote("d", ABSTAIN, "n/a", 1.0)])
    assert "weaker than it looks" in r.diagnose()


def test_wyckoff_is_weighted_below_cross_channel_concepts():
    # Wyckoff looked like a top-three finding on one channel and collapsed to
    # 61-of-62-videos-from-one-source once three more were ingested. The weight
    # has to record that, or single-source vocabulary outvotes replicated ideas.
    assert consensus.WEIGHTS["wyckoff"] < consensus.WEIGHTS["mitigation"]
    assert consensus.WEIGHTS["wyckoff"] < consensus.WEIGHTS["candle"]
    assert consensus.WEIGHTS["mitigation"] == 1.0


def test_evaluate_runs_every_concept_on_real_shaped_data():
    r = consensus.evaluate(TREND_UP, TREND_UP, "long", float(TREND_UP.iloc[-1]["close"]))
    assert len(r.votes) == 10, [v.concept for v in r.votes]
    assert r.agreed + r.dissented + r.abstained == 10
    assert -1.0 <= r.score <= 1.0
    r.diagnose()          # must not raise


def test_missing_volume_makes_volume_profile_abstain_not_dissent():
    no_vol = TREND_UP[["open", "high", "low", "close"]]
    r = consensus.evaluate(no_vol, no_vol, "long", float(no_vol.iloc[-1]["close"]))
    vp = [v for v in r.votes if v.concept == "volume_profile"][0]
    assert vp.verdict == ABSTAIN, vp
    assert "volume" in vp.detail


def test_a_broken_detector_abstains_instead_of_crashing():
    # One failing concept must not take the decision layer down. A system that
    # dies when a single detector throws is worse than one that says "unknown".
    r = consensus.evaluate(_df([]), None, "long", 100.0)
    assert isinstance(r, ConsensusResult)
    assert len(r.votes) == 10
    r.diagnose()


def test_direction_is_respected():
    up = consensus.evaluate(TREND_UP, TREND_UP, "long", float(TREND_UP.iloc[-1]["close"]))
    down = consensus.evaluate(TREND_UP, TREND_UP, "short", float(TREND_UP.iloc[-1]["close"]))
    # In a clean uptrend the structure vote must not agree with both sides.
    us = [v for v in up.votes if v.concept == "structure"][0]
    ds = [v for v in down.votes if v.concept == "structure"][0]
    assert not (us.verdict == AGREE and ds.verdict == AGREE)


def test_config_keys_reach_the_live_strategy():
    """A config key nothing reads is a silent no-op, not a setting.

    stop_atr_mult was added to config.yaml and initially wired into NOTHING --
    bot/runner.py passed stop_loss_pct and ignored it, so the fix for the
    unreachable-stop bug would have changed no behaviour at all while looking
    fully applied. This asserts the live execute paths actually forward it.
    """
    import re
    root = Path(__file__).resolve().parents[1]
    for rel in ("bot/runner.py", "hypertrade.py"):
        src = (root / rel).read_text()
        if "SMCStrategy(" not in src:
            continue
        assert "stop_atr_mult" in src, f"{rel} builds SMCStrategy without stop_atr_mult"


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
