"""Multi-concept decision layer: many opinions, one verdict, and a reason.

The seven-gate screen in bot/screening.py is an AND. Every gate must pass, so a
single failing check kills the trade and the reason it failed is the only thing
you learn. That is safe but blunt: it cannot tell the difference between "one
minor condition missed while everything else strongly agrees" and "nothing
agrees at all", and it discards the first case entirely.

This module answers a different question. Every concept the corpus identified as
load-bearing gets a VOTE -- agree, disagree, or abstain -- and the result carries
a diagnosis explaining what dissented and whether the remaining evidence still
supports the trade. When a concept fails, you get why it failed, not just that
it did.

    screening.py   "is every condition met?"          -> trade / no trade
    consensus.py   "what does each concept think,     -> score + diagnosis
                    and if one dissents, do the
                    others still carry it?"

ABSTAIN IS NOT DISAGREE, and keeping them separate is the whole design. A
volume profile cannot form an opinion on a feed with no volume column; a Wyckoff
reading is meaningless when price is trending rather than ranging. Scoring those
as disagreement would penalise a trade for conditions that were never measurable,
and a system that cannot say "I don't know" will always find a reason to say no.

WEIGHTS COME FROM CROSS-CHANNEL EVIDENCE, not from conviction. A concept taught
by four independent educators is weighted above one taught by a single channel,
because the four-channel signal survived a test the single-channel one failed.
Wyckoff is the concrete case: it looked like a top-three finding on one channel
and collapsed to 61-of-62-videos-from-one-source once three more were ingested.
It is included, at a weight that reflects that.

NOTHING HERE PLACES A TRADE. It scores and explains; bot/screening.py remains
the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

AGREE = "agree"
DISAGREE = "disagree"
ABSTAIN = "abstain"

# channels-out-of-4 that discussed the concept, from the 329-document corpus.
# 4/4 concepts weigh most; single-channel concepts weigh least.
WEIGHTS: dict = {
    "mitigation": 1.0,        # 4 channels, 183 videos
    "candle": 1.0,            # 4 channels (candle_close 81v, pin_bar 60v)
    "structure": 1.0,         # 4 channels (swing 291v, bos)
    "liquidity_sweep": 1.0,   # 4 channels, 183 videos
    "supply_demand": 1.0,     # 4 channels, 161 videos
    "premium_discount": 1.0,  # 4 channels, 164 videos
    "fibonacci": 0.8,         # 4 channels but thinner, 89 videos
    "volume_profile": 0.7,    # 3 channels, 115 videos
    "breaker": 0.7,           # 3 channels, 89 videos
    "wyckoff": 0.3,           # 2 channels, and 61 of 62 videos are ONE of them
}


@dataclass
class ConceptVote:
    concept: str
    verdict: str            # agree | disagree | abstain
    detail: str
    weight: float = 1.0

    @property
    def signed(self) -> float:
        if self.verdict == AGREE:
            return self.weight
        if self.verdict == DISAGREE:
            return -self.weight
        return 0.0


@dataclass
class ConsensusResult:
    direction: str
    votes: list[ConceptVote] = field(default_factory=list)
    score: float = 0.0          # -1.0 .. +1.0 over the concepts that voted
    agreed: int = 0
    dissented: int = 0
    abstained: int = 0

    @property
    def participating_weight(self) -> float:
        return sum(v.weight for v in self.votes if v.verdict != ABSTAIN)

    @property
    def verdict(self) -> str:
        if self.participating_weight <= 0:
            return "no_opinion"
        if self.score >= 0.6:
            return "strong"
        if self.score >= 0.25:
            return "supported"
        if self.score > -0.25:
            return "mixed"
        return "contradicted"

    def diagnose(self) -> str:
        """Why the dissenters dissented, and whether the rest still carry it.

        This is the "reanalyse when a concept fails" step. A bare score hides
        the two cases that matter most: a strong setup with one nitpick, and a
        weak setup that only looks acceptable because most concepts abstained.
        """
        lines = [f"  direction {self.direction}  verdict {self.verdict.upper()}  "
                 f"score {self.score:+.2f}  "
                 f"({self.agreed} agree / {self.dissented} dissent / "
                 f"{self.abstained} abstain)"]

        dissent = [v for v in self.votes if v.verdict == DISAGREE]
        agree = [v for v in self.votes if v.verdict == AGREE]
        abstain = [v for v in self.votes if v.verdict == ABSTAIN]

        if dissent:
            lines.append("  DISSENT — what failed and why:")
            for v in sorted(dissent, key=lambda x: -x.weight):
                lines.append(f"    - {v.concept:16s} (w{v.weight:.1f})  {v.detail}")
        if agree:
            lines.append("  SUPPORT — what still backs the trade:")
            for v in sorted(agree, key=lambda x: -x.weight):
                lines.append(f"    + {v.concept:16s} (w{v.weight:.1f})  {v.detail}")
        if abstain:
            lines.append("  NO READING — these could not form an opinion:")
            for v in abstain:
                lines.append(f"    ? {v.concept:16s}  {v.detail}")

        # The honest interpretation, spelled out rather than left to the reader.
        if self.verdict == "no_opinion":
            lines.append("  => Nothing could read this setup. Absence of dissent is "
                         "NOT support.")
        elif self.abstained > self.agreed + self.dissented:
            lines.append("  => Most concepts abstained. The score rests on a "
                         "minority and is weaker than it looks.")
        elif self.verdict == "contradicted":
            lines.append("  => The weight of evidence is AGAINST this trade, not "
                         "merely absent.")
        elif dissent and self.verdict in ("strong", "supported"):
            top = max(dissent, key=lambda v: v.weight)
            lines.append(f"  => Carried despite {top.concept} dissenting. The "
                         f"remaining concepts outweigh it; that is a judgement, "
                         f"not a certainty.")
        return "\n".join(lines)


def _vote(concept: str, ok: bool | None, yes: str, no: str,
          abstain_reason: str = "no reading available") -> ConceptVote:
    w = WEIGHTS.get(concept, 0.5)
    if ok is None:
        return ConceptVote(concept, ABSTAIN, abstain_reason, w)
    return ConceptVote(concept, AGREE if ok else DISAGREE, yes if ok else no, w)


def evaluate(df: pd.DataFrame, htf_df: pd.DataFrame | None,
             direction: str, price: float) -> ConsensusResult:
    """Poll every concept about `direction` at `price`. Never raises.

    A detector that throws must not take the decision layer down with it -- one
    broken concept becoming a system-wide outage is exactly the failure this
    module exists to survive. Exceptions become abstentions, and the diagnosis
    says so.
    """
    votes: list[ConceptVote] = []
    want_bull = direction == "long"

    def guarded(concept: str, fn):
        try:
            votes.append(fn())
        except Exception as exc:
            votes.append(ConceptVote(concept, ABSTAIN,
                                     f"detector error: {type(exc).__name__}",
                                     WEIGHTS.get(concept, 0.5)))

    def _structure():
        from .structure import Trend, detect_trend, find_swing_points
        t = detect_trend(find_swing_points(df))
        if t == Trend.NEUTRAL:
            return _vote("structure", None, "", "", "trend is neutral")
        ok = (t == Trend.BULLISH) if want_bull else (t == Trend.BEARISH)
        return _vote("structure", ok, f"trend {t.value} agrees",
                     f"trend {t.value} opposes")

    def _mitigation():
        from .mitigation import active_mitigation
        m = active_mitigation(df, price, direction)
        return _vote("mitigation", m is not None,
                     f"in respected zone {m.bottom:.4g}-{m.top:.4g}" if m else "",
                     "entry is not in a respected mitigation zone")

    def _breaker():
        from .breaker import active_breaker
        b = active_breaker(df, price, direction)
        return _vote("breaker", b is not None,
                     f"retested zone (was {b.origin_direction}, now {b.direction})" if b else "",
                     "no retested breaker at this price")

    def _candle():
        from .candles import confirms
        p = confirms(df, direction)
        return _vote("candle", p is not None,
                     f"{p.name} {p.direction} (strength {p.strength:.2f})" if p else "",
                     "no confirming candle on the recent bars")

    def _wyckoff():
        from .wyckoff import analyse
        st = analyse(df)
        if st.trading_range is None:
            return _vote("wyckoff", None, "", "", "no trading range (trending)")
        if st.direction == "neutral":
            return _vote("wyckoff", None, "", "",
                         "range rejected both sides; genuinely no signal")
        ok = (st.direction == "bullish") if want_bull else (st.direction == "bearish")
        return _vote("wyckoff", ok, f"{st.bias} bias agrees",
                     f"{st.bias} bias opposes")

    def _volume_profile():
        from .volume_profile import at_value_area_edge, build_profile
        if "volume" not in df.columns:
            return _vote("volume_profile", None, "", "", "feed has no volume column")
        if build_profile(df) is None:
            return _vote("volume_profile", None, "", "", "profile could not be built")
        edge = at_value_area_edge(df, price)
        want = "low" if want_bull else "high"
        if edge is None:
            return _vote("volume_profile", False, "",
                         "entry is mid-value-area (fair value, no edge)")
        return _vote("volume_profile", edge == want,
                     f"at value-area {edge}", f"at value-area {edge}, wrong side")

    def _supply_demand():
        from .structure import find_swing_points
        from .supply_demand import detect_supply_demand_zones, nearest_zone
        zones = detect_supply_demand_zones(df, find_swing_points(df))
        want = "demand" if want_bull else "supply"
        z = nearest_zone(price, zones, want)
        return _vote("supply_demand", z is not None,
                     f"at a {want} zone" if z else "",
                     f"not at a {want} zone")

    def _premium_discount():
        from .structure import (find_swing_points, is_in_discount,
                                is_in_premium, premium_discount_zone)
        swings = find_swing_points(df)
        zone = premium_discount_zone(df, swings)
        if zone is None:
            return _vote("premium_discount", None, "", "", "no clean range")
        ok = is_in_discount(price, zone) if want_bull else is_in_premium(price, zone)
        side = "discount" if want_bull else "premium"
        return _vote("premium_discount", ok, f"price is in {side}",
                     f"price is NOT in {side}")

    def _liquidity_sweep():
        from .liquidity import detect_liquidity_pools, recent_sweep
        pools = detect_liquidity_pools(df)
        s = recent_sweep(pools, df)
        want = "sell_side" if want_bull else "buy_side"
        if s is None:
            return _vote("liquidity_sweep", False, "", "no recent sweep")
        return _vote("liquidity_sweep", s.kind == want,
                     f"{s.kind} swept", f"{s.kind} swept (wanted {want})")

    def _fibonacci():
        from .fibonacci import in_ote, recent_leg
        from .structure import find_swing_points
        leg = recent_leg(find_swing_points(df), direction)
        if leg is None:
            return _vote("fibonacci", None, "", "", "no clean leg to measure")
        ok = in_ote(price, leg[0], leg[1])
        return _vote("fibonacci", ok, "entry in the OTE pocket",
                     "entry outside the OTE pocket")

    for name, fn in (("structure", _structure), ("mitigation", _mitigation),
                     ("breaker", _breaker), ("candle", _candle),
                     ("wyckoff", _wyckoff), ("volume_profile", _volume_profile),
                     ("supply_demand", _supply_demand),
                     ("premium_discount", _premium_discount),
                     ("liquidity_sweep", _liquidity_sweep),
                     ("fibonacci", _fibonacci)):
        guarded(name, fn)

    total_w = sum(v.weight for v in votes if v.verdict != ABSTAIN)
    score = (sum(v.signed for v in votes) / total_w) if total_w > 0 else 0.0
    return ConsensusResult(
        direction=direction, votes=votes, score=score,
        agreed=sum(1 for v in votes if v.verdict == AGREE),
        dissented=sum(1 for v in votes if v.verdict == DISAGREE),
        abstained=sum(1 for v in votes if v.verdict == ABSTAIN),
    )
