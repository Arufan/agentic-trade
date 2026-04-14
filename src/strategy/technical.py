from dataclasses import dataclass
from enum import Enum

import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator, ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange


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
    """Trend-following strategy with momentum confirmation.

    Core idea: trade WITH the trend, only enter on pullbacks + momentum reversal.
    Uses ADX for trend strength, EMA for direction, RSI/MACD for timing.
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # === Indicators ===
    rsi = RSIIndicator(close, window=14).rsi()
    current_rsi = rsi.iloc[-1]

    macd = MACD(close)
    macd_hist = macd.macd_diff()
    current_macd_hist = macd_hist.iloc[-1]
    prev_macd_hist = macd_hist.iloc[-2]

    bb = BollingerBands(close, window=20, window_dev=2)
    bb_upper = bb.bollinger_hband().iloc[-1]
    bb_lower = bb.bollinger_lband().iloc[-1]

    ema_8 = EMAIndicator(close, window=8).ema_indicator()
    ema_21 = EMAIndicator(close, window=21).ema_indicator()
    ema_55 = EMAIndicator(close, window=55).ema_indicator()
    e8 = ema_8.iloc[-1]
    e21 = ema_21.iloc[-1]
    e55 = ema_55.iloc[-1]
    prev_e8 = ema_8.iloc[-2]
    prev_e21 = ema_21.iloc[-2]

    adx_ind = ADXIndicator(high, low, close, window=14)
    current_adx = adx_ind.adx().iloc[-1]
    plus_di = adx_ind.adx_pos().iloc[-1]
    minus_di = adx_ind.adx_neg().iloc[-1]

    atr = AverageTrueRange(high, low, close, window=14)
    current_atr = atr.average_true_range().iloc[-1]

    current_vol = volume.iloc[-1]
    avg_vol = volume.rolling(20).mean().iloc[-1]
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

    current_price = close.iloc[-1]

    indicators = {
        "rsi": round(current_rsi, 2),
        "macd_hist": round(current_macd_hist, 4),
        "ema_8": round(e8, 2),
        "ema_21": round(e21, 2),
        "ema_55": round(e55, 2),
        "adx": round(current_adx, 2),
        "atr": round(current_atr, 2),
        "vol_ratio": round(vol_ratio, 2),
        "price": round(current_price, 2),
    }

    # === GATE 1: Need some trend (ADX > 20) ===
    if not np.isfinite(current_adx) or current_adx < 20:
        return TechnicalSignal(signal=Signal.HOLD, strength=0.0, indicators=indicators)

    # === GATE 2: Volume must confirm ===
    if vol_ratio < 0.85:
        return TechnicalSignal(signal=Signal.HOLD, strength=0.0, indicators=indicators)

    # === GATE 3: ATR must be healthy (no choppy micro-ranges) ===
    if not np.isfinite(current_atr) or current_atr < (current_price * 0.003):
        return TechnicalSignal(signal=Signal.HOLD, strength=0.0, indicators=indicators)

    # === Determine TREND DIRECTION ===
    # Primary trend: EMA 21 vs 55
    uptrend = e21 > e55
    downtrend = e21 < e55

    # === BUY SIGNAL: Uptrend + pullback + reversal confirmation ===
    buy_score = 0
    if uptrend:
        # Price near or below EMA21 (pullback in uptrend)
        if current_price <= e21 * 1.01:
            buy_score += 2

        # RSI in buy zone (not overbought)
        if current_rsi < 55:
            buy_score += 1

        # EMA 8 above or crossing above EMA 21
        if e8 > e21:
            buy_score += 1
        if prev_e8 <= prev_e21 and e8 > e21:
            buy_score += 1  # Crossover bonus

        # MACD histogram turning up or positive
        if current_macd_hist > prev_macd_hist:
            buy_score += 1
        if current_macd_hist > 0:
            buy_score += 1

        # Volume confirmation
        if vol_ratio > 0.9:
            buy_score += 1

        # Price above EMA 55 (macro uptrend)
        if current_price > e55:
            buy_score += 1

        # DI+ > DI- (trend direction confirmation)
        if plus_di > minus_di and plus_di > 20:
            buy_score += 1

    # === SELL SIGNAL: Downtrend + pullback + reversal confirmation ===
    sell_score = 0
    if downtrend:
        # Price near or above EMA21 (pullback in downtrend)
        if current_price >= e21 * 0.99:
            sell_score += 2

        # RSI in sell zone (not oversold)
        if current_rsi > 45:
            sell_score += 1

        # EMA 8 below or crossing below EMA 21
        if e8 < e21:
            sell_score += 1
        if prev_e8 >= prev_e21 and e8 < e21:
            sell_score += 1

        # MACD turning down or negative
        if current_macd_hist < prev_macd_hist:
            sell_score += 1
        if current_macd_hist < 0:
            sell_score += 1

        # Volume confirmation
        if vol_ratio > 0.9:
            sell_score += 1

        # Price below EMA 55
        if current_price < e55:
            sell_score += 1

        # DI- > DI+ (trend direction confirmation)
        if minus_di > plus_di and minus_di > 20:
            sell_score += 1

    # === Determine signal ===
    threshold = 5

    if buy_score >= threshold:
        strength = min(buy_score / 8.0, 1.0)
        return TechnicalSignal(signal=Signal.BUY, strength=strength, indicators=indicators)
    elif sell_score >= threshold:
        strength = min(sell_score / 8.0, 1.0)
        return TechnicalSignal(signal=Signal.SELL, strength=strength, indicators=indicators)
    else:
        return TechnicalSignal(signal=Signal.HOLD, strength=0.0, indicators=indicators)
