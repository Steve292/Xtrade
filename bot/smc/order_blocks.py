from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class OrderBlock:
    index: int
    direction: str  # "bullish" | "bearish"
    top: float
    bottom: float
    mitigated: bool = False


def detect_raw_order_blocks(
    df: pd.DataFrame, impulse_threshold: float = 0.005
) -> list[OrderBlock]:
    """Every order block in the frame, before the recency window and the
    mitigation filter are applied.

    Split out of detect_order_blocks() so bot/smc/breaker.py can reach the
    blocks that were BROKEN — the ones detect_order_blocks() deliberately
    drops, and exactly the ones a breaker block is made of. Behaviour of
    detect_order_blocks() is unchanged.
    """
    blocks: list[OrderBlock] = []
    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values

    for i in range(1, len(df) - 3):
        # Bullish impulse: strong up move after a down candle
        future_move = (closes[i + 2] - closes[i]) / closes[i]
        if future_move >= impulse_threshold and closes[i] < opens[i]:
            blocks.append(
                OrderBlock(
                    index=i,
                    direction="bullish",
                    top=float(highs[i]),
                    bottom=float(lows[i]),
                )
            )

        # Bearish impulse: strong down move after an up candle
        if future_move <= -impulse_threshold and closes[i] > opens[i]:
            blocks.append(
                OrderBlock(
                    index=i,
                    direction="bearish",
                    top=float(highs[i]),
                    bottom=float(lows[i]),
                )
            )

    return blocks


def detect_order_blocks(
    df: pd.DataFrame, lookback: int = 20, impulse_threshold: float = 0.005
) -> list[OrderBlock]:
    """
    Detect order blocks: last opposing candle before a strong impulsive move.
    Bullish OB = last bearish candle before bullish impulse.
    Bearish OB = last bullish candle before bearish impulse.
    """
    blocks = detect_raw_order_blocks(df, impulse_threshold)
    # Keep only recent, unmitigated blocks
    recent = blocks[-lookback:]
    return _mark_mitigated(df, recent)


def _mark_mitigated(df: pd.DataFrame, blocks: list[OrderBlock]) -> list[OrderBlock]:
    active: list[OrderBlock] = []
    for block in blocks:
        future = df.iloc[block.index + 1 :]
        if block.direction == "bullish":
            touched = (future["low"] <= block.top).any()
            broken = (future["close"] < block.bottom).any()
        else:
            touched = (future["high"] >= block.bottom).any()
            broken = (future["close"] > block.top).any()

        block.mitigated = bool(broken)
        if touched and not broken:
            active.append(block)

    return active


def price_in_order_block(price: float, block: OrderBlock) -> bool:
    return block.bottom <= price <= block.top
