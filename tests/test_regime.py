"""Unit tests for src/strategy/regime.py — focused on the deterministic
behaviours (tracker, clear trend detection)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategy.regime import Regime, RegimeTracker, detect_regime


def _trend_df(start: float, end: float, n: int = 120, noise: float = 0.05) -> pd.DataFrame:
    """Build a monotonically trending OHLCV frame with mild noise."""
    rng = np.random.default_rng(42)
    closes = np.linspace(start, end, n) + rng.normal(0, noise * start / 100, n)
    highs = closes * 1.002
    lows = closes * 0.998
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def _sideways_df(mid: float = 100, n: int = 120) -> pd.DataFrame:
    closes = mid + np.sin(np.linspace(0, 8 * np.pi, n)) * 0.5
    return pd.DataFrame({
        "open": closes,
        "high": closes * 1.001,
        "low": closes * 0.999,
        "close": closes,
        "volume": np.full(n, 1000.0),
    })


# --------------------------------------------------------------------------- #
#  detect_regime                                                              #
# --------------------------------------------------------------------------- #

def test_short_df_returns_sideways():
    df = pd.DataFrame({
        "open": [100] * 20, "high": [101] * 20, "low": [99] * 20,
        "close": [100] * 20, "volume": [1] * 20,
    })
    res = detect_regime(df, symbol="TEST/USDT", use_persistence=False)
    assert res.regime == Regime.SIDEWAYS
    assert res.score == 0.0


def test_strong_uptrend_produces_positive_trend_and_momentum():
    """A clean uptrend should produce positive trend & momentum scores.
    Final regime classification additionally requires volatility
    percentile > 0.5, so we don't assert Regime.BULL — that's a
    system-level call covered by integration / backtest."""
    df = _trend_df(100, 150, n=150)
    res = detect_regime(df, symbol="UP/USDT", use_persistence=False)
    assert res.trend_score > 0
    assert res.momentum_score > 0


def test_strong_downtrend_produces_negative_trend_and_momentum():
    df = _trend_df(150, 100, n=150)
    res = detect_regime(df, symbol="DOWN/USDT", use_persistence=False)
    assert res.trend_score < 0
    assert res.momentum_score < 0


def test_sideways_not_trending():
    df = _sideways_df(100, n=150)
    res = detect_regime(df, symbol="FLAT/USDT", use_persistence=False)
    # Chop should not confidently be bull/bear — either sideways or weak score
    assert res.regime == Regime.SIDEWAYS or abs(res.trend_score) <= 0.5


# --------------------------------------------------------------------------- #
#  RegimeTracker                                                              #
# --------------------------------------------------------------------------- #

def test_tracker_first_eval_accepts():
    tr = RegimeTracker(confirm_count=3)
    confirmed, switched = tr.get_confirmed("BTC", Regime.BULL)
    assert confirmed == Regime.BULL
    assert switched is True


def test_tracker_requires_confirmations_to_switch():
    tr = RegimeTracker(confirm_count=3)
    tr.get_confirmed("BTC", Regime.BULL)           # first -> BULL

    # One BEAR reading shouldn't flip
    confirmed, switched = tr.get_confirmed("BTC", Regime.BEAR)
    assert confirmed == Regime.BULL
    assert switched is False

    # Second BEAR still not enough
    confirmed, _ = tr.get_confirmed("BTC", Regime.BEAR)
    assert confirmed == Regime.BULL

    # Third BEAR -> switch confirmed
    confirmed, switched = tr.get_confirmed("BTC", Regime.BEAR)
    assert confirmed == Regime.BEAR
    assert switched is True


def test_tracker_reset_on_oscillation():
    tr = RegimeTracker(confirm_count=3)
    tr.get_confirmed("BTC", Regime.BULL)
    tr.get_confirmed("BTC", Regime.BEAR)      # candidate=BEAR count=1
    tr.get_confirmed("BTC", Regime.SIDEWAYS)  # candidate flips -> count=1
    # Still BULL, not enough consecutive BEAR/SIDEWAYS to switch
    confirmed, switched = tr.get_confirmed("BTC", Regime.BULL)
    assert confirmed == Regime.BULL
    assert switched is False
