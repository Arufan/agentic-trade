"""AlphaEngine — runs every alpha module and blends their outputs.

Usage (from combined.py):
    engine = AlphaEngine()
    combined_alpha = engine.evaluate(
        symbol=symbol,
        current_price=price,
        current_oi=oi,
        funding_1h=funding,
        store=market_state_store,   # injected; tests can pass their own
    )
    # combined_alpha.score ∈ [-1, 1], blended into CombinedSignal.

The engine is stateless apart from its config; all history lives in
MarketStateStore. That separation makes it trivial to unit-test the engine
with a fake store, and keeps the live loop in charge of persistence cadence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.strategy.alpha.base import AlphaSignal, AlphaSource, CombinedAlpha
from src.strategy.alpha.oi_anomaly import detect_oi_anomaly
from src.strategy.alpha.funding_contrarian import detect_funding_contrarian
from src.strategy.technical import Signal
from src.utils.logger import logger


# Default lookback / threshold knobs. All overridable via config.
DEFAULT_OI_LOOKBACK_SEC: int = 4 * 3600        # 4 hours
DEFAULT_FUNDING_LOOKBACK_SEC: int = 12 * 3600  # 12 hours — enough to see a pump
DEFAULT_OI_THRESHOLD: float = 0.10              # 10 % OI change triggers
DEFAULT_PRICE_THRESHOLD: float = 0.02           # 2 % price change confirms
DEFAULT_FUNDING_CONTRARIAN_ANNUAL: float = 0.50 # 50 % annual = extreme
DEFAULT_FUNDING_MIN_PRICE_MOVE: float = 0.02    # 2 % price move to confirm squeeze


@dataclass
class AlphaEngineConfig:
    """All knobs live here so the live loop can construct one from settings
    and tests can construct one with explicit values."""
    # OI anomaly
    oi_lookback_sec: int = DEFAULT_OI_LOOKBACK_SEC
    oi_threshold: float = DEFAULT_OI_THRESHOLD
    price_threshold: float = DEFAULT_PRICE_THRESHOLD

    # Funding contrarian
    funding_lookback_sec: int = DEFAULT_FUNDING_LOOKBACK_SEC
    funding_contrarian_annual: float = DEFAULT_FUNDING_CONTRARIAN_ANNUAL
    funding_min_price_move: float = DEFAULT_FUNDING_MIN_PRICE_MOVE

    # Which modules are active
    enable_oi_anomaly: bool = True
    enable_funding_contrarian: bool = True

    # Per-module weights when blending (must be >0; unnormalised — engine
    # divides by sum of active weights). Modules that short-circuit to HOLD
    # don't consume weight.
    weights: dict[AlphaSource, float] = field(default_factory=lambda: {
        AlphaSource.OI_ANOMALY: 1.0,
        AlphaSource.FUNDING_CONTRARIAN: 1.0,
    })


class AlphaEngine:
    def __init__(self, config: Optional[AlphaEngineConfig] = None):
        self.config = config or AlphaEngineConfig()

    @classmethod
    def from_settings(cls, settings) -> "AlphaEngine":
        """Build an engine from the global Settings object. Missing attributes
        fall back to defaults — safe even when the project hasn't wired all
        knobs into settings yet."""
        cfg = AlphaEngineConfig(
            oi_lookback_sec=int(getattr(settings, "ALPHA_OI_LOOKBACK_SEC", DEFAULT_OI_LOOKBACK_SEC)),
            oi_threshold=float(getattr(settings, "ALPHA_OI_THRESHOLD", DEFAULT_OI_THRESHOLD)),
            price_threshold=float(getattr(settings, "ALPHA_PRICE_THRESHOLD", DEFAULT_PRICE_THRESHOLD)),
            funding_lookback_sec=int(getattr(settings, "ALPHA_FUNDING_LOOKBACK_SEC", DEFAULT_FUNDING_LOOKBACK_SEC)),
            funding_contrarian_annual=float(getattr(settings, "ALPHA_FUNDING_CONTRARIAN_ANNUAL", DEFAULT_FUNDING_CONTRARIAN_ANNUAL)),
            funding_min_price_move=float(getattr(settings, "ALPHA_FUNDING_MIN_PRICE_MOVE", DEFAULT_FUNDING_MIN_PRICE_MOVE)),
            enable_oi_anomaly=bool(getattr(settings, "ALPHA_OI_ENABLED", True)),
            enable_funding_contrarian=bool(getattr(settings, "ALPHA_FUNDING_CONTRARIAN_ENABLED", True)),
        )
        return cls(cfg)

    def evaluate(
        self,
        symbol: str,
        current_price: float,
        current_oi: float,
        funding_1h: float,
        store,
    ) -> CombinedAlpha:
        """Run every enabled alpha module and blend into a CombinedAlpha.

        Args:
            symbol:        trading pair (used as key into `store`)
            current_price: latest price
            current_oi:    latest open interest in base-asset units
            funding_1h:    latest per-hour funding rate
            store:         MarketStateStore-like object; must expose .delta(
                           symbol, field, lookback_sec) returning
                           (old, new, pct) or None.

        Returns:
            CombinedAlpha with blended action + aggregated diagnostics.
        """
        signals: list[AlphaSignal] = []

        # --- OI anomaly ---
        if self.config.enable_oi_anomaly:
            oi_delta = store.delta(symbol, "open_interest", self.config.oi_lookback_sec)
            price_delta = store.delta(symbol, "price", self.config.oi_lookback_sec)
            oi_pct = oi_delta[2] if oi_delta else None
            price_pct = price_delta[2] if price_delta else None
            sig = detect_oi_anomaly(
                oi_pct=oi_pct,
                price_pct=price_pct,
                oi_threshold=self.config.oi_threshold,
                price_threshold=self.config.price_threshold,
            )
            signals.append(sig)

        # --- Funding contrarian ---
        if self.config.enable_funding_contrarian:
            price_delta = store.delta(symbol, "price", self.config.funding_lookback_sec)
            price_pct = price_delta[2] if price_delta else None
            sig = detect_funding_contrarian(
                funding_1h=funding_1h,
                recent_price_pct=price_pct,
                extreme_annual=self.config.funding_contrarian_annual,
                min_price_move=self.config.funding_min_price_move,
            )
            signals.append(sig)

        # --- Blend ---
        if not signals:
            return CombinedAlpha(
                action=Signal.HOLD,
                strength=0.0,
                score=0.0,
                signals=[],
                reasoning="no alpha modules enabled",
            )

        # Weighted sum of signed scores. Modules that output HOLD contribute 0.
        weighted_score = 0.0
        total_weight = 0.0
        for sig in signals:
            w = self.config.weights.get(sig.source, 1.0)
            weighted_score += sig.score * w
            total_weight += w
        blended = weighted_score / total_weight if total_weight > 0 else 0.0

        # Map blended score back to action with a conservative threshold:
        # 0.25 keeps us from firing on a single weakly-held signal.
        if blended >= 0.25:
            action = Signal.BUY
        elif blended <= -0.25:
            action = Signal.SELL
        else:
            action = Signal.HOLD

        # Strength = magnitude of the blended score, capped.
        strength = min(abs(blended), 1.0)

        reason_parts = [
            f"{s.source.value}={s.action.value}({s.strength:.2f})"
            for s in signals
        ]
        reasoning = f"blended={blended:+.2f} from " + ", ".join(reason_parts)

        logger.info(f"Alpha {symbol}: {action.value} (strength={strength:.2f}) — {reasoning}")

        return CombinedAlpha(
            action=action,
            strength=strength,
            score=blended,
            signals=signals,
            reasoning=reasoning,
        )
