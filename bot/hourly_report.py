"""
Prose SMC analysis for the hourly report -- explanation and expectation, not
just a pass/fail line.

The live loop's own log is deliberately terse: one line per symbol per poll,
built for grepping (bot/runner.py's `No signal — {reason}` / `SIGNAL {type}
conf {c}% final {f}% rejected at screen: {gate}`). That is right for a 30s
poll cycle but wrong for something a person reads once an hour -- it names
WHAT failed without explaining what it means or what would need to change.

This module is a pure formatter: everything it needs is passed in as already-
computed values (a SymbolSnapshot), so it has no MT5/network dependency and is
fully testable. scripts/hourly_analysis.py is what gathers the snapshot and
calls this.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SymbolSnapshot:
    symbol: str
    price: float
    bar_time: str
    ltf_label: str
    htf_label: str
    ltf_trend: str  # "bullish" | "bearish" | "neutral"
    htf_trend: str
    ltf_last_event: str | None  # e.g. "BOS bullish, 4 bars ago", or None
    htf_last_event: str | None
    sweep: str | None  # e.g. "sell_side @ 78,940 (6 bars ago)", or None
    ote_band: tuple | None  # (low, high) of the golden pocket for the active leg direction, if any
    ote_direction: str | None  # "bullish" | "bearish" -- which leg the OTE band above is for
    signal_type: str  # "long" | "short" | "none"
    signal_reason: str
    confidence: float = 0.0
    entry: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    gate_checks: list = field(default_factory=list)  # [(name, passed, detail), ...]
    final_pct: float | None = None
    knowledge_pct: float | None = None
    auto_fire_pct: float = 100.0
    approved: bool = False
    # Candlestick-only read (bot/smc/candles.py), independent of the SMC
    # confluence stack above -- no zones, no sweeps, no fib. Wick/close
    # geometry only, checked on both timeframes so the report can say whether
    # they actually agree with each other, which the SMC-side numbers alone
    # don't surface. None on any field means the data wasn't available (e.g.
    # too few bars); the section is omitted rather than printed with gaps.
    ltf_candle_kind: str | None = None
    ltf_candle_strength: float | None = None
    ltf_candle_bias: str | None = None  # "bullish" | "bearish" | "neutral"
    ltf_candle_bias_strength: float | None = None
    htf_candle_kind: str | None = None
    htf_candle_strength: float | None = None
    htf_candle_bias: str | None = None
    htf_candle_bias_strength: float | None = None


def _price_vs_ote(price: float, band: tuple | None) -> str | None:
    if not band:
        return None
    lo, hi = band
    if price < lo:
        return f"below the pocket ({lo:,.2f}-{hi:,.2f}) by {lo - price:,.2f}"
    if price > hi:
        return f"above the pocket ({lo:,.2f}-{hi:,.2f}) by {price - hi:,.2f}"
    return f"INSIDE the pocket ({lo:,.2f}-{hi:,.2f})"


def _expectation(snap: SymbolSnapshot) -> str:
    """What would need to happen for this symbol to move toward a trade.
    Built from the same structural facts the gates check, not from the
    generic reason text -- this is the forward-looking half."""
    if snap.signal_type == "none":
        # Defer to the strategy's own diagnosis rather than re-deriving one.
        # An earlier version guessed from a fixed priority order (HTF neutral
        # -> LTF disagreement -> no sweep -> no zone) and got it wrong: on a
        # bar where HTF read neutral but the REAL blocker was a missing
        # supply/demand zone, it reported "HTF neutral blocks everything" --
        # a plausible-sounding sentence that didn't match what the strategy
        # actually computed. signal_reason IS that computation
        # (SMCStrategy._diagnose); repeating it can't diverge from it.
        return snap.signal_reason

    # There IS a signal. Point at the first failing gate, if any.
    failing = next((g for g in snap.gate_checks if not g[1]), None)
    if failing is None:
        gap = (snap.auto_fire_pct - snap.final_pct) if snap.final_pct is not None else None
        if gap is not None and gap > 0:
            return (f"Every gate has cleared. final_pct ({snap.final_pct:.1f}%) is "
                    f"{gap:.1f} points under the {snap.auto_fire_pct:.0f}% auto-fire line, "
                    f"so this would queue for approval rather than fire on its own if it "
                    f"holds through the next poll.")
        return "Every gate has cleared and the score clears auto-fire — this would execute."

    name, _, detail = failing
    asks = {
        "Liquidity sweep": "needs a confirmed stop-hunt in this direction within the recency window",
        "Supply/Demand": "needs price to be sitting inside a demand/supply zone in this direction",
        "Fibonacci OTE (final)": "needs price to retrace into the 0.618-0.786 golden pocket",
        "Market structure shift": "needs a fresh BOS/CHoCH in this direction",
        "Sniper entry": "needs either higher confluence or a tighter stop to clear the ceiling",
        "Top-down alignment": "needs the HTF bias to stop opposing this direction",
        "Risk/reward": "needs a better entry/stop relationship to clear the minimum R:R",
    }
    ask = asks.get(name, f"needs '{name}' to clear ({detail})")
    return f"Blocked on {name}: {ask}."


def build_symbol_report(snap: SymbolSnapshot) -> str:
    """One symbol's section of the hourly report, as prose."""
    lines = [f"{snap.symbol}  —  {snap.price:,.2f}  (bar {snap.bar_time})", ""]

    lines.append(f"  {snap.htf_label} bias:  {snap.htf_trend.upper()}"
                 + (f"  (last: {snap.htf_last_event})" if snap.htf_last_event else ""))
    lines.append(f"  {snap.ltf_label} structure:  {snap.ltf_trend.upper()}"
                 + (f"  (last: {snap.ltf_last_event})" if snap.ltf_last_event else ""))
    lines.append(f"  Liquidity sweep:  {snap.sweep or 'none confirmed recently'}")
    if snap.ote_band:
        lines.append(f"  Fib OTE ({snap.ote_direction}):  "
                     f"{_price_vs_ote(snap.price, snap.ote_band)}")

    if snap.ltf_candle_bias is not None and snap.htf_candle_bias is not None:
        lines.append("")
        lines.append(f"  Candlesticks only (no zones/sweeps/fib):")
        lines.append(f"    {snap.ltf_label}: {snap.ltf_candle_kind}"
                     f" (strength {snap.ltf_candle_strength:.2f})  |  "
                     f"3-bar bias {snap.ltf_candle_bias} ({snap.ltf_candle_bias_strength:.2f})")
        lines.append(f"    {snap.htf_label}: {snap.htf_candle_kind}"
                     f" (strength {snap.htf_candle_strength:.2f})  |  "
                     f"3-bar bias {snap.htf_candle_bias} ({snap.htf_candle_bias_strength:.2f})")
        correspond = (snap.ltf_candle_bias == snap.htf_candle_bias
                     and snap.ltf_candle_bias != "neutral")
        lines.append(f"    Correspond: {'YES — ' + snap.ltf_candle_bias.upper() + ' on both' if correspond else 'NO'}")

    lines.append("")
    if snap.signal_type == "none":
        lines.append(f"  No setup — {snap.signal_reason}")
    else:
        lines.append(f"  Signal: {snap.signal_type.upper()}  "
                     f"conf {snap.confidence:.0%}  "
                     f"entry {snap.entry:,.2f}  stop {snap.stop:,.2f}  tp {snap.take_profit:,.2f}")
        for name, passed, detail in snap.gate_checks:
            lines.append(f"    [{'PASS' if passed else 'FAIL'}] {name:<24} {detail}")
        if snap.final_pct is not None:
            verdict = ("FIRES unattended" if snap.approved and snap.final_pct >= snap.auto_fire_pct
                       else "queues for approval" if snap.approved else "rejected")
            kn = f", knowledge {snap.knowledge_pct:.0f}%" if snap.knowledge_pct is not None else ""
            lines.append(f"    final_pct {snap.final_pct:.1f}%{kn} -> {verdict}")

    lines.append("")
    lines.append(f"  Expectation: {_expectation(snap)}")
    return "\n".join(lines)


def build_report(snapshots: list, generated_at: str) -> str:
    """The full hourly report: a header plus one section per symbol."""
    header = f"{'='*70}\n  Hourly SMC scan — {generated_at}\n{'='*70}\n"
    body = "\n\n".join(build_symbol_report(s) for s in snapshots)
    return f"{header}\n{body}\n"
