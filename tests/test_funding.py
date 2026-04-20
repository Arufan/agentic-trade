"""Unit tests for src/strategy/funding.py — covers the allow / penalize / skip
branches for both BUY and SELL sides, plus favorable-funding edge cases."""

from __future__ import annotations

import math

from src.strategy.funding import evaluate_funding, HOURS_PER_YEAR


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _rate_for_annual(annual: float) -> float:
    """Convert an annualized funding figure (e.g. 0.30) into the equivalent
    per-hour rate used by Hyperliquid."""
    return annual / HOURS_PER_YEAR


# --------------------------------------------------------------------------- #
# Favorable / neutral cases                                                   #
# --------------------------------------------------------------------------- #

def test_favorable_funding_for_long_is_allow():
    # Negative funding = shorts pay longs → going long is free money
    d = evaluate_funding(_rate_for_annual(-0.50), "BUY")
    assert d.action == "allow"
    assert d.size_modifier == 1.0


def test_favorable_funding_for_short_is_allow():
    # Positive funding = longs pay shorts → going short gets paid
    d = evaluate_funding(_rate_for_annual(0.50), "SELL")
    assert d.action == "allow"
    assert d.size_modifier == 1.0


def test_zero_funding_is_allow():
    d = evaluate_funding(0.0, "BUY")
    assert d.action == "allow"
    assert d.size_modifier == 1.0
    assert d.annualized == 0.0


def test_hold_signal_is_allow_regardless():
    d = evaluate_funding(_rate_for_annual(0.80), "HOLD")
    assert d.action == "allow"
    assert d.size_modifier == 1.0


# --------------------------------------------------------------------------- #
# Penalize band (extreme_annual ≤ |annualized| < skip_annual)                 #
# --------------------------------------------------------------------------- #

def test_mild_adverse_funding_still_allow_long():
    # 10 % annualized adverse, below the 30 % threshold
    d = evaluate_funding(_rate_for_annual(0.10), "BUY",
                         extreme_annual=0.30, skip_annual=0.60)
    assert d.action == "allow"
    assert d.size_modifier == 1.0


def test_elevated_adverse_funding_penalizes_long():
    d = evaluate_funding(_rate_for_annual(0.40), "BUY",
                         extreme_annual=0.30, skip_annual=0.60)
    assert d.action == "penalize"
    assert d.size_modifier == 0.5


def test_elevated_adverse_funding_penalizes_short():
    d = evaluate_funding(_rate_for_annual(-0.40), "SELL",
                         extreme_annual=0.30, skip_annual=0.60)
    assert d.action == "penalize"
    assert d.size_modifier == 0.5


# --------------------------------------------------------------------------- #
# Skip band (|annualized| ≥ skip_annual)                                      #
# --------------------------------------------------------------------------- #

def test_extreme_adverse_funding_skips_long():
    d = evaluate_funding(_rate_for_annual(0.80), "BUY",
                         extreme_annual=0.30, skip_annual=0.60)
    assert d.action == "skip"
    assert d.size_modifier == 0.0


def test_extreme_adverse_funding_skips_short():
    d = evaluate_funding(_rate_for_annual(-0.80), "SELL",
                         extreme_annual=0.30, skip_annual=0.60)
    assert d.action == "skip"
    assert d.size_modifier == 0.0


# --------------------------------------------------------------------------- #
# Annualization math                                                          #
# --------------------------------------------------------------------------- #

def test_annualization_is_rate_times_hours_per_year():
    d = evaluate_funding(0.0001, "BUY")
    assert math.isclose(d.annualized, 0.0001 * 24 * 365)


def test_threshold_boundary_inclusive_on_extreme():
    # Exactly at extreme threshold → penalize (>= triggers)
    d = evaluate_funding(_rate_for_annual(0.30), "BUY",
                         extreme_annual=0.30, skip_annual=0.60)
    assert d.action == "penalize"


def test_threshold_boundary_inclusive_on_skip():
    d = evaluate_funding(_rate_for_annual(0.60), "BUY",
                         extreme_annual=0.30, skip_annual=0.60)
    assert d.action == "skip"
