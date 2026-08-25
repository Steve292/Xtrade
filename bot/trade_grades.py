"""
Grade SMC concepts by what they actually did, not by how often they get talked about.

bot/knowledge.py weights each detector by its prevalence in the ingested
corpus -- how many times ~1000 transcripts mention "order block", "liquidity
pool", and so on. That is a popularity measure. It says nothing about whether
trading a concept makes money, and the corpus contains no win rate, no P&L,
and no backtest result to say otherwise.

This module builds the missing half from the only source that can supply it:
this account's own closed trades. Every entry records which detectors fired;
every exit records what happened. Accumulated per detector, that yields a real
win rate and net P&L -- prevalence replaced by performance.

Two properties this deliberately keeps:

1. It NEVER extrapolates from a thin sample. grade_for() returns None until a
   detector has at least MIN_SAMPLE closed trades. A 100% win rate from one
   trade is noise, and dressing it up as a grade would be worse than having no
   grade at all -- it would look like evidence.

2. It records; it does not decide. Nothing here changes sizing, entry, or exit.
   bot/knowledge.py may consult these grades once they exist, gated by its own
   config flag, and the caller can always see the sample size behind a number.

Storage is a JSON file of closed-trade records rather than running totals, so
the grades can be recomputed if the grading rule changes and a bad run can be
inspected trade by trade.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("trade_grades.json")

# Closed trades a detector needs before grade_for() reports anything. Below
# this the win rate is noise; see the module docstring.
MIN_SAMPLE = 10


@dataclass
class DetectorGrade:
    module: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return (self.wins / decided) if decided else None

    @property
    def avg_pnl(self) -> float | None:
        return (self.net_pnl / self.trades) if self.trades else None

    @property
    def graded(self) -> bool:
        """Whether this detector has enough closed trades to be believed."""
        return self.trades >= MIN_SAMPLE


@dataclass
class GradeBook:
    records: list = field(default_factory=list)
    path: Path = DEFAULT_PATH

    # --- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "GradeBook":
        """Never raises: a missing or corrupt file yields an empty book. A
        grading store is an observation log, and losing it must not be able to
        stop the trading loop."""
        path = Path(path)
        if not path.exists():
            return cls(records=[], path=path)
        try:
            data = json.loads(path.read_text())
            records = data.get("records") if isinstance(data, dict) else data
            return cls(records=records if isinstance(records, list) else [], path=path)
        except (json.JSONDecodeError, OSError, TypeError):
            return cls(records=[], path=path)

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps({"records": self.records}, indent=1))
        except OSError:
            pass

    # --- recording ---------------------------------------------------------

    def record_entry(self, trade_id, symbol: str, side: str, detectors,
                     entry: float, stop: float, take_profit: float,
                     confidence: float = 0.0, knowledge_pct: float = 0.0) -> None:
        """Open a record. `detectors` are the dotted module names from
        Signal.detectors -- the same identifiers the corpus's `maps_to` uses,
        which is what lets a grade line up with a corpus weight later."""
        self.records.append({
            "trade_id": str(trade_id),
            "symbol": symbol,
            "side": side,
            "detectors": list(detectors or []),
            "entry": float(entry),
            "stop": float(stop),
            "take_profit": float(take_profit),
            "confidence": float(confidence),
            "knowledge_pct": float(knowledge_pct),
            "opened_at": time.time(),
            "outcome": None,
            "pnl": None,
            "closed_at": None,
        })
        self.save()

    def record_exit(self, trade_id, pnl: float, outcome: str | None = None) -> bool:
        """Close the matching open record. Returns False when no open record
        matches -- a close with no recorded entry (a restart mid-trade, a
        position closed by hand) is not an error, it is simply ungradeable,
        and inventing an entry for it would poison the sample."""
        for rec in reversed(self.records):
            if rec["trade_id"] == str(trade_id) and rec["outcome"] is None:
                rec["pnl"] = float(pnl)
                rec["outcome"] = outcome or ("win" if pnl > 0 else "loss" if pnl < 0 else "flat")
                rec["closed_at"] = time.time()
                self.save()
                return True
        return False

    # --- grading -----------------------------------------------------------

    def grades(self) -> dict:
        """Per-detector performance over CLOSED trades only.

        A trade credits every detector that fired on it, so a setup built on
        five detectors contributes to all five. That is attribution, not
        isolation: it cannot say which detector was responsible, only which
        ones were present when things went well or badly. Isolating a single
        detector's contribution would need setups that fired on it alone,
        which the confluence model does not produce.
        """
        out: dict = {}
        for rec in self.records:
            if rec.get("outcome") is None:
                continue
            pnl = rec.get("pnl") or 0.0
            for module in rec.get("detectors") or []:
                g = out.setdefault(module, DetectorGrade(module=module))
                g.trades += 1
                g.net_pnl += pnl
                if rec["outcome"] == "win":
                    g.wins += 1
                elif rec["outcome"] == "loss":
                    g.losses += 1
        return out

    def grade_for(self, module: str) -> DetectorGrade | None:
        """This detector's grade, or None if it has not cleared MIN_SAMPLE."""
        g = self.grades().get(module)
        return g if (g and g.graded) else None

    def summary(self) -> dict:
        closed = [r for r in self.records if r.get("outcome") is not None]
        wins = sum(1 for r in closed if r["outcome"] == "win")
        losses = sum(1 for r in closed if r["outcome"] == "loss")
        graded = sum(1 for g in self.grades().values() if g.graded)
        return {
            "recorded": len(self.records),
            "open": len(self.records) - len(closed),
            "closed": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / (wins + losses)) if (wins + losses) else None,
            "net_pnl": sum((r.get("pnl") or 0.0) for r in closed),
            "detectors_graded": graded,
            "min_sample": MIN_SAMPLE,
        }
