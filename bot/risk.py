from __future__ import annotations


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
