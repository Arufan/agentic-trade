from dataclasses import dataclass
from enum import Enum

from tavily import TavilyClient

from config import settings
from src.utils.logger import logger


class Sentiment(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass
class SentimentResult:
    sentiment: Sentiment
    confidence: float  # 0.0 to 1.0
    summary: str
    sources: list[str]


def analyze_sentiment(query: str) -> SentimentResult:
    """Search for crypto news and analyze market sentiment."""
    if not settings.TAVILY_API_KEY:
        logger.warning("Tavily API key not set, skipping sentiment analysis")
        return SentimentResult(
            sentiment=Sentiment.NEUTRAL,
            confidence=0.0,
            summary="Sentiment analysis skipped (no API key)",
            sources=[],
        )

    client = TavilyClient(api_key=settings.TAVILY_API_KEY)

    try:
        results = client.search(
            query=f"{query} crypto news market analysis",
            topic="news",
            max_results=5,
        )
    except Exception as e:
        logger.error(f"Tavily search failed: {e}")
        return SentimentResult(
            sentiment=Sentiment.NEUTRAL,
            confidence=0.0,
            summary=f"Search failed: {e}",
            sources=[],
        )

    # Simple keyword-based sentiment scoring
    bullish_keywords = ["surge", "rally", "bullish", "breakout", "gain", "pump", "moon", "uptrend", "adoption", "positive"]
    bearish_keywords = ["crash", "dump", "bearish", "decline", "fall", "drop", "fear", "sell-off", "hack", "ban", "regulation"]

    bullish_count = 0
    bearish_count = 0
    summaries = []
    sources = []

    for result in results.get("results", []):
        text = (result.get("title", "") + " " + result.get("content", "")).lower()
        summaries.append(result.get("title", ""))
        sources.append(result.get("url", ""))

        for kw in bullish_keywords:
            if kw in text:
                bullish_count += 1

        for kw in bearish_keywords:
            if kw in text:
                bearish_count += 1

    total = bullish_count + bearish_count
    if total == 0:
        sentiment = Sentiment.NEUTRAL
        confidence = 0.0
    elif bullish_count > bearish_count:
        sentiment = Sentiment.BULLISH
        confidence = bullish_count / (total + 1)
    elif bearish_count > bullish_count:
        sentiment = Sentiment.BEARISH
        confidence = bearish_count / (total + 1)
    else:
        sentiment = Sentiment.NEUTRAL
        confidence = 0.3

    summary = " | ".join(summaries[:3])
    logger.info(f"Sentiment for {query}: {sentiment.value} (confidence: {confidence:.2f})")

    return SentimentResult(
        sentiment=sentiment,
        confidence=confidence,
        summary=summary,
        sources=sources,
    )
