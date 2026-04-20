"""Unit tests for src/strategy/fvg.py.

These tests construct OHLCV DataFrames with deterministic 3-candle patterns
to assert that detect_fvgs, price_in_zone, and fvg_score behave correctly.
"""

from __future__ import annotations

import pandas as pd

from src.strategy.fvg import detect_fvgs, fvg_score, price_in_zone


def _df(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """bars: list of (open, high, low, close)."""
    return pd.DataFrame({
        "open": [b[0] for b in bars],
        "high": [b[1] for b in bars],
        "low": [b[2] for b in bars],
        "close": [b[3] for b in bars],
        "volume": [1000.0] * len(bars),
    })


def _pad(bars: list[tuple[float, float, float, float]], n: int = 30, base: float = 100.0) -> pd.DataFrame:
    """Pad left with flat candles so the fvg detector has enough history."""
    pad_bars = [(base, base + 0.1, base - 0.1, base)] * n
    return _df(pad_bars + bars)


# --------------------------------------------------------------------------- #
#  detect_fvgs                                                                #
# --------------------------------------------------------------------------- #

def test_bullish_fvg_detected():
    # c0: 100/101/99/100  ->  high=101
    # c1: 103/104/102/103 (middle, gapping up)
    # c2: 105/106/104/105 ->  low=104  --> gap 101 -> 104
    bars = [
        (100, 101, 99, 100),
        (103, 104, 102, 103),
        (105, 106, 104, 105),
    ]
    df = _pad(bars, n=20)
    zones = detect_fvgs(df, lookback=10)
    bullish = [z for z in zones if z.gap_type == "bullish"]
    assert len(bullish) >= 1
    z = bullish[-1]
    assert z.bottom == 101  # c0 high
    assert z.top == 104     # c2 low
    assert z.filled is False


def test_bearish_fvg_detected():
    # c0: 100/101/99/100   -> low=99
    # c1: 96/97/95/96
    # c2: 93/94/92/93      -> high=94  --> gap 94 -> 99
    bars = [
        (100, 101, 99, 100),
        (96, 97, 95, 96),
        (93, 94, 92, 93),
    ]
    df = _pad(bars, n=20)
    zones = detect_fvgs(df, lookback=10)
    bearish = [z for z in zones if z.gap_type == "bearish"]
    assert len(bearish) >= 1
    z = bearish[-1]
    assert z.top == 99
    assert z.bottom == 94


def test_fvg_filled_when_price_revisits():
    # Build a bullish FVG, then a later candle whose close lands inside it.
    bars = [
        (100, 101, 99, 100),
        (103, 104, 102, 103),
        (105, 106, 104, 105),
        # retrace: close at 102.5 — inside [101, 104]
        (103, 103.5, 102, 102.5),
        (102, 103, 101.5, 102.5),
    ]
    df = _pad(bars, n=10)
    zones = detect_fvgs(df, lookback=10)
    bullish = [z for z in zones if z.gap_type == "bullish"]
    # At least one bullish FVG should now be marked filled
    assert any(z.filled for z in bullish)


def test_no_fvg_when_candles_overlap():
    bars = [
        (100, 102, 98, 100),
        (101, 103, 99, 101),
        (100, 102, 98, 100),
    ]
    df = _pad(bars, n=20)
    zones = detect_fvgs(df, lookback=10)
    # No gap — no zones
    assert all(z.gap_type not in ("bullish", "bearish") or
               (z.top - z.bottom) > 0 for z in zones)
    # Specifically, the last 3-candle pattern produces no zone
    # (we accept the padded flat candles produce nothing either)
    # So zones for the last triple should be empty:
    recent = [z for z in zones if z.mid_idx >= len(df) - 3]
    assert len(recent) == 0


# --------------------------------------------------------------------------- #
#  price_in_zone                                                              #
# --------------------------------------------------------------------------- #

def test_price_in_zone_exact():
    from src.strategy.fvg import FVGZone
    z = FVGZone(top=105, bottom=100, gap_type="bullish", filled=False, mid_idx=0)
    assert price_in_zone(102.5, z) is True
    assert price_in_zone(99.0, z) is False
    assert price_in_zone(106.0, z) is False


def test_price_in_zone_with_tolerance():
    from src.strategy.fvg import FVGZone
    z = FVGZone(top=105, bottom=100, gap_type="bullish", filled=False, mid_idx=0)
    # width=5, tolerance 50% → margin 2.5 -> [97.5, 107.5]
    assert price_in_zone(98.0, z, tolerance_pct=0.5) is True
    assert price_in_zone(95.0, z, tolerance_pct=0.5) is False


# --------------------------------------------------------------------------- #
#  fvg_score                                                                  #
# --------------------------------------------------------------------------- #

def test_fvg_score_buy_when_price_in_bullish_gap():
    # Bullish FVG = gap [c0.high, c2.low] = [101, 104].
    # We need the most recent bar to be the c2 candle itself (so the gap is
    # not yet marked filled) AND its close to sit inside the gap. Closing
    # at c2.low satisfies both.
    bars = [
        (100, 101, 99, 100),    # c0  high=101
        (103, 104, 102, 103),   # c1
        (105, 106, 104, 104),   # c2  low=104, close=104 → inside gap, and last bar
    ]
    # fvg_score uses lookback=20 internally, which needs len(df) >= 22.
    df = _pad(bars, n=25)
    assert df["close"].iloc[-1] == 104
    buy, sell, meta = fvg_score(df)
    assert buy >= 3
    assert sell == 0
    assert meta["fvg_bullish"] >= 1
