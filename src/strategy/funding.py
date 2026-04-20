"""Funding-rate filter for perpetual futures.

Idea: on a perp, the *funding rate* is paid from one side of the book to the
other every hour (on Hyperliquid). When longs are crowded, funding goes
positive and longs pay shorts; that's a bearish tell even if price has been
rising. This module converts the per-hour rate to an annualized figure and
decides whether to allow, penalize, or outright skip a trade.

Example:
    rate = 0.0001 per hour = 0.01 % / h
    annualized ≈ 0.0001 * 24 * 365 ≈ 0.876 ( 87.6 % )

Thresholds are in annualized terms so they read naturally:
    FUNDING_EXTREME_ANNUAL = 0.30 → 30 % yearly cost to be on the wrong side
    FUNDING_SKIP_ANNUAL    = 0.60 → 60 % = outright skip

The filter only penalizes when funding is *against* the signal direction:
    long  + positive funding  → crowded longs, penalize
    short + negative funding  → crowded shorts, penalize
The mirror cases (funding pays you to take the trade) are left alone — we
don't want to *boost* size on a free-money signal from a noisy indicator.
"""

from __future__ import annotations

from dataclasses import dataclass


HOURS_PER_YEAR = 24 * 365


@dataclass
class FundingDecision:
    """Outcome of the funding filter for one trade.

    Attributes:
        rate_1h:       raw per-hour rate reported by the exchange
        annualized:    rate_1h * 24 * 365
        action:        "allow" | "penalize" | "skip"
        size_modifier: multiply notional by this (1.0 / 0.5 / 0.0)
        reason:        human-readable explanation for logs
    """
    rate_1h: float
    annualized: float
    action: str
    size_modifier: float
    reason: str


def evaluate_funding(
    rate_1h: float,
    signal_side: str,
    extreme_annual: float = 0.30,
    skip_annual: float = 0.60,
) -> FundingDecision:
    """Decide whether funding cost should affect this trade.

    Args:
        rate_1h:        Hyperliquid per-hour funding rate (e.g. 0.0001).
        signal_side:    "BUY" / "SELL" / "HOLD".
        extreme_annual: annualized threshold that halves the size.
        skip_annual:    annualized threshold that blocks the trade.

    Returns:
        FundingDecision with action and size modifier.
    """
    annualized = rate_1h * HOURS_PER_YEAR
    side = (signal_side or "").upper()

    # HOLD — doesn't matter, but return a neutral decision so callers can log.
    if side not in ("BUY", "SELL"):
        return FundingDecision(
            rate_1h=rate_1h, annualized=annualized,
            action="allow", size_modifier=1.0,
            reason="no directional signal",
        )

    # Is funding pointing against our intended direction?
    #   BUY  + positive funding → against us (longs pay shorts)
    #   SELL + negative funding → against us (shorts pay longs)
    adverse = (side == "BUY" and annualized > 0) or \
              (side == "SELL" and annualized < 0)
    abs_ann = abs(annualized)

    if not adverse:
        return FundingDecision(
            rate_1h=rate_1h, annualized=annualized,
            action="allow", size_modifier=1.0,
            reason=f"funding favorable ({annualized*100:.2f}% annual)",
        )

    if abs_ann >= skip_annual:
        return FundingDecision(
            rate_1h=rate_1h, annualized=annualized,
            action="skip", size_modifier=0.0,
            reason=f"funding extreme adverse ({annualized*100:.2f}% annual ≥ {skip_annual*100:.0f}%)",
        )

    if abs_ann >= extreme_annual:
        return FundingDecision(
            rate_1h=rate_1h, annualized=annualized,
            action="penalize", size_modifier=0.5,
            reason=f"funding elevated adverse ({annualized*100:.2f}% annual ≥ {extreme_annual*100:.0f}%)",
        )

    return FundingDecision(
        rate_1h=rate_1h, annualized=annualized,
        action="allow", size_modifier=1.0,
        reason=f"funding adverse but mild ({annualized*100:.2f}% annual)",
    )
