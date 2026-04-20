"""OI (open-interest) anomaly detector.

Classic perp-flow analytics relates OI delta to price delta over the same
window to classify the move:

    price ↑ + OI ↑   → strong uptrend   (new longs opening)     → BUY confirm
    price ↓ + OI ↑   → strong downtrend (new shorts opening)    → SELL confirm
    price ↑ + OI ↓   → weak uptrend     (short covering)        → FADE (SELL)
    price ↓ + OI ↓   → weak downtrend   (long liquidation)      → FADE (BUY)

A separate "build-up" case arises when OI jumps without price moving — that's
positions accumulating, usually before a breakout. On its own the direction
is ambiguous, so we emit HOLD and rely on tech/regime to pick the side.

Thresholds are relative (%) so they work across symbols regardless of OI
magnitude. Defaults are conservative; tune via settings.
"""

from __future__ import annotations

from typing import Optional

from src.strategy.alpha.base import AlphaSignal, AlphaSource
from src.strategy.technical import Signal


def detect_oi_anomaly(
    oi_pct: Optional[float],
    price_pct: Optional[float],
    oi_threshold: float = 0.10,
    price_threshold: float = 0.02,
    strong_oi_threshold: float = 0.15,
) -> AlphaSignal:
    """Classify an OI + price move into an alpha signal.

    Args:
        oi_pct:              OI % change over the lookback window (e.g. 0.15 = +15 %)
        price_pct:           price % change over the same window (e.g. 0.03 = +3 %)
        oi_threshold:        OI change magnitude that counts as "active flow" (0.10 = 10 %)
        price_threshold:     price change magnitude that counts as "moving" (0.02 = 2 %)
        strong_oi_threshold: OI change magnitude that counts as "strong flow" (0.15)

    Returns:
        AlphaSignal with source=OI_ANOMALY.

    Edge cases:
        - None inputs (insufficient history) → HOLD with strength 0.0
        - Both oi_pct and price_pct below threshold → HOLD (nothing interesting)
    """
    meta = {"oi_pct": oi_pct, "price_pct": price_pct}

    if oi_pct is None or price_pct is None:
        return AlphaSignal(
            source=AlphaSource.OI_ANOMALY,
            action=Signal.HOLD,
            strength=0.0,
            reasoning="insufficient OI history",
            metadata=meta,
        )

    oi_big = abs(oi_pct) >= oi_threshold
    price_big = abs(price_pct) >= price_threshold

    # --- Nothing interesting ---
    if not oi_big and not price_big:
        return AlphaSignal(
            source=AlphaSource.OI_ANOMALY,
            action=Signal.HOLD,
            strength=0.0,
            reasoning=f"quiet: OI {oi_pct*100:+.1f}%, price {price_pct*100:+.1f}%",
            metadata=meta,
        )

    # --- OI spike with no price move: accumulation, direction ambiguous ---
    # Ambiguous by construction → HOLD, but emit metadata so downstream
    # (tech layer) can up-weight when it sees the same build-up.
    if oi_big and not price_big:
        meta["pattern"] = "build_up"
        return AlphaSignal(
            source=AlphaSource.OI_ANOMALY,
            action=Signal.HOLD,
            strength=0.0,
            reasoning=f"position build-up: OI {oi_pct*100:+.1f}% with price flat {price_pct*100:+.1f}%",
            metadata=meta,
        )

    # --- Price moved without OI: retail fomo, not real flow — weak / HOLD ---
    if price_big and not oi_big:
        return AlphaSignal(
            source=AlphaSource.OI_ANOMALY,
            action=Signal.HOLD,
            strength=0.0,
            reasoning=f"price move without OI: {price_pct*100:+.1f}% price, {oi_pct*100:+.1f}% OI",
            metadata=meta,
        )

    # --- Both moved: classify by OI direction (positions opening vs closing) ---
    # OI UP   = new positions opening → confirmation signal
    # OI DOWN = positions closing     → fade signal (counter-trend reversal setup)
    is_confirm = oi_pct > 0

    # Scale strength on the STRONGER of the two normalized magnitudes, capped.
    oi_norm = min(abs(oi_pct) / strong_oi_threshold, 1.0)
    price_norm = min(abs(price_pct) / (price_threshold * 3), 1.0)
    strength = max(oi_norm, price_norm)
    # Fades (positions closing) bet AGAINST ongoing flow → higher-risk, dampened.
    if not is_confirm:
        strength *= 0.8
    strength = min(max(strength, 0.0), 1.0)

    if oi_pct > 0 and price_pct > 0:
        # price up + OI up → new longs opening, confirm breakout long
        meta["pattern"] = "long_build"
        return AlphaSignal(
            source=AlphaSource.OI_ANOMALY,
            action=Signal.BUY,
            strength=strength,
            reasoning=f"long build-up: price {price_pct*100:+.1f}% OI {oi_pct*100:+.1f}%",
            metadata=meta,
        )

    if oi_pct > 0 and price_pct < 0:
        # price down + OI up → new shorts opening, confirm breakdown
        meta["pattern"] = "short_build"
        return AlphaSignal(
            source=AlphaSource.OI_ANOMALY,
            action=Signal.SELL,
            strength=strength,
            reasoning=f"short build-up: price {price_pct*100:+.1f}% OI {oi_pct*100:+.1f}%",
            metadata=meta,
        )

    if oi_pct < 0 and price_pct > 0:
        # price up + OI down → shorts covering (closing), NOT organic buying → fade
        meta["pattern"] = "short_cover"
        return AlphaSignal(
            source=AlphaSource.OI_ANOMALY,
            action=Signal.SELL,
            strength=strength,
            reasoning=f"short cover (unsustainable): price {price_pct*100:+.1f}% OI {oi_pct*100:+.1f}%",
            metadata=meta,
        )

    # price down + OI down → longs liquidating (closing), capitulation → fade long
    meta["pattern"] = "long_liquidation"
    return AlphaSignal(
        source=AlphaSource.OI_ANOMALY,
        action=Signal.BUY,
        strength=strength,
        reasoning=f"long liquidation (capitulation): price {price_pct*100:+.1f}% OI {oi_pct*100:+.1f}%",
        metadata=meta,
    )
