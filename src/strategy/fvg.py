"""Fair Value Gap (FVG) and Inverse FVG (IFVG) detection and scoring.

FVG = 3-candle pattern where candle[0] and candle[2] don't overlap,
      leaving an "imbalance" zone that price tends to retrace to.

Bullish FVG: candle[0].high < candle[2].low  (gap up → support zone on retrace)
Bearish FVG: candle[0].low  > candle[2].high (gap down → resistance zone on retrace)

IFVG = an FVG that has been filled (price crossed through the gap).
       The zone flips role: former support becomes resistance and vice versa.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.utils.logger import logger


@dataclass
class FVGZone:
    top: float          # upper boundary of the gap
    bottom: float       # lower boundary of the gap
    gap_type: str       # "bullish" or "bearish"
    filled: bool        # whether price has crossed through
    mid_idx: int        # index of the middle candle (candle[1])


def detect_fvgs(df: pd.DataFrame, lookback: int = 20) -> list[FVGZone]:
    """Scan last N 3-candle sequences for FVGs.

    Returns list of FVGZone sorted by recency (newest first).
    """
    if len(df) < lookback + 2:
        return []

    zones = []
    start = max(0, len(df) - lookback)

    for i in range(start + 2, len(df)):
        # 3-candle pattern: [i-2, i-1, i]
        c0_high = df["high"].iloc[i - 2]
        c0_low = df["low"].iloc[i - 2]
        c2_high = df["high"].iloc[i]
        c2_low = df["low"].iloc[i]

        # Bullish FVG: gap between c0.high and c2.low
        if c0_high < c2_low:
            zones.append(FVGZone(
                top=c2_low,
                bottom=c0_high,
                gap_type="bullish",
                filled=False,
                mid_idx=i - 1,
            ))

        # Bearish FVG: gap between c2.high and c0.low
        elif c0_low > c2_high:
            zones.append(FVGZone(
                top=c0_low,
                bottom=c2_high,
                gap_type="bearish",
                filled=False,
                mid_idx=i - 1,
            ))

    # Check fill status: if any candle after the FVG closes inside the gap, it's filled
    for zone in zones:
        for j in range(zone.mid_idx + 2, len(df)):
            candle_close = df["close"].iloc[j]
            if zone.bottom <= candle_close <= zone.top:
                zone.filled = True
                break

    return zones


def price_in_zone(price: float, zone: FVGZone, tolerance_pct: float = 0.0) -> bool:
    """Check if price is inside a zone with optional tolerance (percentage of zone width)."""
    if zone.top == zone.bottom:
        return False
    width = zone.top - zone.bottom
    margin = width * tolerance_pct
    return (zone.bottom - margin) <= price <= (zone.top + margin)


def fvg_score(df: pd.DataFrame) -> tuple[float, float, dict]:
    """Calculate FVG/IFVG-based buy and sell scores plus metadata.

    Returns (buy_score, sell_score, meta_dict).
    Max +3 per direction from FVG, +2 from IFVG.

    Scoring:
      Price in bullish FVG (unfilled) → +3 BUY  (expect bounce up from support)
      Price in bearish FVG (unfilled)  → +3 SELL (expect rejection from resistance)
      Price near bearish IFVG (filled) → +2 BUY  (former resistance = support)
      Price near bullish IFVG (filled) → +2 SELL (former support = resistance)
    """
    buy_score = 0.0
    sell_score = 0.0

    zones = detect_fvgs(df, lookback=20)
    price = df["close"].iloc[-1]

    unfilled_bullish = [z for z in zones if z.gap_type == "bullish" and not z.filled]
    unfilled_bearish = [z for z in zones if z.gap_type == "bearish" and not z.filled]
    filled_bullish = [z for z in zones if z.gap_type == "bullish" and z.filled]  # IFVG bearish
    filled_bearish = [z for z in zones if z.gap_type == "bearish" and z.filled]  # IFVG bullish

    # Price in unfilled bullish FVG → expect bounce up
    for z in unfilled_bullish:
        if price_in_zone(price, z):
            buy_score += 3
            logger.info(f"FVG: price {price:.2f} in bullish FVG [{z.bottom:.2f}-{z.top:.2f}]")
            break  # only count the most relevant

    # Price in unfilled bearish FVG → expect rejection down
    for z in unfilled_bearish:
        if price_in_zone(price, z):
            sell_score += 3
            logger.info(f"FVG: price {price:.2f} in bearish FVG [{z.bottom:.2f}-{z.top:.2f}]")
            break

    # Price near filled bearish FVG (IFVG bullish support) → expect bounce up
    for z in filled_bearish:
        if price_in_zone(price, z, tolerance_pct=0.5):
            buy_score += 2
            logger.info(f"IFVG: price {price:.2f} near filled bearish FVG (support) [{z.bottom:.2f}-{z.top:.2f}]")
            break

    # Price near filled bullish FVG (IFVG bearish resistance) → expect rejection down
    for z in filled_bullish:
        if price_in_zone(price, z, tolerance_pct=0.5):
            sell_score += 2
            logger.info(f"IFVG: price {price:.2f} near filled bullish FVG (resistance) [{z.bottom:.2f}-{z.top:.2f}]")
            break

    meta = {
        "fvg_bullish": len(unfilled_bullish),
        "fvg_bearish": len(unfilled_bearish),
        "ifvg_bullish": len(filled_bearish),  # filled bearish = bullish IFVG
        "ifvg_bearish": len(filled_bullish),  # filled bullish = bearish IFVG
        "fvg_signal": "none",
    }

    if buy_score > 0 and sell_score == 0:
        meta["fvg_signal"] = "in bullish FVG/IFVG"
    elif sell_score > 0 and buy_score == 0:
        meta["fvg_signal"] = "in bearish FVG/IFVG"

    return buy_score, sell_score, meta
