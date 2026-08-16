"""Tests for the abstention veto in bot/trade_review.py.

The property that matters is fail-closed: every path that is not a confirmed
SMC setup must VETO. A veto that silently allows on error would reintroduce
exactly the cohort the replay showed losing 33,859 over 2,947 trades.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.smc.strategy import Signal, SignalType  # noqa: E402
from bot.trade_review import (  # noqa: E402
    ReviewStats,
    Verdict,
    allows,
    review,
    summarize,
)


class _Stub:
    """Stands in for SMCStrategy. analyze() is the only surface used."""

    def __init__(self, signal=None, raises=None):
        self._signal = signal
        self._raises = raises

    def analyze(self, df, htf_df=None):
        if self._raises:
            raise self._raises
        return self._signal


def _frame(n: int = 50) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": [1.0] * n, "high": [1.1] * n,
        "low": [0.9] * n, "close": [1.0] * n, "volume": [100] * n,
    })


def _sig(t: SignalType, conf: float = 0.8) -> Signal:
    return Signal(type=t, entry=1.0, stop_loss=0.9, take_profit=1.2,
                  reason="test", confidence=conf)


def test_no_signal_is_vetoed():
    v = review(_Stub(_sig(SignalType.NONE)), _frame())
    assert not v.allowed, "NONE must veto"
    assert "no SMC setup" in v.reason


def test_long_signal_is_allowed():
    v = review(_Stub(_sig(SignalType.LONG)), _frame())
    assert v.allowed
    assert v.signal_type == "long"
    assert abs(v.confidence - 0.8) < 1e-9


def test_short_signal_is_allowed():
    assert review(_Stub(_sig(SignalType.SHORT)), _frame()).allowed


def test_strategy_error_fails_closed():
    """A broken detector must not become an implicit approval."""
    v = review(_Stub(raises=ValueError("detector exploded")), _frame())
    assert not v.allowed, "an exception must veto, never allow"
    assert "strategy error" in v.reason


def test_empty_data_fails_closed():
    assert not review(_Stub(_sig(SignalType.LONG)), pd.DataFrame()).allowed
    assert not review(_Stub(_sig(SignalType.LONG)), None).allowed


def test_allows_matches_review():
    for t in (SignalType.NONE, SignalType.LONG, SignalType.SHORT):
        s = _Stub(_sig(t))
        assert allows(s, _frame()) == review(s, _frame()).allowed


def test_veto_never_proposes_a_trade():
    """The module must expose no way to originate a trade -- only to remove one.

    Guards the documented boundary: the replay showed the directional signal
    was not predictive (-9.9pp win rate, z = -2.41), so this must stay a veto.
    """
    import bot.trade_review as tr

    assert not hasattr(tr, "propose")
    assert not hasattr(tr, "signal")
    # review() returns a Verdict, never a Signal
    v = review(_Stub(_sig(SignalType.LONG)), _frame())
    assert isinstance(v, Verdict)
    assert not isinstance(v, Signal)


def test_stats_accumulate_both_cohorts():
    s = ReviewStats()
    s.add(Verdict(True, "SMC setup present (long, 80%)"), 100.0)
    s.add(Verdict(True, "SMC setup present (long, 80%)"), -40.0)
    s.add(Verdict(False, "no SMC setup (flat)"), -250.0)
    s.add(Verdict(False, "no SMC setup (flat)"), 10.0)

    assert s.total == 4
    assert s.kept == 2 and s.vetoed == 2
    assert abs(s.kept_pnl - 60.0) < 1e-9
    assert abs(s.vetoed_pnl - (-240.0)) < 1e-9
    assert abs(s.kept_win_rate - 50.0) < 1e-9
    assert abs(s.vetoed_win_rate - 50.0) < 1e-9
    assert abs(s.pnl_avoided - (-240.0)) < 1e-9


def test_stats_group_veto_reasons():
    s = ReviewStats()
    s.add(Verdict(False, "no SMC setup (flat)"), -1.0)
    s.add(Verdict(False, "no SMC setup (choppy)"), -1.0)
    s.add(Verdict(False, "strategy error: ValueError: x"), -1.0)
    assert s.reasons["no SMC setup"] == 2
    assert sum(s.reasons.values()) == 3


def test_summarize_reports_losses_avoided():
    s = ReviewStats()
    s.add(Verdict(True, "SMC setup present (long, 80%)"), 100.0)
    s.add(Verdict(False, "no SMC setup (flat)"), -250.0)
    out = summarize(s)
    assert "losses avoided" in out
    assert "reviewed      : 2" in out
    assert summarize(ReviewStats()) == "no trades reviewed"


def test_veto_is_armed_on_the_live_execute_path():
    """The veto is ARMED in bot/runner.py as of the decision to wire it in.

    This started life as the opposite assertion -- that no live entry point
    imported the module -- so that arming it could not happen as a side effect
    of someone adding an import. It was flipped deliberately. Keeping the test
    (rather than deleting it) means DISarming is equally deliberate: remove the
    veto and this fails.
    """
    root = Path(__file__).resolve().parents[1]
    runner = (root / "bot/runner.py").read_text()
    assert "trade_review" in runner, "the veto must stay wired into bot/runner.py"
    assert "verdict.allowed" in runner, (
        "runner must branch on the verdict, not merely import the module"
    )


def test_runner_has_no_second_no_signal_gate():
    """The veto must be the ONLY 'is there a setup' test in the live path.

    Two gates asking the same question is how they drift apart: one gets
    updated, the other silently keeps the old behaviour. The inline
    `signal.type != SignalType.NONE` checks the veto replaced must stay gone.
    """
    root = Path(__file__).resolve().parents[1]
    src = (root / "bot/runner.py").read_text()
    assert "signal.type != SignalType.NONE" not in src, (
        "bot/runner.py still has an inline no-signal check; the veto in "
        "bot/trade_review.py is meant to be the single gate."
    )


def test_gates_are_advisory_in_config():
    """The eight optional gates run as indicators, not vetoes."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config.yaml").read_text())
    assert cfg["screening"].get("advisory_only") is True, (
        "screening.advisory_only must stay true: the veto is the only gate "
        "allowed to stop a trade"
    )


def test_screen_still_reports_an_honest_verdict_under_advisory():
    """advisory_only must not change what screen() computes.

    If it silently forced approved=True, every log line and downstream test
    reading ScreenResult.approved would start lying about what the gates said.
    """
    from bot.screening import ScreenConfig

    assert ScreenConfig().advisory_only is False, "advisory must be opt-in"
    src = (Path(__file__).resolve().parents[1] / "bot/screening.py").read_text()
    body = src.split("def screen(")[1]
    assert "advisory_only" not in body, (
        "screen() must not read advisory_only -- honouring it is the caller's "
        "job, so ScreenResult.approved keeps meaning 'did the gates approve'"
    )


def test_advisory_cannot_bypass_the_veto():
    """ORDER MATTERS. The veto's `continue` must come BEFORE the advisory branch.

    If advisory were evaluated first, or the veto's skip were made conditional
    on it, a no-setup trade could fire -- which is precisely the 2,947-trade
    cohort that lost 33,859 at PF 0.912.
    """
    src = (Path(__file__).resolve().parents[1] / "bot/runner.py").read_text()
    veto_skip = src.index("if not verdict.allowed:")
    advisory = src.index("advisory = getattr(screener.cfg")
    assert veto_skip < advisory, (
        "the veto must be evaluated and able to `continue` before advisory "
        "mode is consulted"
    )
    # and the veto's skip must be unconditional. Strip comments first -- the
    # block is heavily commented and the word "advisory" appears in the prose
    # explaining it, which is not the same as the code depending on it.
    seg = src[veto_skip:advisory]
    code = "\n".join(ln.split("#", 1)[0] for ln in seg.splitlines())
    assert "continue" in code, "the veto branch must still skip the symbol"
    assert "advisory" not in code, (
        "the veto's skip must not depend on advisory mode"
    )


def test_runner_fires_on_the_veto_when_gates_dissent():
    src = (Path(__file__).resolve().parents[1] / "bot/runner.py").read_text()
    assert "if unified.approved or advisory:" in src, (
        "runner must fire when the veto allows, even if the gates dissent"
    )


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
