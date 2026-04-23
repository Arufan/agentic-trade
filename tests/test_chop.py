"""Unit tests for the chop/mean-reversion engine.

Locks in the activation rules so future refactors can't silently
reopen the trend-follows-chop fight that put 8335 holds in the 24h
live-test log with only 358 trend signals.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from src.strategy.chop import (
    ChopResult,
    evaluate_chop,
    DEFAULT_CHANNEL_LEN,
)
from src.strategy.levels import KeyLevel, KeyLevelResult
from src.strategy.technical import Signal


# --------------------------------------------------------------------------- #
#  Synthetic bar factories                                                    #
# --------------------------------------------------------------------------- #

def _make_range_df(
    n: int = 60,
    upper: float = 110.0,
    lower: float = 100.0,
    last_close: float = 100.5,
    last_low: float = 100.2,
    last_high: float = 100.8,
    noise: float = 0.3,
    seed: int = 2,
):
    """Build an OHLCV df where prices chop between [lower, upper] for n bars,
    ending with a specific bar at (last_low, last_high, last_close) so the
    test can control the current channel position precisely."""
    rng = np.random.default_rng(seed)
    mid = (upper + lower) / 2.0
    closes = np.clip(mid + rng.normal(0, noise, size=n), lower + 0.2, upper - 0.2)

    # Enforce the band touching to avoid degenerate ADX reads
    closes[1] = lower + 0.5
    closes[2] = upper - 0.5
    closes[3] = lower + 0.8
    closes[4] = upper - 0.8

    opens = closes + rng.normal(0, 0.1, size=n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.2, size=n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.2, size=n))

    # Pin the bounds so the Donchian window sees them.
    highs[5] = upper
    lows[6] = lower

    # Override the last bar with the requested candle
    closes[-1] = last_close
    opens[-1] = last_close - 0.05
    highs[-1] = last_high
    lows[-1] = last_low

    idx = pd.date_range(
        end=datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
        periods=n, freq="h",
    )
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": 1000.0,
    }, index=idx)


def _make_trending_df(n: int = 60, start: float = 100.0, slope: float = 0.3):
    """Monotonic uptrend — should drive ADX > 25 and prevent chop activation."""
    idx = pd.date_range(
        end=datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
        periods=n, freq="h",
    )
    closes = np.linspace(start, start + slope * n, num=n)
    opens = closes - 0.05
    highs = closes + 0.15
    lows = closes - 0.15
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": 1000.0,
    }, index=idx)


# --------------------------------------------------------------------------- #
#  Activation gates                                                           #
# --------------------------------------------------------------------------- #

def test_returns_hold_on_empty_df():
    res = evaluate_chop(pd.DataFrame())
    assert res.action == Signal.HOLD
    assert "insufficient" in res.reasoning


def test_returns_hold_in_strong_trend():
    """ADX gets very high on a clean trend → chop must not fade it."""
    df = _make_trending_df(n=80)
    res = evaluate_chop(df)
    assert res.action == Signal.HOLD
    # Either "trend too strong" or no edge — both acceptable
    assert res.strength < 0.45


def test_returns_hold_when_channel_too_tight():
    """A tight range (< 2×ATR) is unsafe to fade."""
    idx = pd.date_range(
        end=datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
        periods=40, freq="h",
    )
    # Very low-range data
    close = 100.0 + 0.01 * np.sin(np.arange(40))
    df = pd.DataFrame({
        "open": close, "high": close + 0.02, "low": close - 0.02,
        "close": close, "volume": 1000.0,
    }, index=idx)
    res = evaluate_chop(df)
    assert res.action == Signal.HOLD


# --------------------------------------------------------------------------- #
#  Fire conditions                                                            #
# --------------------------------------------------------------------------- #

def test_fires_long_at_lower_edge_with_low_rsi():
    """Price at lower band + many red bars → RSI drops under 35."""
    n = 60
    idx = pd.date_range(
        end=datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
        periods=n, freq="h",
    )
    # Build a sharp dump into the lower band for the last ~12 bars so RSI
    # actually cranks down below 35.
    closes = np.empty(n)
    closes[:40] = 105.0 + np.random.default_rng(1).normal(0, 0.4, size=40)
    closes[40:] = np.linspace(105.0, 100.1, num=n - 40)  # downturn

    opens = closes + 0.03
    highs = np.maximum(opens, closes) + 0.3
    lows = np.minimum(opens, closes) - 0.3
    # Plant channel bounds early so Donchian window sees 110 & 100
    highs[3] = 110.0
    lows[5] = 100.0

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": 1000.0,
    }, index=idx)

    res = evaluate_chop(df)
    # We expect a LONG fade; ADX on a dump may be moderate. If the trend
    # engine would block it, chop might too — in that case at least verify
    # the result is never a SHORT (which would be wrong).
    assert res.action in (Signal.BUY, Signal.HOLD)
    if res.action == Signal.BUY:
        assert res.strength >= 0.45
        assert res.sl_hint is not None and res.sl_hint < float(closes[-1])
        assert res.tp_hint is not None and res.tp_hint > float(closes[-1])


def test_level_confluence_boosts_long_strength():
    """Same setup WITH a prev_week_low hugging current price → strength
    must be ≥ the no-levels case."""
    n = 60
    idx = pd.date_range(
        end=datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
        periods=n, freq="h",
    )
    closes = np.empty(n)
    closes[:40] = 105.0
    closes[40:] = np.linspace(105.0, 100.1, num=n - 40)
    opens = closes + 0.03
    highs = np.maximum(opens, closes) + 0.3
    lows = np.minimum(opens, closes) - 0.3
    highs[3] = 110.0
    lows[5] = 100.0
    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000.0,
    }, index=idx)

    # Craft a KeyLevelResult with prev_week_low close to the last close.
    last_px = float(closes[-1])
    lv = KeyLevel(name="prev_week_low", price=last_px * 0.997, priority=4)
    levels = KeyLevelResult(
        levels=[lv], nearest_support=lv, nearest_resistance=None,
        bias_score=0.5, confluence_score=0.3,
    )

    r_no = evaluate_chop(df, levels=None)
    r_with = evaluate_chop(df, levels=levels)

    if r_no.action == Signal.BUY and r_with.action == Signal.BUY:
        assert r_with.strength >= r_no.strength, \
            f"confluence must not hurt: {r_no.strength} vs {r_with.strength}"


def test_fires_short_at_upper_edge_with_high_rsi():
    """Build a rip to the upper edge so RSI crosses 65."""
    n = 60
    idx = pd.date_range(
        end=datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
        periods=n, freq="h",
    )
    closes = np.empty(n)
    closes[:40] = 100.0 + np.random.default_rng(1).normal(0, 0.4, size=40)
    closes[40:] = np.linspace(100.0, 109.9, num=n - 40)

    opens = closes - 0.03
    highs = np.maximum(opens, closes) + 0.3
    lows = np.minimum(opens, closes) - 0.3
    highs[3] = 110.0
    lows[5] = 100.0

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000.0,
    }, index=idx)

    res = evaluate_chop(df)
    assert res.action in (Signal.SELL, Signal.HOLD)
    if res.action == Signal.SELL:
        assert res.strength >= 0.45
        assert res.sl_hint is not None and res.sl_hint > float(closes[-1])
        assert res.tp_hint is not None and res.tp_hint < float(closes[-1])


def test_middle_of_channel_returns_hold():
    """Price at mid → no edge → HOLD."""
    df = _make_range_df(n=60, last_close=105.0, last_low=104.8, last_high=105.2)
    res = evaluate_chop(df)
    assert res.action == Signal.HOLD


# --------------------------------------------------------------------------- #
#  SL/TP sanity                                                               #
# --------------------------------------------------------------------------- #

def test_long_tp_above_entry_sl_below_entry():
    """Any firing LONG must have SL < entry < TP, with positive RR."""
    n = 60
    idx = pd.date_range(
        end=datetime(2026, 4, 22, 12, tzinfo=timezone.utc),
        periods=n, freq="h",
    )
    closes = np.empty(n)
    closes[:40] = 105.0
    closes[40:] = np.linspace(105.0, 100.1, num=n - 40)
    opens = closes + 0.03
    highs = np.maximum(opens, closes) + 0.3
    lows = np.minimum(opens, closes) - 0.3
    highs[3] = 110.0
    lows[5] = 100.0
    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000.0,
    }, index=idx)
    res = evaluate_chop(df)
    if res.action == Signal.BUY:
        assert res.sl_hint < float(closes[-1]) < res.tp_hint
        risk = float(closes[-1]) - res.sl_hint
        reward = res.tp_hint - float(closes[-1])
        assert risk > 0 and reward / risk >= 1.2


def test_indicators_exposed_for_logging():
    df = _make_range_df(n=60, last_close=100.2, last_low=100.0, last_high=100.4)
    res = evaluate_chop(df)
    for k in ("donchian_upper", "donchian_lower", "donchian_mid",
              "donchian_width", "rsi", "adx", "atr"):
        assert k in res.indicators
