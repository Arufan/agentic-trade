"""Crypto news sentiment classifier.

Pipeline:
  1. Fetch recent news via Tavily (primary + backup key).
  2. Hand the headlines and snippets to an LLM classifier that
     returns a structured {sentiment, confidence, rationale}.
  3. If the LLM leg fails (no key, network, parse error), fall back
     to a conservative keyword counter so the bot never blocks on
     sentiment alone.

The default weight applied to sentiment in signal aggregation is
kept intentionally low (`settings.SENTIMENT_WEIGHT`) because
headline-based sentiment is noisy; it should nudge, not drive,
trade decisions.
"""

from __future__ import annotations

import json
import re
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


# --------------------------------------------------------------------------- #
#  Tavily search                                                              #
# --------------------------------------------------------------------------- #

def _try_tavily_search(api_key: str, query: str) -> dict | None:
    """Attempt a Tavily search with a given API key. Returns results or None."""
    try:
        client = TavilyClient(api_key=api_key)
        return client.search(
            query=f"{query} crypto news market analysis",
            topic="news",
            max_results=5,
        )
    except Exception as e:
        suffix = api_key[-6:] if api_key else "(none)"
        logger.warning(f"Tavily search failed with key ...{suffix}: {e}")
        return None


def _fetch_news(query: str) -> dict | None:
    """Primary → backup Tavily key, return raw result dict or None."""
    primary = settings.TAVILY_API_KEY
    backup = settings.TAVILY_API_KEY_BACKUP

    if not primary and not backup:
        logger.info("Tavily key not set, sentiment will be NEUTRAL")
        return None

    results = None
    if primary:
        results = _try_tavily_search(primary, query)
    if results is None and backup:
        logger.info("Falling back to backup Tavily API key")
        results = _try_tavily_search(backup, query)
    return results


# --------------------------------------------------------------------------- #
#  LLM classifier                                                             #
# --------------------------------------------------------------------------- #

_LLM_SYSTEM = (
    "You are a crypto market-sentiment classifier. "
    "Given a list of news headlines and snippets, decide whether the near-term "
    "(1-3 day) sentiment for the named coin is BULLISH, BEARISH, or NEUTRAL. "
    "Respond with STRICT JSON only: "
    '{"sentiment":"bullish|bearish|neutral","confidence":0..1,"rationale":"..."}.'
)


def _extract_json(text: str) -> dict | None:
    """Tolerant JSON extractor — finds the first balanced {...} blob."""
    # Fast path
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fenced ```json block?
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # First balanced {...}
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                blob = text[start:i + 1]
                try:
                    return json.loads(blob)
                except Exception:
                    start = None
    return None


def _classify_with_llm(coin: str, articles: list[dict]) -> SentimentResult | None:
    """Ask the configured LLM to classify sentiment. Returns None on failure."""
    api_key = getattr(settings, "LLM_API_KEY", None) or getattr(settings, "ANTHROPIC_API_KEY", None)
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed — skipping LLM sentiment")
        return None

    # Build a compact prompt (cap to 10 articles, 280 chars each)
    bullets = []
    for a in articles[:10]:
        title = (a.get("title") or "").strip()
        snippet = (a.get("content") or "").strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        bullets.append(f"- {title}: {snippet}")
    if not bullets:
        return None

    user_msg = (
        f"Coin: {coin}\n"
        f"Headlines ({len(bullets)}):\n" + "\n".join(bullets) + "\n\n"
        "Return JSON only."
    )

    try:
        client_kwargs = {"api_key": api_key}
        base_url = getattr(settings, "LLM_BASE_URL", None)
        if base_url:
            client_kwargs["base_url"] = base_url
        client = anthropic.Anthropic(**client_kwargs)
        resp = client.messages.create(
            model=getattr(settings, "LLM_MODEL", "claude-sonnet-4-6"),
            max_tokens=300,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
    except Exception as e:
        logger.warning(f"LLM sentiment call failed: {e}")
        return None

    data = _extract_json(raw)
    if not data:
        logger.warning(f"LLM sentiment returned unparseable text: {raw[:200]}")
        return None

    try:
        label = str(data.get("sentiment", "")).lower().strip()
        conf = float(data.get("confidence", 0.0))
    except Exception:
        return None

    if label not in {"bullish", "bearish", "neutral"}:
        return None
    conf = max(0.0, min(1.0, conf))

    summary = data.get("rationale") or ""
    summaries = [a.get("title", "") for a in articles[:3]]
    sources = [a.get("url", "") for a in articles if a.get("url")]

    logger.info(
        f"LLM sentiment for {coin}: {label} (conf={conf:.2f}) — {summary[:80]}"
    )
    return SentimentResult(
        sentiment=Sentiment(label),
        confidence=conf,
        summary=summary or " | ".join(s for s in summaries if s),
        sources=sources,
    )


# --------------------------------------------------------------------------- #
#  Keyword fallback                                                           #
# --------------------------------------------------------------------------- #

_BULL_KEYWORDS = (
    "surge", "rally", "bullish", "breakout", "gain", "pump", "moon",
    "uptrend", "adoption", "positive", "all-time high", "ath", "approve",
    "approval", "inflow", "etf inflow",
)
_BEAR_KEYWORDS = (
    "crash", "dump", "bearish", "decline", "fall", "drop", "fear", "sell-off",
    "hack", "ban", "regulation", "lawsuit", "liquidation", "outflow",
    "exploit", "rug",
)


def _classify_with_keywords(articles: list[dict]) -> SentimentResult:
    bull = bear = 0
    summaries: list[str] = []
    sources: list[str] = []

    for a in articles:
        text = ((a.get("title") or "") + " " + (a.get("content") or "")).lower()
        summaries.append(a.get("title", ""))
        url = a.get("url", "")
        if url:
            sources.append(url)
        bull += sum(1 for kw in _BULL_KEYWORDS if kw in text)
        bear += sum(1 for kw in _BEAR_KEYWORDS if kw in text)

    total = bull + bear
    if total == 0:
        sentiment = Sentiment.NEUTRAL
        confidence = 0.0
    elif bull > bear:
        sentiment = Sentiment.BULLISH
        confidence = bull / (total + 1)
    elif bear > bull:
        sentiment = Sentiment.BEARISH
        confidence = bear / (total + 1)
    else:
        sentiment = Sentiment.NEUTRAL
        confidence = 0.3

    # Cap keyword-fallback confidence — it's not very reliable.
    confidence = min(confidence, 0.6)

    return SentimentResult(
        sentiment=sentiment,
        confidence=round(confidence, 2),
        summary=" | ".join(s for s in summaries[:3] if s),
        sources=sources,
    )


# --------------------------------------------------------------------------- #
#  Public entry point                                                         #
# --------------------------------------------------------------------------- #

def analyze_sentiment(query: str) -> SentimentResult:
    """Search for crypto news and classify market sentiment.

    Strategy: LLM classifier over fetched headlines, with a keyword
    counter as a conservative fallback when the LLM leg is unavailable.
    """
    results = _fetch_news(query)
    if results is None:
        return SentimentResult(
            sentiment=Sentiment.NEUTRAL,
            confidence=0.0,
            summary="Sentiment unavailable (no news source)",
            sources=[],
        )

    articles = results.get("results", []) or []
    if not articles:
        return SentimentResult(
            sentiment=Sentiment.NEUTRAL,
            confidence=0.0,
            summary="No recent news returned",
            sources=[],
        )

    # 1) Try the LLM classifier.
    llm_res = _classify_with_llm(query, articles)
    if llm_res is not None:
        return llm_res

    # 2) Keyword fallback.
    res = _classify_with_keywords(articles)
    logger.info(
        f"Keyword sentiment for {query}: {res.sentiment.value} "
        f"(confidence: {res.confidence:.2f})"
    )
    return res
