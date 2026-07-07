from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.mt5.client import SymbolInfo


def calc_position_size(
    balance: float,
    entry: float,
    stop_loss: float,
    risk_pct: float = 1.0,
) -> float:
    """Calculate position size based on fixed fractional risk."""
    risk_amount = balance * (risk_pct / 100)
    risk_per_unit = abs(entry - stop_loss)

    if risk_per_unit == 0 or entry == 0:
        return 0.0

    size = risk_amount / risk_per_unit
    max_size = balance / entry  # cap notional exposure to available balance
    return min(size, max_size)


def calc_lot_size(
    info: "SymbolInfo",
    balance: float,
    entry: float,
    stop_loss: float,
    risk_pct: float = 1.0,
) -> float:
    """Fixed-fractional lot sizing for an MT5 symbol.

    Converts a risk budget (``balance * risk_pct%``) into lots using the
    symbol's tick economics: the loss on 1.0 lot if the stop is hit is
    ``(stop_distance / tick_size) * tick_value``. The result is floored to the
    broker's ``volume_step`` and clamped to ``volume_max``. If the risk-correct
    size falls below ``volume_min``, returns ``0.0`` (skip) rather than
    over-risk the account by rounding up to the minimum lot.
    """
    stop_distance = abs(entry - stop_loss)
    if (
        stop_distance == 0
        or info.tick_size == 0
        or info.tick_value == 0
        or info.volume_step == 0
    ):
        return 0.0

    risk_amount = balance * (risk_pct / 100)
    loss_per_lot = (stop_distance / info.tick_size) * info.tick_value
    if loss_per_lot <= 0:
        return 0.0

    lots = risk_amount / loss_per_lot

    # Floor to the broker's volume step. Round the step count to 6 decimals
    # first so floating-point noise (e.g. 99.99999999 for an exact 100) doesn't
    # truncate a whole step off; a genuine 99.9999 still floors to 99.
    steps = math.floor(round(lots / info.volume_step, 6))
    lots = round(steps * info.volume_step, 8)

    if lots < info.volume_min:
        return 0.0
    return min(lots, info.volume_max)
