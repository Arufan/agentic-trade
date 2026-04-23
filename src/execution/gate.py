"""Pure decision functions for the entry-gate pipeline.

The live loop (src/main.py) used to inline gate rules, making it
impossible to unit-test. This module extracts the direction/confidence
portion of the gate (evaluate_entry_gate) plus a thin wrapper around the
economic-calendar blackout check (evaluate_event_gate) so we can lock
their rules with tests.

Contracts:
    evaluate_entry_gate(signal_action, signal_conf, ai_action, ai_conf,
                        min_confidence, ai_veto_threshold) -> GateDecision
    evaluate_event_gate(now, events, blackout_min,
                        currencies, impacts) -> GateDecision

Rules (evaluate_entry_gate, in order):
    1. signal_hold      — combined blend isn't tradable
    2. low_confidence   — combined conf below floor
    3. ai_veto_hold     — AI says HOLD with conf >= veto threshold
    4. ai_veto_opposite — AI says opposite direction with conf >= veto threshold
    5. allowed (reason="") — all checks pass

Rules (evaluate_event_gate):
    * allowed=False, reason="event_blackout" if any matching high-impact
      event is within [now, now + blackout_min).
    * allowed=True otherwise (including when blackout_min <= 0).

The live loop still owns downstream gates (risk, sizing, funding, order
placement); those don't belong in this pure module because they depend on
exchange/balance state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, Optional


Action = Literal["buy", "sell", "hold"]


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str  # empty string when allowed=True
    detail: str = ""  # optional human-readable context (e.g. event title)


def evaluate_entry_gate(
    signal_action: str,
    signal_conf: float,
    ai_action: str,
    ai_conf: float,
    min_confidence: float,
    ai_veto_threshold: float,
) -> GateDecision:
    """Evaluate the direction/confidence portion of the entry gate.

    Returns a GateDecision. When allowed is False, `reason` is a short
    machine-readable tag that matches the rejection_stats keys logged by
    the live loop (signal_hold / low_confidence / ai_veto_hold /
    ai_veto_opposite).
    """
    sa = str(signal_action or "").lower()
    aa = str(ai_action or "").lower()

    # Gate 1: combined signal must be a tradable direction.
    if sa not in ("buy", "sell"):
        return GateDecision(False, "signal_hold")

    # Gate 2: combined signal confidence must clear the floor.
    if float(signal_conf) < float(min_confidence):
        return GateDecision(False, "low_confidence")

    # Gate 3a: AI HOLD only vetoes when confident enough.
    if aa == "hold" and float(ai_conf) >= float(ai_veto_threshold):
        return GateDecision(False, "ai_veto_hold")

    # Gate 3b: AI opposite direction only vetoes when confident enough.
    if (
        aa in ("buy", "sell")
        and aa != sa
        and float(ai_conf) >= float(ai_veto_threshold)
    ):
        return GateDecision(False, "ai_veto_opposite")

    return GateDecision(True, "")


def evaluate_event_gate(
    now: datetime,
    events: Iterable,
    blackout_min: int,
    currencies: Iterable = ("USD",),
    impacts: Iterable = ("High",),
) -> GateDecision:
    """Gate new entries inside the macro-event blackout window.

    Thin wrapper around econ_calendar.is_in_blackout so main.py doesn't
    need to import the calendar helpers directly at the gate site.

    Returns:
        GateDecision(False, "event_blackout", detail="<event title>")
            if any matching high-impact event is within blackout_min.
        GateDecision(True, "") otherwise.

    Kept as a separate function (rather than folded into evaluate_entry_gate)
    because its inputs — a list of events + a wall-clock — are totally
    different from the tactical signal/AI inputs. Composing separately
    keeps both sides testable in isolation.
    """
    # Local import to avoid module-load order coupling.
    from src.strategy.econ_calendar import is_in_blackout

    if blackout_min <= 0:
        return GateDecision(True, "")
    in_blackout, ev = is_in_blackout(
        now=now,
        events=list(events),
        blackout_min=int(blackout_min),
        currencies=currencies,
        impacts=impacts,
    )
    if in_blackout and ev is not None:
        detail = f"{ev.currency} {ev.title} @ {ev.timestamp_utc.isoformat()}"
        return GateDecision(False, "event_blackout", detail)
    return GateDecision(True, "")
