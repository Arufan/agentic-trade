"""Alpha-extraction modules.

Unlike the technical layer (public indicators, priced-in edge) or the
sentiment layer (lagging headlines), modules in this package look at
*flow* and *positioning* data: open-interest deltas, funding-rate
extremes, liquidation magnets. These are meant to generate trades that
the indicator-only layer would miss — not just gate them.

Public API:
    AlphaSignal            — single alpha module output (action + strength)
    CombinedAlpha          — orchestrator output (one per symbol)
    AlphaEngine            — runs all modules, combines outputs
    detect_oi_anomaly      — Fase 2 module
    detect_funding_contrarian — Fase 3 module
"""

from src.strategy.alpha.base import AlphaSignal, AlphaSource, CombinedAlpha
from src.strategy.alpha.oi_anomaly import detect_oi_anomaly
from src.strategy.alpha.funding_contrarian import detect_funding_contrarian
from src.strategy.alpha.engine import AlphaEngine

__all__ = [
    "AlphaSignal",
    "AlphaSource",
    "CombinedAlpha",
    "AlphaEngine",
    "detect_oi_anomaly",
    "detect_funding_contrarian",
]
