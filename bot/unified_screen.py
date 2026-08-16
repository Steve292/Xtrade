"""
Unified screening gate — smart money combined with the existing SMC/
Fibonacci seven-gate screener, at explicit user request: "use the smart
money system as a main gated system crossing into fibonacci and the smc
signal all as a screening medium before a final summary on %, provided
market structure is organized properly."

Two layers, BOTH required — this adds a gate, it doesn't replace one:

1. "Market structure organized properly" = the EXISTING seven-gate
   TradeScreener result (bot/screening.py), unchanged: SMC confluence,
   top-down alignment, liquidity sweep, risk/reward, sniper entry,
   Supply/Demand, and Fibonacci OTE (run last, by that module's own design).

2. Smart money (bot/smart_money.py's 9-module aggregate, including the
   regulatory-news signal) must not ACTIVELY CONTRADICT the signal's side.
   NEUTRAL doesn't block — in practice the aggregate reads NEUTRAL or
   split most passes (see bot/smart_money.py's own documented asymmetry:
   a bearish read can only ever draw on 4 of 9 modules), so requiring
   strict same-direction agreement would block nearly everything, which
   isn't what "screening medium" was asking for. An outright opposite call
   (smart money BEARISH while the signal is LONG, or vice versa) does block.

final_pct is a blend of the SMC signal's own confidence and how many of the
9 smart-money modules actually agree with the signal's side — an honest
"how many independent systems point the same way" summary, not just the
seven-gate screener's confidence relabeled.

Smart money here is the GLOBAL/market-wide read (bot/market_snapshot.py's
BTC-centric aggregate, the same one the dashboard already shows), not
recomputed per-symbol — CVD/GEX/stablecoin-flow/divergence are inherently
market-wide concepts (see bot/smart_money.py), and recomputing a full
per-symbol version for every coin on every pass would multiply this
project's Yahoo/CoinGecko/Deribit calls far past what those free tiers
tolerate. This is a deliberate scope decision, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.screening import ScreenResult
from bot.smc.strategy import Signal, SignalType

_TOTAL_SMART_MONEY_MODULES = 9  # bot/smart_money.py's 8 + regulatory_news


@dataclass
class UnifiedResult:
    approved: bool
    final_pct: float  # 0-100
    structure_ok: bool
    smart_money_ok: bool
    smart_money_direction: str
    smart_money_agreement_count: int
    reason: str


def evaluate_unified(
    signal: Signal,
    screen_result: ScreenResult,
    smart_money_direction: str,
    smart_money_bullish_count: int,
    smart_money_bearish_count: int,
) -> UnifiedResult:
    structure_ok = screen_result.approved

    if signal.type == SignalType.LONG:
        agreement_count = smart_money_bullish_count
        smart_money_ok = smart_money_direction != "BEARISH"
    elif signal.type == SignalType.SHORT:
        agreement_count = smart_money_bearish_count
        smart_money_ok = smart_money_direction != "BULLISH"
    else:
        agreement_count = 0
        smart_money_ok = False

    smart_money_agreement_pct = (agreement_count / _TOTAL_SMART_MONEY_MODULES) * 100
    final_pct = (signal.confidence * 100 + smart_money_agreement_pct) / 2
    approved = structure_ok and smart_money_ok

    if not structure_ok:
        reason = "market structure gates not cleared"
    elif not smart_money_ok:
        reason = f"smart money contradicts signal (reads {smart_money_direction})"
    else:
        reason = "structure and smart money aligned"

    return UnifiedResult(
        approved=approved,
        final_pct=final_pct,
        structure_ok=structure_ok,
        smart_money_ok=smart_money_ok,
        smart_money_direction=smart_money_direction,
        smart_money_agreement_count=agreement_count,
        reason=reason,
    )
