"""Market regime detector — classifies BULL / BEAR / SIDEWAYS.

Two-layer detection:
  Layer 1 (Technical): EMA + RSI + HH/LL + ATR volatility — fast, per-symbol
  Layer 2 (AI Macro): LLM reads funding, OI, news, BTC dom — slow, once per cycle

Features:
  - Multi-timeframe: 4H for regime, 1H for execution
  - Volatility filter: ATR percentile confirms trending vs choppy
  - Regime persistence: needs N consecutive confirmations before switching
  - AI macro regime: aware of funding, OI, narrative bias
"""

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

from src.utils.logger import logger


class Regime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"


class Bias(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    NEUTRAL = "neutral"


@dataclass
class RegimeResult:
    regime: Regime
    score: float             # 0.0-1.0 confidence
    trend_score: float       # EMA component (-1 to +1)
    momentum_score: float    # RSI component (-1 to +1)
    structure_score: float   # HH/LL component (-1 to +1)
    volatility_score: float  # ATR percentile component (0 to 1)
    is_persisted: bool       # True if confirmed by persistence check


class RegimeTracker:
    """Tracks regime state per symbol to prevent flip-flopping.

    A regime must be confirmed for N consecutive evaluations before switching.
    """

    def __init__(self, confirm_count: int = 3):
        self.confirm_count = confirm_count
        self._states: dict[str, dict] = {}  # symbol -> {regime, candidate, count}

    def get_confirmed(self, symbol: str, raw_regime: Regime) -> tuple[Regime, bool]:
        """Return the confirmed regime after applying persistence filter.

        Returns (confirmed_regime, just_switched).
        """
        state = self._states.get(symbol)

        if state is None:
            # First evaluation — accept immediately
            self._states[symbol] = {
                "regime": raw_regime,
                "candidate": raw_regime,
                "count": 1,
            }
            return raw_regime, True

        if raw_regime == state["regime"]:
            # Same as current — reset candidate
            state["candidate"] = raw_regime
            state["count"] = 1
            return raw_regime, False

        if raw_regime == state["candidate"]:
            # Candidate gaining confirmations
            state["count"] += 1
            if state["count"] >= self.confirm_count:
                state["regime"] = raw_regime
                state["count"] = 1
                logger.info(
                    f"Regime switch confirmed for {symbol}: {raw_regime.value} "
                    f"(after {self.confirm_count} confirmations)"
                )
                return raw_regime, True
            return state["regime"], False

        # New candidate — reset counter
        state["candidate"] = raw_regime
        state["count"] = 1
        return state["regime"], False


# Global tracker instance — persists across cycles
_tracker = RegimeTracker(confirm_count=3)


def detect_regime(
    df: pd.DataFrame,
    symbol: str = "",
    use_persistence: bool = True,
) -> RegimeResult:
    """Detect market regime from OHLCV data.

    Designed for multi-timeframe use:
      - Pass 4H dataframe for regime detection (higher accuracy)
      - Pass 1H dataframe for execution signals

    Requires at least 60 candles. Returns SIDEWAYS if insufficient data.
    """
    if len(df) < 60:
        result = RegimeResult(Regime.SIDEWAYS, 0.0, 0.0, 0.0, 0.0, 0.0, False)
        return result

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # --- 1. EMA Trend (30% weight) ---
    e8 = EMAIndicator(close=close, window=8).ema_indicator()
    e21 = EMAIndicator(close=close, window=21).ema_indicator()
    e55 = EMAIndicator(close=close, window=55).ema_indicator()

    cur_e8 = e8.iloc[-1]
    cur_e21 = e21.iloc[-1]
    cur_e55 = e55.iloc[-1]
    cur_price = close.iloc[-1]

    trend_score = 0.0
    if cur_e8 > cur_e21 > cur_e55 and cur_price > cur_e21:
        trend_score = 1.0
    elif cur_e8 > cur_e21 and cur_price > cur_e21:
        trend_score = 0.5
    elif cur_e8 < cur_e21 < cur_e55 and cur_price < cur_e21:
        trend_score = -1.0
    elif cur_e8 < cur_e21 and cur_price < cur_e21:
        trend_score = -0.5

    # --- 2. RSI Momentum (20% weight) ---
    rsi = RSIIndicator(close=close, window=14).rsi()
    cur_rsi = rsi.iloc[-1]

    momentum_score = 0.0
    if cur_rsi >= 60:
        momentum_score = 1.0
    elif cur_rsi >= 55:
        momentum_score = 0.5
    elif cur_rsi <= 40:
        momentum_score = -1.0
    elif cur_rsi <= 45:
        momentum_score = -0.5

    # --- 3. Price Structure: HH/LL over last 20 bars (25% weight) ---
    lookback = min(20, len(df) - 1)
    recent_high = high.iloc[-lookback:]
    recent_low = low.iloc[-lookback:]

    highs_idx = _find_swings(recent_high.values, direction="high")
    lows_idx = _find_swings(recent_low.values, direction="low")

    structure_score = 0.0
    if len(highs_idx) >= 2 and len(lows_idx) >= 2:
        hh = recent_high.iloc[highs_idx[-1]] > recent_high.iloc[highs_idx[-2]]
        hl = recent_low.iloc[lows_idx[-1]] > recent_low.iloc[lows_idx[-2]]
        lh = recent_high.iloc[highs_idx[-1]] < recent_high.iloc[highs_idx[-2]]
        ll = recent_low.iloc[lows_idx[-1]] < recent_low.iloc[lows_idx[-2]]

        if hh and hl:
            structure_score = 1.0
        elif lh and ll:
            structure_score = -1.0
        elif hh or hl:
            structure_score = 0.3
        elif lh or ll:
            structure_score = -0.3

    # --- 4. Volatility Filter via ATR percentile (25% weight) ---
    atr_indicator = AverageTrueRange(high=high, low=low, close=close, window=14)
    atr_series = atr_indicator.average_true_range()

    # ATR as percentage of price — normalized across assets
    atr_pct = atr_series / close * 100

    # Percentile rank over last 50 bars (where does current ATR sit?)
    atr_window = atr_pct.iloc[-50:]
    cur_atr_pct = atr_pct.iloc[-1]
    atr_percentile = (atr_window < cur_atr_pct).sum() / len(atr_window)

    # High ATR percentile (>0.6) = trending, Low (<0.4) = choppy
    # This modulates the directional scores
    volatility_score = atr_percentile  # 0-1, higher = more trending

    # When volatility is low, dampen directional signals (more sideways)
    vol_dampener = 0.4 + (volatility_score * 0.6)  # ranges from 0.4 to 1.0

    # --- Combine scores with volatility dampening ---
    raw_combined = (trend_score * 0.30) + (momentum_score * 0.20) + (structure_score * 0.25)
    combined = raw_combined * vol_dampener + (volatility_score - 0.5) * 0.25

    if combined >= 0.6:
        raw_regime = Regime.BULL
        score = combined
    elif combined <= -0.6:
        raw_regime = Regime.BEAR
        score = abs(combined)
    else:
        raw_regime = Regime.SIDEWAYS
        score = 1.0 - abs(combined)

    # --- Persistence filter ---
    is_persisted = True
    final_regime = raw_regime
    if use_persistence and symbol:
        final_regime, is_persisted = _tracker.get_confirmed(symbol, raw_regime)

    vol_label = "TREND" if volatility_score > 0.6 else "CHOP" if volatility_score < 0.4 else "NORMAL"
    logger.info(
        f"Regime: {final_regime.value} ({score:.2f}) | "
        f"trend={trend_score:+.1f} mom={momentum_score:+.1f} struct={structure_score:+.1f} | "
        f"ATR_pct={cur_atr_pct:.3f}% vol={vol_label}({volatility_score:.0%}) damp={vol_dampener:.2f}"
        + (" [PERSISTED]" if is_persisted else " [PENDING]")
    )

    return RegimeResult(
        regime=final_regime,
        score=score,
        trend_score=trend_score,
        momentum_score=momentum_score,
        structure_score=structure_score,
        volatility_score=volatility_score,
        is_persisted=is_persisted,
    )


def _find_swings(values: np.ndarray, direction: str = "high") -> list[int]:
    """Find local peaks (high) or troughs (low) in a price series.

    A swing is confirmed when 2 bars on each side are lower (peak) or higher (trough).
    """
    swings = []
    n = len(values)
    for i in range(2, n - 2):
        if direction == "high":
            if (values[i] > values[i - 1] and values[i] > values[i - 2]
                    and values[i] > values[i + 1] and values[i] > values[i + 2]):
                swings.append(i)
        else:
            if (values[i] < values[i - 1] and values[i] < values[i - 2]
                    and values[i] < values[i + 1] and values[i] < values[i + 2]):
                swings.append(i)
    return swings


# =============================================================================
# Layer 2: AI Macro Regime — called ONCE per cycle, not per pair
# =============================================================================

@dataclass
class MacroData:
    """On-chain macro context from Hyperliquid."""
    btc_funding: float       # BTC funding rate (e.g. -0.000015)
    eth_funding: float       # ETH funding rate
    avg_funding: float       # Average funding across top assets
    btc_oi_usd: float        # BTC open interest in USD
    eth_oi_usd: float        # ETH open interest in USD
    btc_24h_vol: float       # BTC 24h notional volume
    btc_24h_change: float    # BTC 24h price change %
    sentiment_summary: str   # News sentiment summary from Tavily
    sentiment_label: str     # bullish / bearish / neutral


@dataclass
class AiRegimeResult:
    """AI macro regime analysis."""
    regime: Regime
    confidence: float        # 0.0-1.0
    bias: Bias               # risk_on / risk_off / neutral
    reasoning: str


def fetch_macro_data(sentiment_summary: str = "", sentiment_label: str = "") -> MacroData:
    """Fetch macro market context from Hyperliquid.

    Call this ONCE per cycle, not per pair.
    """
    import requests

    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data[0]
        ctxs = data[1]
    except Exception as e:
        logger.warning(f"Failed to fetch macro data: {e}")
        return MacroData(0, 0, 0, 0, 0, 0, 0, sentiment_summary, sentiment_label)

    # Extract key asset data
    fundings = []
    btc_ctx = eth_ctx = None
    for i, ctx in enumerate(ctxs):
        name = meta["universe"][i]["name"]
        funding = float(ctx.get("funding", 0))
        fundings.append(funding)
        if name == "BTC":
            btc_ctx = ctx
        elif name == "ETH":
            eth_ctx = ctx

    btc_funding = float(btc_ctx.get("funding", 0)) if btc_ctx else 0
    eth_funding = float(eth_ctx.get("funding", 0)) if eth_ctx else 0
    avg_funding = np.mean(fundings) if fundings else 0

    btc_oi = float(btc_ctx.get("openInterest", 0)) * float(btc_ctx.get("markPx", 1)) if btc_ctx else 0
    eth_oi = float(eth_ctx.get("openInterest", 0)) * float(eth_ctx.get("markPx", 1)) if eth_ctx else 0

    btc_vol = float(btc_ctx.get("dayNtlVlm", 0)) if btc_ctx else 0

    btc_24h_change = 0.0
    if btc_ctx:
        prev = float(btc_ctx.get("prevDayPx", 0))
        cur = float(btc_ctx.get("markPx", 0))
        if prev > 0:
            btc_24h_change = ((cur - prev) / prev) * 100

    return MacroData(
        btc_funding=btc_funding,
        eth_funding=eth_funding,
        avg_funding=avg_funding,
        btc_oi_usd=btc_oi,
        eth_oi_usd=eth_oi,
        btc_24h_vol=btc_vol,
        btc_24h_change=btc_24h_change,
        sentiment_summary=sentiment_summary,
        sentiment_label=sentiment_label,
    )


def detect_ai_regime(macro: MacroData) -> AiRegimeResult:
    """Ask AI to classify macro regime from on-chain + news context.

    Called ONCE per cycle. Falls back to rule-based if AI unavailable.
    """
    from config import settings
    import json

    api_key = settings.LLM_API_KEY or settings.ANTHROPIC_API_KEY
    if not api_key:
        return _rule_based_ai_regime(macro)

    funding_label = "positive (longs paying)" if macro.btc_funding > 0 else "negative (shorts paying)"
    oi_billions = macro.btc_oi_usd / 1e9

    prompt = f"""You are a crypto macro analyst. Classify the current market regime based on this data:

BTC Funding Rate: {macro.btc_funding:.8f} ({funding_label})
ETH Funding Rate: {macro.eth_funding:.8f}
Average Funding (top assets): {macro.avg_funding:.8f}
BTC Open Interest: ${oi_billions:.2f}B
ETH Open Interest: ${macro.eth_oi_usd / 1e9:.2f}B
BTC 24h Volume: ${macro.btc_24h_vol / 1e6:.0f}M
BTC 24h Change: {macro.btc_24h_change:+.2f}%
News Sentiment: {macro.sentiment_label}
News Summary: {macro.sentiment_summary or 'No recent news'}

Classify the macro regime:
- BULL: risk-on, positive funding, rising OI, bullish news → favor longs
- BEAR: risk-off, negative funding, falling OI, bearish news → favor shorts
- SIDEWAYS: mixed signals, neutral funding, uncertain → defensive

Respond in this exact JSON format only:
{{"regime": "bull"|"bear"|"sideways", "confidence": 0.0-1.0, "bias": "risk_on"|"risk_off"|"neutral", "reasoning": "1-2 sentence macro assessment"}}"""

    try:
        import anthropic
        client_kwargs: dict = {"api_key": api_key}
        if settings.LLM_BASE_URL:
            client_kwargs["base_url"] = settings.LLM_BASE_URL
        client = anthropic.Anthropic(**client_kwargs)
        response = client.messages.create(
            model=settings.LLM_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        logger.info(f"AI macro regime response: {text}")

        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        # Scan for JSON object braces as extra robustness
        _s, _e = text.find("{"), text.rfind("}")
        if _s >= 0 and _e > _s:
            text = text[_s:_e + 1]

        result = json.loads(text)
        regime_str = result.get("regime", "sideways").lower()
        regime = Regime(regime_str) if regime_str in ("bull", "bear", "sideways") else Regime.SIDEWAYS
        confidence = min(max(float(result.get("confidence", 0.5)), 0.0), 1.0)
        bias_str = result.get("bias", "neutral").lower()
        bias = Bias(bias_str) if bias_str in ("risk_on", "risk_off", "neutral") else Bias.NEUTRAL

        return AiRegimeResult(
            regime=regime,
            confidence=confidence,
            bias=bias,
            reasoning=result.get("reasoning", ""),
        )
    except Exception as e:
        logger.warning(f"AI macro regime failed: {e}")
        return _rule_based_ai_regime(macro)


def _rule_based_ai_regime(macro: MacroData) -> AiRegimeResult:
    """Fallback: rule-based macro regime from funding + OI + news."""
    score = 0.0

    # Funding signal
    if macro.btc_funding > 0.0001:
        score += 0.3  # longs paying = bullish
    elif macro.btc_funding < -0.0001:
        score -= 0.3  # shorts paying = bearish

    # 24h BTC price change
    if macro.btc_24h_change > 2:
        score += 0.3
    elif macro.btc_24h_change < -2:
        score -= 0.3

    # News sentiment
    if macro.sentiment_label == "bearish":
        score -= 0.2
    elif macro.sentiment_label == "bullish":
        score += 0.2

    if score >= 0.3:
        regime = Regime.BULL
        bias = Bias.RISK_ON
    elif score <= -0.3:
        regime = Regime.BEAR
        bias = Bias.RISK_OFF
    else:
        regime = Regime.SIDEWAYS
        bias = Bias.NEUTRAL

    return AiRegimeResult(
        regime=regime,
        confidence=min(abs(score) + 0.3, 1.0),
        bias=bias,
        reasoning=f"Rule-based: funding={macro.btc_funding:.6f}, btc_24h={macro.btc_24h_change:+.1f}%, sentiment={macro.sentiment_label}",
    )


# =============================================================================
# Blended Regime — combines technical + AI macro
# =============================================================================

@dataclass
class BlendedRegimeResult:
    """Final regime combining technical (fast) + AI macro (slow)."""
    regime: Regime
    confidence: float
    technical_regime: Regime
    ai_regime: Regime | None
    ai_confidence: float
    ai_bias: Bias
    volatility_score: float
    is_persisted: bool
    reasoning: str


def blend_regimes(
    tech_result: RegimeResult,
    ai_result: AiRegimeResult | None = None,
    tech_weight: float = 0.6,
    ai_weight: float = 0.4,
    ai_min_confidence: float = 0.6,
) -> BlendedRegimeResult:
    """Blend technical and AI macro regime into final regime.

    Technical regime is always used. AI regime is only blended if:
      1. ai_result is provided (not None)
      2. ai_result.confidence >= ai_min_confidence

    When both agree → higher confidence
    When they disagree → technical wins but confidence is reduced
    """
    tech_regime = tech_result.regime
    tech_score = tech_result.score

    # No AI data — use technical only
    if ai_result is None or ai_result.confidence < ai_min_confidence:
        return BlendedRegimeResult(
            regime=tech_regime,
            confidence=tech_score,
            technical_regime=tech_regime,
            ai_regime=ai_result.regime if ai_result else None,
            ai_confidence=ai_result.confidence if ai_result else 0.0,
            ai_bias=ai_result.bias if ai_result else Bias.NEUTRAL,
            volatility_score=tech_result.volatility_score,
            is_persisted=tech_result.is_persisted,
            reasoning=f"Technical only: {tech_regime.value} ({tech_score:.2f})"
            + (f" | AI skipped (conf={ai_result.confidence:.2f}<{ai_min_confidence})" if ai_result else " | No AI data"),
        )

    # Both available — blend
    ai_regime = ai_result.regime
    ai_conf = ai_result.confidence

    if tech_regime == ai_regime:
        # Agreement → boost confidence
        blended_confidence = min(tech_score * tech_weight + ai_conf * ai_weight, 1.0)
        final_regime = tech_regime
        agreement = "AGREE"
    else:
        # Disagreement → technical wins, reduced confidence
        blended_confidence = tech_score * 0.7  # penalize disagreement
        final_regime = tech_regime  # technical wins
        agreement = "DISAGREE"

    return BlendedRegimeResult(
        regime=final_regime,
        confidence=blended_confidence,
        technical_regime=tech_regime,
        ai_regime=ai_regime,
        ai_confidence=ai_conf,
        ai_bias=ai_result.bias,
        volatility_score=tech_result.volatility_score,
        is_persisted=tech_result.is_persisted,
        reasoning=f"Tech={tech_regime.value}({tech_score:.2f}) + AI={ai_regime.value}({ai_conf:.2f}) [{agreement}] bias={ai_result.bias.value}",
    )
