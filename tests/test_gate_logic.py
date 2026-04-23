"""Unit tests for the entry-gate decision logic.

Locks in the Phase 2 rewire: combined signal is PRIMARY, AI is advisory
and only vetoes at high confidence. Prevents regressions where AI HOLD
silently re-overrides strong tech setups (the bug behind 8335 holds / 0
orders in the 24h live-test log).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.execution.gate import evaluate_entry_gate, evaluate_event_gate, GateDecision
from src.strategy.econ_calendar import EconEvent


# --------------------------------------------------------------------------- #
#  Defaults mirror production (settings.MIN_CONFIDENCE=0.7, veto=0.80)        #
# --------------------------------------------------------------------------- #

MIN_CONF = 0.70
VETO = 0.80


# --------------------------------------------------------------------------- #
#  Gate 1: signal_hold                                                        #
# --------------------------------------------------------------------------- #

def test_signal_hold_is_rejected():
    g = evaluate_entry_gate("hold", 0.99, "buy", 0.99, MIN_CONF, VETO)
    assert g.allowed is False
    assert g.reason == "signal_hold"


def test_signal_garbage_is_rejected_as_hold():
    # Defensive: any non-buy/sell string should fall into signal_hold.
    g = evaluate_entry_gate("", 0.99, "buy", 0.99, MIN_CONF, VETO)
    assert g.allowed is False
    assert g.reason == "signal_hold"


# --------------------------------------------------------------------------- #
#  Gate 2: low_confidence                                                     #
# --------------------------------------------------------------------------- #

def test_low_confidence_is_rejected():
    g = evaluate_entry_gate("buy", 0.69, "buy", 0.5, MIN_CONF, VETO)
    assert g.allowed is False
    assert g.reason == "low_confidence"


def test_exact_min_confidence_passes_gate_2():
    # Floor check is strict <, so exactly-at-threshold passes. This is
    # deliberate — MIN_CONFIDENCE is a soft floor, not a hard inequality.
    g = evaluate_entry_gate("buy", MIN_CONF, "buy", 0.5, MIN_CONF, VETO)
    assert g.allowed is True
    assert g.reason == ""


# --------------------------------------------------------------------------- #
#  Gate 3a: ai_veto_hold — AI HOLD only vetoes at high confidence             #
# --------------------------------------------------------------------------- #

def test_ai_hold_at_low_conf_does_NOT_veto():
    """The core Phase-2 bug: AI HOLD at 0.5 conf used to silently block
    a strong tech signal. New behaviour: AI HOLD below veto threshold
    is informational only."""
    g = evaluate_entry_gate("buy", 0.85, "hold", 0.50, MIN_CONF, VETO)
    assert g.allowed is True
    assert g.reason == ""


def test_ai_hold_at_high_conf_DOES_veto():
    g = evaluate_entry_gate("buy", 0.85, "hold", 0.90, MIN_CONF, VETO)
    assert g.allowed is False
    assert g.reason == "ai_veto_hold"


def test_ai_hold_exactly_at_veto_threshold_vetoes():
    # >= is strict; exactly-at counts as veto. Prevents off-by-epsilon bugs.
    g = evaluate_entry_gate("buy", 0.85, "hold", VETO, MIN_CONF, VETO)
    assert g.allowed is False
    assert g.reason == "ai_veto_hold"


def test_ai_hold_just_below_veto_does_NOT_veto():
    g = evaluate_entry_gate("buy", 0.85, "hold", VETO - 0.01, MIN_CONF, VETO)
    assert g.allowed is True


# --------------------------------------------------------------------------- #
#  Gate 3b: ai_veto_opposite — AI opposite dir only vetoes at high conf       #
# --------------------------------------------------------------------------- #

def test_ai_opposite_at_low_conf_does_NOT_veto():
    g = evaluate_entry_gate("buy", 0.85, "sell", 0.40, MIN_CONF, VETO)
    assert g.allowed is True


def test_ai_opposite_at_high_conf_DOES_veto_long():
    g = evaluate_entry_gate("buy", 0.85, "sell", 0.90, MIN_CONF, VETO)
    assert g.allowed is False
    assert g.reason == "ai_veto_opposite"


def test_ai_opposite_at_high_conf_DOES_veto_short():
    g = evaluate_entry_gate("sell", 0.85, "buy", 0.95, MIN_CONF, VETO)
    assert g.allowed is False
    assert g.reason == "ai_veto_opposite"


def test_ai_same_direction_at_any_conf_passes():
    # If AI agrees, there's nothing to veto regardless of its own confidence.
    g = evaluate_entry_gate("buy", 0.85, "buy", 0.30, MIN_CONF, VETO)
    assert g.allowed is True
    g = evaluate_entry_gate("sell", 0.85, "sell", 0.99, MIN_CONF, VETO)
    assert g.allowed is True


# --------------------------------------------------------------------------- #
#  Ordering: earlier gates win                                                #
# --------------------------------------------------------------------------- #

def test_signal_hold_short_circuits_before_low_confidence():
    """If signal is HOLD, we should report signal_hold even if conf is low too."""
    g = evaluate_entry_gate("hold", 0.10, "buy", 0.99, MIN_CONF, VETO)
    assert g.reason == "signal_hold"


def test_low_confidence_short_circuits_before_ai_veto():
    """AI veto should never be inspected if the signal itself failed the floor.
    Prevents misattribution of rejections during ops triage."""
    g = evaluate_entry_gate("buy", 0.40, "hold", 0.95, MIN_CONF, VETO)
    assert g.reason == "low_confidence"


# --------------------------------------------------------------------------- #
#  Veto threshold = 1.0 effectively disables AI override                      #
# --------------------------------------------------------------------------- #

def test_veto_threshold_one_disables_ai_override_hold():
    # At threshold=1.0, even 0.99 AI conf can't veto → allow-through.
    g = evaluate_entry_gate("buy", 0.85, "hold", 0.99, MIN_CONF, 1.0)
    assert g.allowed is True


def test_veto_threshold_one_disables_ai_override_opposite():
    g = evaluate_entry_gate("buy", 0.85, "sell", 0.99, MIN_CONF, 1.0)
    assert g.allowed is True


# --------------------------------------------------------------------------- #
#  Happy path                                                                 #
# --------------------------------------------------------------------------- #

def test_strong_long_with_concurring_ai_passes():
    g = evaluate_entry_gate("buy", 0.82, "buy", 0.75, MIN_CONF, VETO)
    assert g.allowed is True
    assert g.reason == ""


def test_strong_short_with_concurring_ai_passes():
    g = evaluate_entry_gate("sell", 0.92, "sell", 0.85, MIN_CONF, VETO)
    assert g.allowed is True


def test_returns_gate_decision_dataclass():
    g = evaluate_entry_gate("buy", 0.85, "buy", 0.70, MIN_CONF, VETO)
    assert isinstance(g, GateDecision)
    assert g.allowed is True


# --------------------------------------------------------------------------- #
#  Case-insensitivity + None resilience                                        #
# --------------------------------------------------------------------------- #

def test_uppercase_actions_are_normalised():
    g = evaluate_entry_gate("BUY", 0.85, "HOLD", 0.50, MIN_CONF, VETO)
    assert g.allowed is True


def test_none_ai_action_is_treated_as_hold():
    # AI dict may carry None for action if JSON parse returned defaults.
    g = evaluate_entry_gate("buy", 0.85, None, 0.0, MIN_CONF, VETO)  # type: ignore[arg-type]
    assert g.allowed is True  # None → "none" → not a veto


# --------------------------------------------------------------------------- #
#  evaluate_event_gate — macro-event blackout (Phase 9)                       #
#                                                                             #
#  Thin wrapper around econ_calendar.is_in_blackout. Tests pin the contract   #
#  used by the live loop (src/main.py): "reject new entries within            #
#  blackout_min of any matching high-impact USD event".                       #
# --------------------------------------------------------------------------- #

NOW = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)


def _ev(minutes_from_now: float, title: str = "FOMC Rate Decision",
        currency: str = "USD", impact: str = "High") -> EconEvent:
    return EconEvent(
        timestamp_utc=NOW + timedelta(minutes=minutes_from_now),
        currency=currency, title=title, impact=impact,
    )


def test_event_gate_no_events_allows():
    g = evaluate_event_gate(NOW, [], blackout_min=30)
    assert g.allowed is True
    assert g.reason == ""


def test_event_gate_blackout_min_zero_disables():
    # Operator can disable the gate by setting ECON_BLACKOUT_MIN=0.
    # Even events 1 minute away should pass.
    g = evaluate_event_gate(NOW, [_ev(1)], blackout_min=0)
    assert g.allowed is True


def test_event_gate_event_inside_blackout_rejects():
    # FOMC in 15 min with 30-min blackout → REJECT.
    g = evaluate_event_gate(NOW, [_ev(15)], blackout_min=30)
    assert g.allowed is False
    assert g.reason == "event_blackout"
    assert "FOMC" in g.detail
    assert "USD" in g.detail


def test_event_gate_event_at_blackout_boundary_rejects():
    # Inclusive upper bound (<=). Event exactly at T+30 min with 30-min
    # window → still rejected. Prevents off-by-one on the edge case.
    g = evaluate_event_gate(NOW, [_ev(30)], blackout_min=30)
    assert g.allowed is False
    assert g.reason == "event_blackout"


def test_event_gate_event_just_past_blackout_allows():
    g = evaluate_event_gate(NOW, [_ev(31)], blackout_min=30)
    assert g.allowed is True


def test_event_gate_event_in_the_past_allows():
    # Past events never trigger the forward-looking blackout.
    g = evaluate_event_gate(NOW, [_ev(-5)], blackout_min=30)
    assert g.allowed is True


def test_event_gate_filters_by_currency():
    # EUR event inside the window should NOT reject a USD-track bot.
    eur = _ev(15, title="ECB Rate Decision", currency="EUR")
    g = evaluate_event_gate(NOW, [eur], blackout_min=30, currencies=["USD"])
    assert g.allowed is True


def test_event_gate_filters_by_impact():
    # Medium impact inside the window should NOT reject a High-only config.
    med = _ev(15, title="CPI y/y", impact="Medium")
    g = evaluate_event_gate(NOW, [med], blackout_min=30, impacts=["High"])
    assert g.allowed is True


def test_event_gate_filters_non_whitelisted_titles():
    # Event within window + USD + High, but title not in HIGH_IMPACT_TITLES
    # (no FOMC/CPI/NFP/etc keyword) → not counted as market-moving.
    junk = _ev(10, title="Fed Loan Officer Survey")
    g = evaluate_event_gate(NOW, [junk], blackout_min=30)
    assert g.allowed is True


def test_event_gate_picks_soonest_matching_when_multiple():
    # Three events — the soonest inside the window should be surfaced.
    events = [_ev(25, title="NFP"), _ev(10, title="CPI m/m"), _ev(45, title="PCE")]
    g = evaluate_event_gate(NOW, events, blackout_min=30)
    assert g.allowed is False
    assert g.reason == "event_blackout"
    # The CPI (10 min away) is soonest; FOMC in 25 is still inside window
    # but further out. is_in_blackout returns the earliest.
    assert "CPI" in g.detail


def test_event_gate_accepts_tuple_currencies_and_impacts():
    # Production passes a list/tuple; both should work.
    g = evaluate_event_gate(
        NOW, [_ev(10)], blackout_min=30,
        currencies=("USD",), impacts=("High",),
    )
    assert g.allowed is False


def test_event_gate_returns_gate_decision_dataclass():
    g = evaluate_event_gate(NOW, [_ev(10)], blackout_min=30)
    assert isinstance(g, GateDecision)
    assert g.detail != ""  # populated on reject


def test_event_gate_allowed_has_empty_detail():
    g = evaluate_event_gate(NOW, [], blackout_min=30)
    assert isinstance(g, GateDecision)
    assert g.detail == ""
