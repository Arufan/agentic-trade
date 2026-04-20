"""AI agent wrapper for final trade decisions.

Supports any Anthropic-compatible endpoint (Claude, Z.AI GLM, OpenRouter, etc.)
via LLM_BASE_URL + LLM_MODEL env vars.
"""

import json

import anthropic

from config import settings
from src.strategy.combined import CombinedSignal
from src.utils.logger import logger


def _build_client() -> anthropic.Anthropic | None:
    """Build the LLM client, honouring LLM_BASE_URL if set."""
    api_key = settings.LLM_API_KEY or settings.ANTHROPIC_API_KEY
    if not api_key:
        return None
    kwargs: dict = {"api_key": api_key}
    if settings.LLM_BASE_URL:
        kwargs["base_url"] = settings.LLM_BASE_URL
    return anthropic.Anthropic(**kwargs)


def _extract_json(text: str) -> dict:
    """Robustly extract the first JSON object from an LLM reply."""
    text = text.strip()
    if "```" in text:
        # Pull out the first fenced block
        parts = text.split("```")
        if len(parts) >= 2:
            block = parts[1]
            if block.startswith("json"):
                block = block[4:]
            text = block.strip()
    # Fall back to braces scanning
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


class AIAgent:
    def __init__(self):
        self.client = _build_client()
        self.model = settings.LLM_MODEL
        if self.client is None:
            logger.warning("LLM API key not set — AIAgent will fall back to rule-based decisions")
        else:
            logger.info(f"AIAgent initialised (model={self.model}, base_url={settings.LLM_BASE_URL or 'default'})")

    def _rule_based(self, signal: CombinedSignal) -> dict:
        return {
            "action": signal.action.value,
            "confidence": signal.confidence,
            "reasoning": signal.reasoning,
            "amount_pct": 0,
        }

    def decide(self, signal: CombinedSignal, symbol: str, balance: dict, history_summary: str | None = None) -> dict:
        """Let AI make the final trading decision based on combined signals and context."""
        if self.client is None:
            return self._rule_based(signal)

        prompt = f"""You are a crypto trading AI agent on Hyperliquid perpetual futures. You can both LONG (buy) and SHORT (sell) with leverage. Shorting is just as easy as going long — you profit when price drops. Do NOT treat this like spot trading.

Decide whether to BUY (long), SELL (short), or HOLD.

Symbol: {symbol}
Account Balance: {balance.get('total', 0)} USDT (free: {balance.get('free', 0)})
With leverage, even small balance can open positions on any pair.

Technical Analysis:
- RSI: {signal.technical.indicators.get('rsi')}
- MACD Histogram: {signal.technical.indicators.get('macd_hist')}
- EMA 8: {signal.technical.indicators.get('ema_8')}
- EMA 21: {signal.technical.indicators.get('ema_21')}
- EMA 55: {signal.technical.indicators.get('ema_55')}
- ADX: {signal.technical.indicators.get('adx')}
- ATR: {signal.technical.indicators.get('atr')}
- Bollinger Bands: {signal.technical.indicators.get('bb_lower')} - {signal.technical.indicators.get('bb_upper')}
- FVG: {signal.technical.indicators.get('fvg_signal')} (bullish zones: {signal.technical.indicators.get('fvg_bullish')}, bearish zones: {signal.technical.indicators.get('fvg_bearish')}, IFVG bullish: {signal.technical.indicators.get('ifvg_bullish')}, IFVG bearish: {signal.technical.indicators.get('ifvg_bearish')})
- Price: {signal.technical.indicators.get('price')}

Technical Signal: {signal.technical.signal.value} (strength: {signal.technical.strength:.2f})
Sentiment: {signal.sentiment.sentiment.value} (confidence: {signal.sentiment.confidence:.2f})
News Summary: {signal.sentiment.summary}

Market Regime: {signal.regime.regime.value.upper()} (confidence: {signal.regime.score:.2f})
  - Trend: {signal.regime.trend_score:+.1f} | Momentum: {signal.regime.momentum_score:+.1f} | Structure: {signal.regime.structure_score:+.1f}
  - IMPORTANT: Align trades with the regime. In BULL markets, prefer BUY (long). In BEAR markets, prefer SELL (short). In SIDEWAYS, be defensive and reduce position size.

"""
        if history_summary:
            prompt += f"\n=== Your Recent Performance ===\n{history_summary}\n"

        prompt += """Respond with a SINGLE JSON object only (no prose, no code fence):
{"action": "buy|sell|hold", "confidence": 0.0-1.0, "reasoning": "brief", "suggested_amount_pct": 0-100}"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            logger.info(f"AI response: {text}")

            decision = _extract_json(text)
            decision["action"] = str(decision.get("action", "hold")).lower()
            decision["amount_pct"] = decision.pop("suggested_amount_pct", 0)
            return decision

        except Exception as e:
            logger.error(f"AI decision failed ({type(e).__name__}: {e}) — falling back to rule-based")
            fallback = self._rule_based(signal)
            fallback["reasoning"] = f"AI fallback: {e}"
            return fallback
