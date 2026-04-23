"""Mean-reversion ("chop") strategy for sideways regimes.

Purpose
-------
The trend-following engine in ``technical.py`` intentionally HOLDs when
ADX < 20 so it never fights chop. Over the last live-test that gated
out ~8000 cycles with only 358 usable trend signals across 24h. This
module fills that gap: it turns the sideways phase into an opportunity
window by fading extremes against a Donchian range, confirmed by RSI
and by key-level confluence.

Activation
----------
The engine only fires when ALL are true:

  * channel width ≥ 2 × ATR            (enough room to actually trade)
  * ADX < 22                           (not an emerging trend)
  * price is at the outer 15% of the   (candle is already at the edge)
    Donchian channel
  * RSI confirms exhaustion            (< 35 for longs, > 65 for shorts)
  * confidence score clears the floor

Output shape mirrors the trend engine so ``combined.py`` can slot it in
as a fallback without bespoke plumbing. SL / TP hints are tighter than
the default ATR-based sizing: stops go just beyond the channel edge,
targets aim for the channel mid-line — typical RR ~1.5-2.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange

from src.strategy.technical import Signal
from src.strategy.levels import KeyLevelResult
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChopResult:
    action: Signal                          # BUY / SELL / HOLD
    strength: float                         # 0..1
    reasoning: str = ""
    sl_hint: Optional[float] = None         # Price or None (use ATR default)
    tp_hint: Optional[float] = None
    indicators: dict = field(default_factory=dict)

    @property
    def is_tradable(self) -> bool:
        return self.action in (Signal.BUY, Signal.SELL) and self.strength > 0


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# These are tuned for 1H bars. Adjusting the channel length changes
# the horizon — 20 bars ≈ ~1 trading day on 1H.
DEFAULT_CHANNEL_LEN = 20
DEFAULT_MIN_CHANNEL_ATR = 2.0       # width / ATR must exceed this
DEFAULT_EDGE_PCT = 0.15             # price within outer 15% of channel
DEFAULT_RSI_LOW = 35
DEFAULT_RSI_HIGH = 65
DEFAULT_ADX_MAX = 22


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate_chop(
    df: pd.DataFrame,
    levels: Optional[KeyLevelResult] = None,
    channel_len: int = DEFAULT_CHANNEL_LEN,
    min_channel_atr: float = DEFAULT_MIN_CHANNEL_ATR,
    edge_pct: float = DEFAULT_EDGE_PCT,
    rsi_low: float = DEFAULT_RSI_LOW,
    rsi_high: float = DEFAULT_RSI_HIGH,
    adx_max: float = DEFAULT_ADX_MAX,
    min_strength: float = 0.45,
) -> ChopResult:
    """Return a mean-reversion signal from OHLC + key-levels context.

    HOLD is returned whenever any activation precondition fails; the
    caller can inspect the ``reasoning`` field to understand why.
    """
    if df is None or df.empty or len(df) < max(channel_len + 5, 30):
        return ChopResult(action=Signal.HOLD, strength=0.0, reasoning="insufficient data")

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # --- Core indicators ---
    try:
        rsi = float(RSIIndicator(close, window=14).rsi().iloc[-1])
        adx_ind = ADXIndicator(high, low, close, window=14)
        adx = float(adx_ind.adx().iloc[-1])
        atr = float(AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])
    except Exception as e:
        logger.debug(f"chop: indicator calc failed: {e}")
        return ChopResult(action=Signal.HOLD, strength=0.0, reasoning=f"indicator error: {e}")

    # Donchian channel over ``channel_len`` bars (excluding the current bar
    # to avoid lookahead / self-touching).
    upper = float(high.iloc[-channel_len-1:-1].max())
    lower = float(low.iloc[-channel_len-1:-1].min())
    mid = (upper + lower) / 2.0
    width = upper - lower
    price = float(close.iloc[-1])

    indicators = {
        "donchian_upper": round(upper, 4),
        "donchian_lower": round(lower, 4),
        "donchian_mid": round(mid, 4),
        "donchian_width": round(width, 4),
        "channel_pos": None,
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "atr": round(atr, 4),
    }

    # --- Activation gates ---
    if not np.isfinite(adx) or not np.isfinite(rsi) or not np.isfinite(atr):
        return ChopResult(action=Signal.HOLD, strength=0.0, reasoning="NaN indicators",
                          indicators=indicators)
    if atr <= 0 or width <= 0:
        return ChopResult(action=Signal.HOLD, strength=0.0, reasoning="degenerate channel",
                          indicators=indicators)
    if width < min_channel_atr * atr:
        return ChopResult(
            action=Signal.HOLD, strength=0.0,
            reasoning=f"channel too tight: width={width:.4f} < {min_channel_atr}×atr={atr:.4f}",
            indicators=indicators,
        )
    if adx >= adx_max:
        return ChopResult(
            action=Signal.HOLD, strength=0.0,
            reasoning=f"adx={adx:.1f} >= {adx_max} (trend too strong to fade)",
            indicators=indicators,
        )

    # --- Position within channel (0=lower, 1=upper) ---
    channel_pos = (price - lower) / width if width > 0 else 0.5
    channel_pos = max(0.0, min(1.0, channel_pos))
    indicators["channel_pos"] = round(channel_pos, 3)

    # --- Direction + strength ---
    at_lower_edge = channel_pos <= edge_pct
    at_upper_edge = channel_pos >= (1.0 - edge_pct)

    # BUY (fade short): near lower band + oversold RSI
    if at_lower_edge and rsi < rsi_low:
        # Base strength: deeper into edge and more oversold → stronger
        depth = 1.0 - (channel_pos / edge_pct) if edge_pct > 0 else 0.0
        rsi_factor = max(0.0, (rsi_low - rsi) / rsi_low)
        base = 0.4 + 0.4 * depth + 0.2 * rsi_factor

        # Level confluence: supports near current price amplify conviction
        support_boost = _support_confluence_boost(levels, price, is_long=True)

        strength = min(1.0, base + support_boost)
        if strength < min_strength:
            return ChopResult(
                action=Signal.HOLD, strength=strength,
                reasoning=f"long setup too weak: strength={strength:.2f} < {min_strength}",
                indicators=indicators,
            )

        sl_hint, tp_hint = _long_sl_tp(price, lower, mid, atr, levels)
        reasoning = (
            f"chop LONG fade @ channel_pos={channel_pos:.2f} "
            f"rsi={rsi:.1f} adx={adx:.1f} "
            f"sup_boost={support_boost:+.2f} → strength={strength:.2f}"
        )
        indicators["sl_hint"] = sl_hint
        indicators["tp_hint"] = tp_hint
        return ChopResult(
            action=Signal.BUY, strength=strength, reasoning=reasoning,
            sl_hint=sl_hint, tp_hint=tp_hint, indicators=indicators,
        )

    # SELL (fade long): near upper band + overbought RSI
    if at_upper_edge and rsi > rsi_high:
        depth = (channel_pos - (1.0 - edge_pct)) / edge_pct if edge_pct > 0 else 0.0
        depth = max(0.0, min(1.0, depth))
        rsi_factor = max(0.0, (rsi - rsi_high) / (100.0 - rsi_high))
        base = 0.4 + 0.4 * depth + 0.2 * rsi_factor

        resist_boost = _support_confluence_boost(levels, price, is_long=False)

        strength = min(1.0, base + resist_boost)
        if strength < min_strength:
            return ChopResult(
                action=Signal.HOLD, strength=strength,
                reasoning=f"short setup too weak: strength={strength:.2f} < {min_strength}",
                indicators=indicators,
            )

        sl_hint, tp_hint = _short_sl_tp(price, upper, mid, atr, levels)
        reasoning = (
            f"chop SHORT fade @ channel_pos={channel_pos:.2f} "
            f"rsi={rsi:.1f} adx={adx:.1f} "
            f"res_boost={resist_boost:+.2f} → strength={strength:.2f}"
        )
        indicators["sl_hint"] = sl_hint
        indicators["tp_hint"] = tp_hint
        return ChopResult(
            action=Signal.SELL, strength=strength, reasoning=reasoning,
            sl_hint=sl_hint, tp_hint=tp_hint, indicators=indicators,
        )

    # No extreme — stand aside
    return ChopResult(
        action=Signal.HOLD, strength=0.0,
        reasoning=f"no edge tag (pos={channel_pos:.2f} rsi={rsi:.1f})",
        indicators=indicators,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUPPORT_LEVEL_NAMES = {
    "prev_week_low", "monday_low", "weekly_low", "prev_week_mid",
    "prev_quarter_mid", "current_year_mid",
}
_RESISTANCE_LEVEL_NAMES = {
    "prev_week_high", "monday_high", "prev_month_high", "prev_week_mid",
    "prev_quarter_mid", "current_year_mid",
}


def _support_confluence_boost(
    levels: Optional[KeyLevelResult],
    price: float,
    is_long: bool,
    proximity_pct: float = 0.006,
) -> float:
    """Return 0..0.25 bonus strength if a compatible level sits near price."""
    if levels is None or not levels.levels or price <= 0:
        return 0.0
    names = _SUPPORT_LEVEL_NAMES if is_long else _RESISTANCE_LEVEL_NAMES
    best = 0.0
    for lv in levels.levels:
        if lv.name not in names:
            continue
        if lv.price <= 0:
            continue
        dist_pct = abs(price - lv.price) / price
        if dist_pct <= proximity_pct:
            # Closer + higher-priority level → bigger boost.
            nearness = 1.0 - dist_pct / proximity_pct
            boost = 0.15 + 0.10 * (lv.priority / 5.0) * nearness
            best = max(best, boost)
    return round(min(best, 0.25), 3)


def _long_sl_tp(
    price: float, lower: float, mid: float, atr: float,
    levels: Optional[KeyLevelResult],
) -> tuple[float, float]:
    """Stop: below the lower band by ~0.5 ATR (or below a support if lower).
    Target: channel midpoint — gives ~1.5-2.2 RR with these defaults.
    """
    sl_base = lower - 0.5 * atr
    # If a supportive level sits just below price, use it for a tighter stop.
    if levels is not None:
        for lv in levels.levels:
            if lv.name in _SUPPORT_LEVEL_NAMES and 0 < lv.price < price:
                candidate = lv.price - 0.4 * atr
                if candidate < price and candidate > sl_base * 0.995:
                    sl_base = max(sl_base, candidate)  # pick tighter, but not inside
    sl = min(sl_base, price - 0.4 * atr)   # don't pick a "stop" above price
    tp = mid
    # Ensure RR >= 1.2 minimum; if mid is too close, extend toward upper mid
    risk = price - sl
    reward = tp - price
    if risk > 0 and reward / risk < 1.2:
        tp = price + max(reward, 1.5 * risk)
    return round(sl, 4), round(tp, 4)


def _short_sl_tp(
    price: float, upper: float, mid: float, atr: float,
    levels: Optional[KeyLevelResult],
) -> tuple[float, float]:
    sl_base = upper + 0.5 * atr
    if levels is not None:
        for lv in levels.levels:
            if lv.name in _RESISTANCE_LEVEL_NAMES and lv.price > price:
                candidate = lv.price + 0.4 * atr
                if candidate > price:
                    sl_base = min(sl_base, candidate) if candidate < sl_base else sl_base
    sl = max(sl_base, price + 0.4 * atr)
    tp = mid
    risk = sl - price
    reward = price - tp
    if risk > 0 and reward / risk < 1.2:
        tp = price - max(reward, 1.5 * risk)
    return round(sl, 4), round(tp, 4)
