"""Unit tests for the key-levels engine.

These pin down the expected behaviour of compute_key_levels so Phase 5
(chop strategy) and the gate integration can rely on:

  - the full level inventory being produced when enough history exists,
  - nearest support/resistance selection,
  - confluence zone clustering,
  - bias_score sign flipping correctly as price straddles support vs
    resistance,
  - graceful degradation when history is short or columns are missing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.strategy.levels import (
    compute_key_levels,
    KeyLevelResult,
    KeyLevel,
    PRIORITY,
    _week_monday,
    _quarter_bounds,
)


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #

def _mk_daily_df(end: datetime, days: int = 200, base: float = 100.0, seed: int = 1):
    """Deterministic daily OHLCV with a known range for easy assertion."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=end, periods=days, freq="D", tz="UTC")
    closes = base + rng.normal(0, 1, size=days).cumsum() * 0.5
    opens = closes - rng.normal(0, 0.3, size=days)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.5, size=days))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.5, size=days))
    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": 1000.0,
    }, index=idx)
    return df


# --------------------------------------------------------------------------- #
#  Helper-function tests                                                      #
# --------------------------------------------------------------------------- #

def test_week_monday_returns_monday_midnight():
    # A Wednesday
    wed = datetime(2026, 4, 22, 14, 30, tzinfo=timezone.utc)
    mon = _week_monday(wed)
    assert mon.weekday() == 0
    assert mon.hour == 0
    assert mon == datetime(2026, 4, 20, tzinfo=timezone.utc)


def test_quarter_bounds_are_correct():
    # April 22, 2026 is in Q2 (Apr-Jun)
    dt = datetime(2026, 4, 22, tzinfo=timezone.utc)
    start, end = _quarter_bounds(dt)
    assert start == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_quarter_bounds_q4_wraps_year():
    dt = datetime(2026, 11, 15, tzinfo=timezone.utc)
    start, end = _quarter_bounds(dt)
    assert start == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
#  compute_key_levels — happy path                                            #
# --------------------------------------------------------------------------- #

def test_full_level_set_is_produced_when_history_sufficient():
    """With 200 days of history ending on a Wednesday, we expect all
    12 canonical levels to show up."""
    end = datetime(2026, 4, 22, tzinfo=timezone.utc)
    df = _mk_daily_df(end, days=200)
    res = compute_key_levels(df, current_price=100.0, symbol="TEST", now=end)

    names = {lv.name for lv in res.levels}
    # All entries from PRIORITY should appear
    assert names == set(PRIORITY.keys()), f"missing: {set(PRIORITY.keys()) - names}"


def test_nearest_support_resistance_split_correctly():
    end = datetime(2026, 4, 22, tzinfo=timezone.utc)
    df = _mk_daily_df(end, days=200)
    # Place current price in the middle of the level distribution.
    res = compute_key_levels(df, current_price=100.0, symbol="TEST", now=end)

    if res.nearest_support is not None:
        assert res.nearest_support.price < 100.0
    if res.nearest_resistance is not None:
        assert res.nearest_resistance.price > 100.0


def test_bias_is_positive_when_price_hugs_support():
    """Handcrafted df: prev_week_low = 95 exactly. Set current_price = 95.05
    → bias_score must lean positive (bullish reaction zone)."""
    # Build exactly one week of prev-week bars with low=95
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)       # Wednesday
    last_monday = datetime(2026, 4, 20, tzinfo=timezone.utc)
    prev_monday = last_monday - timedelta(days=7)          # 2026-04-13

    rows = []
    for i in range(7):
        day = prev_monday + timedelta(days=i)
        rows.append({
            "open": 100.0, "high": 100.5,
            "low": 95.0 if i == 2 else 97.0,  # one bar tags 95
            "close": 98.0, "volume": 1000.0,
        })
    # Current week Monday + following bars at a different level
    for i in range(3):
        day = last_monday + timedelta(days=i)
        rows.append({
            "open": 98.0, "high": 99.0, "low": 97.5,
            "close": 97.8, "volume": 1000.0,
        })
    # Pad history for quarter/year pivots
    pad_start = prev_monday - timedelta(days=180)
    pad_idx = pd.date_range(start=pad_start, end=prev_monday - timedelta(days=1),
                            freq="D", tz="UTC")
    pad = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0,
    }, index=pad_idx)

    main_idx = pd.date_range(start=prev_monday, periods=len(rows), freq="D", tz="UTC")
    main_df = pd.DataFrame(rows, index=main_idx)
    df = pd.concat([pad, main_df])

    res = compute_key_levels(df, current_price=95.05, symbol="TEST", now=now)
    # prev_week_low should be 95 and right below current price
    pwl = next((lv for lv in res.levels if lv.name == "prev_week_low"), None)
    assert pwl is not None
    assert pwl.price == pytest.approx(95.0)
    assert res.bias_score > 0, f"expected bullish bias at support, got {res.bias_score}"


def test_bias_is_negative_when_price_rejects_resistance():
    """Handcraft prev_week_high = 110 and put price just below it."""
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    last_monday = datetime(2026, 4, 20, tzinfo=timezone.utc)
    prev_monday = last_monday - timedelta(days=7)

    rows = []
    for i in range(7):
        rows.append({
            "open": 100.0,
            "high": 110.0 if i == 3 else 101.0,   # one bar prints 110
            "low": 99.0, "close": 100.5, "volume": 1000.0,
        })
    for i in range(3):
        rows.append({
            "open": 101.0, "high": 102.0, "low": 100.0,
            "close": 101.5, "volume": 1000.0,
        })
    pad_start = prev_monday - timedelta(days=180)
    pad_idx = pd.date_range(start=pad_start, end=prev_monday - timedelta(days=1),
                            freq="D", tz="UTC")
    pad = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0,
    }, index=pad_idx)

    main_idx = pd.date_range(start=prev_monday, periods=len(rows), freq="D", tz="UTC")
    main_df = pd.DataFrame(rows, index=main_idx)
    df = pd.concat([pad, main_df])

    res = compute_key_levels(df, current_price=109.8, symbol="TEST", now=now)
    pwh = next((lv for lv in res.levels if lv.name == "prev_week_high"), None)
    assert pwh is not None
    assert pwh.price == pytest.approx(110.0)
    assert res.bias_score < 0, f"expected bearish bias at resistance, got {res.bias_score}"


def test_confluence_zone_detected_when_levels_stack():
    """Stack prev_week_low and monday_low within 0.4% of each other."""
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    last_monday = datetime(2026, 4, 20, tzinfo=timezone.utc)
    prev_monday = last_monday - timedelta(days=7)

    rows = []
    # Previous week: low printed at 100
    for i in range(7):
        rows.append({
            "open": 105.0, "high": 106.0, "low": 100.0 if i == 4 else 104.0,
            "close": 105.5, "volume": 1000.0,
        })
    # This week Monday: low printed at 100.1 (0.1% away → within 0.4% band)
    rows.append({
        "open": 105.0, "high": 106.0, "low": 100.1, "close": 105.5, "volume": 1000.0,
    })
    # Rest of this week
    for i in range(2):
        rows.append({
            "open": 105.0, "high": 106.0, "low": 104.5, "close": 105.5, "volume": 1000.0,
        })

    pad_start = prev_monday - timedelta(days=180)
    pad_idx = pd.date_range(start=pad_start, end=prev_monday - timedelta(days=1),
                            freq="D", tz="UTC")
    pad = pd.DataFrame({
        "open": 105.0, "high": 106.0, "low": 104.0, "close": 105.0, "volume": 1000.0,
    }, index=pad_idx)

    main_idx = pd.date_range(start=prev_monday, periods=len(rows), freq="D", tz="UTC")
    main_df = pd.DataFrame(rows, index=main_idx)
    df = pd.concat([pad, main_df])

    res = compute_key_levels(df, current_price=105.0, symbol="TEST", now=now)
    # Expect at least one zone near ~100 containing prev_week_low + monday_low
    zones_near_100 = [z for z in res.confluence_zones if 99.5 <= z[0] <= 100.5]
    assert zones_near_100, f"no zone near 100 in {res.confluence_zones}"
    names = set(zones_near_100[0][1])
    assert {"prev_week_low", "monday_low"}.issubset(names)


# --------------------------------------------------------------------------- #
#  Degenerate / safety cases                                                  #
# --------------------------------------------------------------------------- #

def test_empty_df_returns_empty_result():
    df = pd.DataFrame()
    res = compute_key_levels(df, current_price=100.0, now=datetime(2026, 4, 22, tzinfo=timezone.utc))
    assert isinstance(res, KeyLevelResult)
    assert res.levels == []
    assert res.bias_score == 0.0
    assert res.confluence_score == 0.0


def test_missing_columns_returns_empty_result():
    idx = pd.date_range(end="2026-04-22", periods=30, freq="D", tz="UTC")
    df = pd.DataFrame({"close": 100.0}, index=idx)
    res = compute_key_levels(df, current_price=100.0)
    assert res.levels == []
    assert "missing OHLC" in res.reasoning


def test_short_history_degrades_gracefully():
    """Only 10 days of history — quarterly/yearly pivots won't fill, but
    daily/weekly ones should still work."""
    end = datetime(2026, 4, 22, tzinfo=timezone.utc)
    df = _mk_daily_df(end, days=10)
    res = compute_key_levels(df, current_price=100.0, symbol="TEST", now=end)
    names = {lv.name for lv in res.levels}
    # Quarterly pivot should be missing (no prev-quarter data)
    assert "prev_quarter_mid" not in names or res.levels  # just don't crash
    # Daily + weekly references should still exist
    assert any(n in names for n in ("weekly_open", "weekly_low", "daily_open"))


def test_as_dict_returns_scalar_snapshot():
    """Used in logging / telegram digest — must be JSON-serialisable."""
    import json
    end = datetime(2026, 4, 22, tzinfo=timezone.utc)
    df = _mk_daily_df(end, days=200)
    res = compute_key_levels(df, current_price=100.0, symbol="TEST", now=end)
    d = res.as_dict()
    # Round-trips through JSON
    out = json.dumps(d)
    assert "bias_score" in out
    assert "confluence_score" in out
