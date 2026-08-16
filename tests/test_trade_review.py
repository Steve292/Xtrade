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


def test_module_is_not_on_the_live_execute_path():
    """Advisory only, mirroring the bot/knowledge boundary discipline.

    Importing this module must not be enough to change live behaviour, so no
    live entry point may import it until that is a deliberate decision.
    """
    root = Path(__file__).resolve().parents[1]
    for rel in ("bot/runner.py", "hypertrade.py"):
        p = root / rel
        if not p.exists():
            continue
        assert "trade_review" not in p.read_text(), (
            f"{rel} imports bot.trade_review -- wiring the veto into the live "
            f"execute path is a deliberate change; update this test on purpose."
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
