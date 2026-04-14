import anthropic
from config import settings
from src.strategy.combined import CombinedSignal
from src.utils.logger import logger


class AIAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    def decide(self, signal: CombinedSignal, symbol: str, balance: dict) -> dict:
        """Let AI make the final trading decision based on combined signals and context."""
        if not settings.ANTHROPIC_API_KEY:
            logger.warning("Anthropic API key not set, using rule-based decision")
            return {
                "action": signal.action.value,
                "confidence": signal.confidence,
                "reasoning": signal.reasoning,
                "amount": 0,
            }

        prompt = f"""You are a crypto trading AI agent. Analyze the following data and decide whether to BUY, SELL, or HOLD.

Symbol: {symbol}
Account Balance: {balance.get('total', 0)} USDT (free: {balance.get('free', 0)})

Technical Analysis:
- RSI: {signal.technical.indicators.get('rsi')}
- MACD Histogram: {signal.technical.indicators.get('macd_hist')}
- EMA 20: {signal.technical.indicators.get('ema_20')}
- EMA 50: {signal.technical.indicators.get('ema_50')}
- Bollinger Bands: {signal.technical.indicators.get('bb_lower')} - {signal.technical.indicators.get('bb_upper')}
- Price: {signal.technical.indicators.get('price')}

Technical Signal: {signal.technical.signal.value} (strength: {signal.technical.strength:.2f})
Sentiment: {signal.sentiment.sentiment.value} (confidence: {signal.sentiment.confidence:.2f})
News Summary: {signal.sentiment.summary}

Respond in this exact JSON format only:
{{"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, "reasoning": "brief explanation", "suggested_amount_pct": 0-100}}"""

        try:
            response = self.client.messages.create(
                model="glm-5.1",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            logger.info(f"AI response: {text}")

            # Parse JSON response
            import json
            # Extract JSON from response (handle markdown code blocks)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            decision = json.loads(text)
            decision["action"] = decision.get("action", "hold").lower()
            decision["amount_pct"] = decision.pop("suggested_amount_pct", 0)
            return decision

        except Exception as e:
            logger.error(f"AI decision failed: {e}")
            return {
                "action": signal.action.value,
                "confidence": signal.confidence,
                "reasoning": f"AI fallback to signal: {e}",
                "amount_pct": 0,
            }
