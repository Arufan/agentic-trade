"""Dataclasses shared by every alpha module."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.strategy.technical import Signal


class AlphaSource(str, Enum):
    """Which flow-edge generated the signal. Add new values as modules land."""
    OI_ANOMALY = "oi_anomaly"
    FUNDING_CONTRARIAN = "funding_contrarian"
    # Reserved for future fases:
    ON_CHAIN_FLOW = "on_chain_flow"
    ORDER_BOOK = "order_book"


@dataclass
class AlphaSignal:
    """Output of a single alpha module.

    Attributes:
        source:     which module produced the signal
        action:     Signal.BUY / SELL / HOLD
        strength:   0.0–1.0 conviction from this module alone
        reasoning:  human-readable explanation for logs
        metadata:   free-form module-specific diagnostics (price pct, oi pct, etc.)
    """
    source: AlphaSource
    action: Signal
    strength: float
    reasoning: str
    metadata: dict = field(default_factory=dict)

    @property
    def score(self) -> float:
        """Signed score on [-1, 1]: + for BUY, - for SELL, 0 for HOLD."""
        if self.action == Signal.BUY:
            return self.strength
        if self.action == Signal.SELL:
            return -self.strength
        return 0.0


@dataclass
class CombinedAlpha:
    """Engine-level output: the blended view across all alpha modules for a
    single (symbol, bar) decision.

    `score` is the weighted average of individual module scores. Positive =
    net bullish, negative = net bearish, magnitude = combined conviction.
    """
    action: Signal
    strength: float
    score: float
    signals: list[AlphaSignal]
    reasoning: str

    def has_any(self) -> bool:
        """True if any sub-module emitted a non-HOLD signal."""
        return any(s.action != Signal.HOLD for s in self.signals)
