"""Funding-rate contrarian alpha.

Distinct from `src/strategy/funding.py` (which is a size-down/skip FILTER on
the trade the tech layer already chose), this module GENERATES its own
contrarian signal when positioning looks extreme:

    extreme positive funding + recent price pump  → crowded longs → SELL (fade)
    extreme negative funding + recent price dump  → crowded shorts → BUY (fade)

Why both exist:
    - Filter runs on the trade you already want. If tech says BUY but funding
      is extreme adverse, it halves / skips. It never flips the direction.
    - Contrarian module says: even if tech is HOLD, a crowded book is itself
      an edge. The squeeze is the alpha.

Safety rails:
    - Require BOTH extreme funding AND a confirming recent price move. A
      standalone "funding is high" reading without price action is often
      just a stable carry trade, not a squeeze setup.
    - Strength scales with how extreme both are — 0.5 at the threshold,
      1.0 at 2x the threshold.
"""

from __future__ import annotations

from typing import Optional

from src.strategy.alpha.base import AlphaSignal, AlphaSource
from src.strategy.technical import Signal


HOURS_PER_YEAR = 24 * 365


def detect_funding_contrarian(
    funding_1h: float,
    recent_price_pct: Optional[float],
    extreme_annual: float = 0.50,
    min_price_move: float = 0.02,
) -> AlphaSignal:
    """Emit a contrarian signal if funding + price both indicate a crowded book.

    Args:
        funding_1h:       per-hour funding rate (0.0001 = +0.01 % / hour)
        recent_price_pct: price % change over a recent window (e.g. last 12–24h).
                          None if insufficient history → HOLD.
        extreme_annual:   annualized funding magnitude that qualifies as "extreme"
                          (0.50 = 50 %). Deliberately HIGHER than the filter's
                          skip threshold (0.60) by default — the filter's
                          purpose is risk, this module's is alpha, and we only
                          want alpha when positioning is truly lopsided.
        min_price_move:   minimum absolute price move (0.02 = 2 %) to confirm
                          the crowd has actually pushed the market.

    Returns:
        AlphaSignal with source=FUNDING_CONTRARIAN.
    """
    annualized = funding_1h * HOURS_PER_YEAR
    meta = {
        "funding_1h": funding_1h,
        "annualized": annualized,
        "price_pct": recent_price_pct,
    }

    if recent_price_pct is None:
        return AlphaSignal(
            source=AlphaSource.FUNDING_CONTRARIAN,
            action=Signal.HOLD,
            strength=0.0,
            reasoning="insufficient price history for confirmation",
            metadata=meta,
        )

    abs_ann = abs(annualized)
    if abs_ann < extreme_annual:
        return AlphaSignal(
            source=AlphaSource.FUNDING_CONTRARIAN,
            action=Signal.HOLD,
            strength=0.0,
            reasoning=f"funding not extreme ({annualized*100:.1f}% annual < {extreme_annual*100:.0f}%)",
            metadata=meta,
        )

    if abs(recent_price_pct) < min_price_move:
        return AlphaSignal(
            source=AlphaSource.FUNDING_CONTRARIAN,
            action=Signal.HOLD,
            strength=0.0,
            reasoning=(
                f"funding extreme ({annualized*100:.1f}%) but price flat "
                f"({recent_price_pct*100:+.2f}%) — no squeeze setup"
            ),
            metadata=meta,
        )

    # Strength: 0.5 at threshold, scales to 1.0 at 2× threshold.
    funding_norm = min((abs_ann - extreme_annual) / extreme_annual + 0.5, 1.0)
    price_norm = min(abs(recent_price_pct) / (min_price_move * 3) + 0.3, 1.0)
    strength = min(max(funding_norm, price_norm), 1.0)

    # Crowded longs + price pumped → fade the move (SELL)
    if annualized > 0 and recent_price_pct > 0:
        meta["pattern"] = "long_squeeze_setup"
        return AlphaSignal(
            source=AlphaSource.FUNDING_CONTRARIAN,
            action=Signal.SELL,
            strength=strength,
            reasoning=(
                f"crowded longs: funding {annualized*100:+.1f}% annual, "
                f"price {recent_price_pct*100:+.1f}% — fade"
            ),
            metadata=meta,
        )

    # Crowded shorts + price dumped → fade the move (BUY)
    if annualized < 0 and recent_price_pct < 0:
        meta["pattern"] = "short_squeeze_setup"
        return AlphaSignal(
            source=AlphaSource.FUNDING_CONTRARIAN,
            action=Signal.BUY,
            strength=strength,
            reasoning=(
                f"crowded shorts: funding {annualized*100:+.1f}% annual, "
                f"price {recent_price_pct*100:+.1f}% — fade"
            ),
            metadata=meta,
        )

    # Funding and price disagree on direction — carry trade or divergence,
    # no clear squeeze setup. HOLD.
    return AlphaSignal(
        source=AlphaSource.FUNDING_CONTRARIAN,
        action=Signal.HOLD,
        strength=0.0,
        reasoning=(
            f"funding/price mismatch: funding {annualized*100:+.1f}%, "
            f"price {recent_price_pct*100:+.1f}% — not a squeeze"
        ),
        metadata=meta,
    )
