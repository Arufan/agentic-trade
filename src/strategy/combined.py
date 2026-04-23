from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config import settings
from src.strategy.technical import Signal, TechnicalSignal, analyze_technical
from src.strategy.sentiment import Sentiment, SentimentResult, analyze_sentiment
from src.strategy.regime import (
    Regime, Bias, RegimeResult, AiRegimeResult, BlendedRegimeResult,
    detect_regime, blend_regimes,
)
from src.strategy.alpha import CombinedAlpha
from src.strategy.chop import ChopResult, evaluate_chop
from src.strategy.levels import KeyLevelResult
from src.utils.logger import logger


@dataclass
class CombinedSignal:
    action: Signal
    confidence: float
    technical: TechnicalSignal
    sentiment: SentimentResult
    regime: RegimeResult
    blended_regime: BlendedRegimeResult | None
    alpha: Optional[CombinedAlpha]
    reasoning: str
    # Higher-TF key-levels summary. Optional — backtests / tests may
    # skip the daily-fetch plumbing. When present, the engine's
    # bias_score nudges the combined score BEFORE the ±0.3 threshold.
    levels: Optional[KeyLevelResult] = None
    # Strategy mode tells the live loop WHY this signal fired:
    #   "trend" — default trend-follow blend
    #   "chop"  — mean-reversion fallback in sideways regime
    # The live loop uses this to apply chop-specific sizing + SL/TP hints.
    strategy_mode: str = "trend"
    chop: Optional[ChopResult] = None


def generate_signal(
    df: pd.DataFrame,
    symbol: str,
    df_regime: pd.DataFrame | None = None,
    ai_regime: AiRegimeResult | None = None,
    tech_weight: float | None = None,
    sent_weight: float | None = None,
    alpha_weight: float | None = None,
    alpha: Optional[CombinedAlpha] = None,
    levels: Optional[KeyLevelResult] = None,
    levels_weight: float | None = None,
) -> CombinedSignal:
    """Combine technical, sentiment, regime, and alpha analysis into a final signal.

    Args:
        df: 1H dataframe for execution signals.
        symbol: Trading pair (e.g. "BTC/USDC").
        df_regime: Optional 4H dataframe for regime detection. Falls back to df.
        ai_regime: Optional AI macro regime (computed once per cycle).
        tech_weight: Weight for technical score (defaults derived from SENTIMENT_WEIGHT + ALPHA_WEIGHT).
        sent_weight: Weight for sentiment score (defaults to settings.SENTIMENT_WEIGHT).
        alpha_weight: Weight for alpha score (defaults to settings.ALPHA_WEIGHT).
        alpha: Optional pre-computed CombinedAlpha (from AlphaEngine.evaluate). The
            live loop computes this before calling generate_signal so it has
            access to the market_state store; backtests / tests can pass None
            to skip the alpha layer.
    """
    # Default weights from settings. Clamp to sensible bounds.
    if sent_weight is None:
        sent_weight = max(0.0, min(0.5, float(getattr(settings, "SENTIMENT_WEIGHT", 0.15))))
    if alpha_weight is None:
        alpha_weight = max(0.0, min(0.5, float(getattr(settings, "ALPHA_WEIGHT", 0.25))))

    # If no alpha supplied (or none of its sub-modules fired), zero out its
    # weight and give the slack back to tech. This keeps backwards-compat:
    # when the caller doesn't pass alpha, behaviour matches the old two-way
    # blend.
    effective_alpha_weight = alpha_weight if (alpha is not None and alpha.has_any()) else 0.0

    if tech_weight is None:
        # Tech takes whatever is left; floor at 0.4 so it always dominates if
        # a user over-configures high sentiment + alpha weights.
        tech_weight = max(0.4, 1.0 - sent_weight - effective_alpha_weight)

    technical = analyze_technical(df)
    sentiment = analyze_sentiment(symbol)

    # Use 4H for regime if provided, otherwise use 1H df
    regime_df = df_regime if df_regime is not None and len(df_regime) >= 60 else df
    tech_regime = detect_regime(regime_df, symbol=symbol, use_persistence=True)

    # Blend technical + AI regime
    blended = blend_regimes(tech_regime, ai_regime)

    # --- Scores: signed floats in [-1, 1] ---
    tech_score = 0.0
    if technical.signal == Signal.BUY:
        tech_score = technical.strength
    elif technical.signal == Signal.SELL:
        tech_score = -technical.strength

    sent_score = 0.0
    if sentiment.sentiment == Sentiment.BULLISH:
        sent_score = sentiment.confidence
    elif sentiment.sentiment == Sentiment.BEARISH:
        sent_score = -sentiment.confidence

    alpha_score = alpha.score if alpha is not None else 0.0

    # Key-levels contribute a bounded ±1 bias that nudges the combined
    # score toward bounce-off-support / reject-at-resistance setups. The
    # default weight (0.1) is modest — levels confirm, they don't drive.
    if levels_weight is None:
        levels_weight = float(getattr(settings, "LEVELS_WEIGHT", 0.10))
    levels_weight = max(0.0, min(0.5, levels_weight))
    levels_score = float(levels.bias_score) if levels is not None else 0.0
    effective_levels_weight = levels_weight if levels is not None else 0.0

    combined = (
        tech_score * tech_weight
        + sent_score * sent_weight
        + alpha_score * effective_alpha_weight
        + levels_score * effective_levels_weight
    )

    if combined >= 0.3:
        action = Signal.BUY
    elif combined <= -0.3:
        action = Signal.SELL
    else:
        action = Signal.HOLD

    confidence = min(abs(combined), 1.0)

    # Blended regime bias
    regime = blended.regime
    if regime == Regime.BULL:
        if action == Signal.BUY:
            confidence *= 1.15
        elif action == Signal.SELL:
            confidence *= 0.7
    elif regime == Regime.BEAR:
        if action == Signal.SELL:
            confidence *= 1.15
        elif action == Signal.BUY:
            confidence *= 0.7

    # AI bias override: risk_off → penalize all longs
    if blended.ai_bias == Bias.RISK_OFF and action == Signal.BUY:
        confidence *= 0.8
    elif blended.ai_bias == Bias.RISK_ON and action == Signal.SELL:
        confidence *= 0.8

    # Volatility dampening: reduce confidence in low-vol (choppy) regimes
    if blended.volatility_score < 0.4:
        confidence *= 0.8

    confidence = min(confidence, 1.0)

    # --- Chop fallback: when the primary blend says HOLD and we're in a
    # sideways regime, give the mean-reversion engine a chance to fire.
    # This is the "edge handler" for chop that the live-test log showed
    # we desperately need — 8k holds vs 0 trades otherwise.
    chop_result: Optional[ChopResult] = None
    strategy_mode = "trend"
    chop_enabled = bool(getattr(settings, "CHOP_ENABLED", True))
    is_sideways = regime == Regime.SIDEWAYS
    if chop_enabled and is_sideways and action == Signal.HOLD:
        try:
            chop_min = float(getattr(settings, "CHOP_MIN_STRENGTH", 0.55))
            chop_result = evaluate_chop(df, levels=levels, min_strength=chop_min)
            if chop_result.is_tradable:
                # Chop sizing is always smaller (handled in risk layer);
                # confidence here reflects the chop strength directly.
                action = chop_result.action
                confidence = min(1.0, chop_result.strength)
                strategy_mode = "chop"
                logger.info(
                    f"Chop fallback fired for {symbol}: {action.value} "
                    f"strength={confidence:.2f} — {chop_result.reasoning}"
                )
        except Exception as e:
            logger.warning(f"Chop evaluation failed for {symbol}: {e}")

    alpha_part = ""
    if alpha is not None:
        alpha_part = (
            f"Alpha: {alpha.action.value} (strength={alpha.strength:.2f} score={alpha.score:+.2f}), "
        )

    levels_part = ""
    if levels is not None:
        levels_part = f"Levels: {levels.reasoning}, "

    reasoning = (
        f"Technical: {technical.signal.value} (strength={technical.strength:.2f}), "
        f"Sentiment: {sentiment.sentiment.value} (confidence={sentiment.confidence:.2f}), "
        f"{alpha_part}"
        f"{levels_part}"
        f"Regime: {regime.value} (score={blended.confidence:.2f} vol={blended.volatility_score:.0%}), "
        f"AI bias: {blended.ai_bias.value}, "
        f"Combined score: {combined:.2f}"
    )

    logger.info(
        f"Combined signal for {symbol}: {action.value} (confidence={confidence:.2f}) "
        f"[regime={regime.value} ai_bias={blended.ai_bias.value}"
        + (f" alpha={alpha.action.value}({alpha.score:+.2f})" if alpha is not None else "")
        + "]"
    )

    return CombinedSignal(
        action=action,
        confidence=confidence,
        technical=technical,
        sentiment=sentiment,
        regime=tech_regime,
        blended_regime=blended,
        alpha=alpha,
        reasoning=reasoning,
        levels=levels,
        strategy_mode=strategy_mode,
        chop=chop_result,
    )
