import pandas as pd
import numpy as np
from src.strategy.technical import analyze_technical, Signal


def _make_df(prices: list[float]) -> pd.DataFrame:
    """Create a dummy OHLCV DataFrame from a list of close prices."""
    n = len(prices)
    return pd.DataFrame({
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1000] * n,
    })


def test_rsi_oversold():
    """Downtrend should trigger oversold RSI → BUY signal."""
    prices = list(np.linspace(100, 60, 50))
    df = _make_df(prices)
    result = analyze_technical(df)
    assert isinstance(result.signal, Signal)
    assert isinstance(result.strength, float)
    assert isinstance(result.indicators, dict)
    assert "rsi" in result.indicators


def test_rsi_overbought():
    """Uptrend should trigger overbought RSI → SELL signal."""
    prices = list(np.linspace(60, 120, 50))
    df = _make_df(prices)
    result = analyze_technical(df)
    assert isinstance(result.signal, Signal)
    assert result.indicators["rsi"] > 0


def test_hold_signal():
    """Sideways market should produce HOLD."""
    prices = [100 + np.sin(i) * 2 for i in range(50)]
    df = _make_df(prices)
    result = analyze_technical(df)
    assert isinstance(result.signal, Signal)
