from dataclasses import dataclass
from enum import Enum

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import BollingerBands


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class TechnicalSignal:
    signal: Signal
    strength: float  # 0.0 to 1.0
    indicators: dict


def analyze_technical(df: pd.DataFrame) -> TechnicalSignal:
    """Run technical indicators and generate a combined signal."""
    close = df["close"]

    # RSI
    rsi = RSIIndicator(close, window=14).rsi()
    current_rsi = rsi.iloc[-1]

    # MACD
    macd = MACD(close)
    macd_line = macd.macd()
    macd_signal = macd.macd_signal()
    macd_hist = macd.macd_diff()
    current_macd_hist = macd_hist.iloc[-1]

    # Bollinger Bands
    bb = BollingerBands(close)
    bb_upper = bb.bollinger_hband()
    bb_lower = bb.bollinger_lband()
    current_price = close.iloc[-1]
    current_bb_upper = bb_upper.iloc[-1]
    current_bb_lower = bb_lower.iloc[-1]

    # EMA 20/50
    ema_20 = EMAIndicator(close, window=20).ema_indicator()
    ema_50 = EMAIndicator(close, window=50).ema_indicator()
    current_ema_20 = ema_20.iloc[-1]
    current_ema_50 = ema_50.iloc[-1]

    indicators = {
        "rsi": round(current_rsi, 2),
        "macd_hist": round(current_macd_hist, 4),
        "bb_upper": round(current_bb_upper, 2),
        "bb_lower": round(current_bb_lower, 2),
        "ema_20": round(current_ema_20, 2),
        "ema_50": round(current_ema_50, 2),
        "price": round(current_price, 2),
    }

    # Scoring: count bullish/bearish signals
    score = 0.0

    # RSI: oversold < 30 = bullish, overbought > 70 = bearish
    if current_rsi < 30:
        score += 1.0
    elif current_rsi > 70:
        score -= 1.0

    # MACD histogram positive = bullish
    if current_macd_hist > 0:
        score += 0.5
    else:
        score -= 0.5

    # Bollinger: price near lower band = bullish
    bb_range = current_bb_upper - current_bb_lower
    if bb_range > 0:
        bb_position = (current_price - current_bb_lower) / bb_range
        if bb_position < 0.2:
            score += 0.5
        elif bb_position > 0.8:
            score -= 0.5

    # EMA crossover: ema_20 > ema_50 = bullish
    if current_ema_20 > current_ema_50:
        score += 0.5
    else:
        score -= 0.5

    # Determine signal
    if score >= 1.0:
        signal = Signal.BUY
        strength = min(score / 2.5, 1.0)
    elif score <= -1.0:
        signal = Signal.SELL
        strength = min(abs(score) / 2.5, 1.0)
    else:
        signal = Signal.HOLD
        strength = 0.0

    return TechnicalSignal(signal=signal, strength=strength, indicators=indicators)
