from dataclasses import dataclass

import pandas as pd

from src.strategy.technical import Signal, TechnicalSignal, analyze_technical
from src.strategy.sentiment import Sentiment, SentimentResult, analyze_sentiment
from src.utils.logger import logger


@dataclass
class CombinedSignal:
    action: Signal
    confidence: float
    technical: TechnicalSignal
    sentiment: SentimentResult
    reasoning: str


def generate_signal(df: pd.DataFrame, symbol: str, tech_weight: float = 0.6, sent_weight: float = 0.4) -> CombinedSignal:
    """Combine technical and sentiment analysis into a final signal."""
    technical = analyze_technical(df)
    sentiment = analyze_sentiment(symbol)

    # Score: technical [-1, 1] * weight + sentiment [-1, 1] * weight
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

    combined = (tech_score * tech_weight) + (sent_score * sent_weight)

    if combined >= 0.3:
        action = Signal.BUY
    elif combined <= -0.3:
        action = Signal.SELL
    else:
        action = Signal.HOLD

    confidence = min(abs(combined), 1.0)
    reasoning = (
        f"Technical: {technical.signal.value} (strength={technical.strength:.2f}), "
        f"Sentiment: {sentiment.sentiment.value} (confidence={sentiment.confidence:.2f}), "
        f"Combined score: {combined:.2f}"
    )

    logger.info(f"Combined signal for {symbol}: {action.value} (confidence={confidence:.2f})")

    return CombinedSignal(
        action=action,
        confidence=confidence,
        technical=technical,
        sentiment=sentiment,
        reasoning=reasoning,
    )
