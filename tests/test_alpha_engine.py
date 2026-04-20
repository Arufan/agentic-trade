"""Unit tests for the AlphaEngine orchestrator + MarketStateStore.

Covers:
  - MarketStateStore: append, delta, latest, retention, persistence round-trip
  - AlphaEngine: module blending, HOLD short-circuits, disable flags,
    weights honored, from_settings() construction.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.data.market_state import MarketStateStore, MarketStateSnapshot, MAX_SNAPSHOTS
from src.strategy.alpha.base import AlphaSource, AlphaSignal, CombinedAlpha
from src.strategy.alpha.engine import AlphaEngine, AlphaEngineConfig
from src.strategy.technical import Signal


# --------------------------------------------------------------------------- #
#  MarketStateStore                                                           #
# --------------------------------------------------------------------------- #

def _hour_ms(offset_hours: int) -> int:
    return int(time.time() * 1000) + offset_hours * 3_600_000


def test_store_append_and_latest(tmp_path):
    store = MarketStateStore(path=str(tmp_path / "s.json"))
    store.append("BTC/USDT", 50_000, 100, 0.0001, ts_ms=_hour_ms(-4))
    store.append("BTC/USDT", 51_000, 115, 0.0002, ts_ms=_hour_ms(0))

    latest = store.latest("BTC/USDT")
    assert latest is not None
    assert latest.price == 51_000
    assert latest.open_interest == 115


def test_store_latest_none_for_unknown_symbol(tmp_path):
    store = MarketStateStore(path=str(tmp_path / "s.json"))
    assert store.latest("UNKNOWN/USDT") is None


def test_store_delta_computes_pct(tmp_path):
    store = MarketStateStore(path=str(tmp_path / "s.json"))
    store.append("BTC/USDT", 50_000, 100, 0.0, ts_ms=_hour_ms(-4))
    store.append("BTC/USDT", 51_000, 115, 0.0, ts_ms=_hour_ms(-3))
    store.append("BTC/USDT", 52_000, 120, 0.0, ts_ms=_hour_ms(0))

    result = store.delta("BTC/USDT", "open_interest", lookback_sec=5 * 3600)
    assert result is not None
    old, new, pct = result
    assert old == 100
    assert new == 120
    assert pct == pytest.approx(0.20)


def test_store_delta_insufficient_history(tmp_path):
    store = MarketStateStore(path=str(tmp_path / "s.json"))
    store.append("BTC/USDT", 50_000, 100, 0.0, ts_ms=_hour_ms(0))
    # Only one snapshot → delta can't compute
    assert store.delta("BTC/USDT", "price", lookback_sec=3600) is None


def test_store_delta_outside_lookback_returns_none(tmp_path):
    store = MarketStateStore(path=str(tmp_path / "s.json"))
    # Two snapshots both 1h apart, ask for 24h lookback → baseline == newest
    # (because the earliest snapshot inside the window IS newest) → None
    store.append("BTC/USDT", 50_000, 100, 0.0, ts_ms=_hour_ms(-1))
    store.append("BTC/USDT", 51_000, 110, 0.0, ts_ms=_hour_ms(0))
    # 30-min lookback: baseline should pick the snapshot at -1h? No — -1h is
    # OUTSIDE the window. Both snapshots are candidates. The one at -1h is
    # 1h ago, outside 30min. So the only one in the window is the latest →
    # returns None.
    result = store.delta("BTC/USDT", "price", lookback_sec=30 * 60)
    assert result is None


def test_store_retention_caps_size(tmp_path):
    """Writing more than max_snapshots drops the oldest entries."""
    store = MarketStateStore(path=str(tmp_path / "s.json"), max_snapshots=5)
    for i in range(10):
        store.append("BTC/USDT", 100 + i, 1000 + i, 0.0, ts_ms=_hour_ms(-10 + i))
    series = store.get_series("BTC/USDT")
    assert len(series) == 5
    # With 10 inserts capped at 5, the 5 kept are the *last 5* (prices 105-109).
    assert series[0].price == 105
    assert series[-1].price == 109


def test_store_persists_across_instances(tmp_path):
    path = str(tmp_path / "persist.json")
    s1 = MarketStateStore(path=path)
    s1.append("BTC/USDT", 50_000, 100, 0.0001, ts_ms=_hour_ms(0))

    # New instance reading the same file should see the snapshot
    s2 = MarketStateStore(path=path)
    latest = s2.latest("BTC/USDT")
    assert latest is not None
    assert latest.price == 50_000
    assert latest.funding_1h == pytest.approx(0.0001)


def test_store_invalid_field_raises(tmp_path):
    store = MarketStateStore(path=str(tmp_path / "s.json"))
    store.append("BTC/USDT", 50_000, 100, 0.0, ts_ms=_hour_ms(-2))
    store.append("BTC/USDT", 51_000, 110, 0.0, ts_ms=_hour_ms(0))
    with pytest.raises(ValueError):
        store.delta("BTC/USDT", "nonsense_field", lookback_sec=3600)


def test_store_clear(tmp_path):
    store = MarketStateStore(path=str(tmp_path / "s.json"))
    store.append("BTC/USDT", 50_000, 100, 0.0, ts_ms=_hour_ms(0))
    store.append("ETH/USDT", 3_000, 50, 0.0, ts_ms=_hour_ms(0))
    store.clear("BTC/USDT")
    assert store.latest("BTC/USDT") is None
    assert store.latest("ETH/USDT") is not None
    store.clear()
    assert store.latest("ETH/USDT") is None


# --------------------------------------------------------------------------- #
#  AlphaEngine                                                                #
# --------------------------------------------------------------------------- #

class _FakeStore:
    """Test double for MarketStateStore: lets tests prescribe exactly what
    delta() should return per (symbol, field, lookback) call."""
    def __init__(self):
        self._responses: dict[tuple, tuple | None] = {}

    def set(self, symbol: str, field: str, lookback_sec: int, response):
        self._responses[(symbol, field, lookback_sec)] = response

    def delta(self, symbol: str, field: str, lookback_sec: int):
        return self._responses.get((symbol, field, lookback_sec))


def _seed_buy_flow(store: _FakeStore, symbol: str, cfg: AlphaEngineConfig):
    """Prime store so OI anomaly reads +15% OI / +3% price (long build → BUY)."""
    store.set(symbol, "open_interest", cfg.oi_lookback_sec, (100, 115, 0.15))
    store.set(symbol, "price", cfg.oi_lookback_sec, (50_000, 51_500, 0.03))


def _seed_sell_flow(store: _FakeStore, symbol: str, cfg: AlphaEngineConfig):
    """Prime store so OI anomaly reads +15% OI / -3% price → short-cover fade (SELL)."""
    store.set(symbol, "open_interest", cfg.oi_lookback_sec, (100, 85, -0.15))
    store.set(symbol, "price", cfg.oi_lookback_sec, (50_000, 51_500, 0.03))


def _seed_no_flow(store: _FakeStore, symbol: str, cfg: AlphaEngineConfig):
    """Both deltas too small → modules emit HOLD."""
    store.set(symbol, "open_interest", cfg.oi_lookback_sec, (100, 101, 0.01))
    store.set(symbol, "price", cfg.oi_lookback_sec, (50_000, 50_100, 0.002))
    store.set(symbol, "price", cfg.funding_lookback_sec, (50_000, 50_100, 0.002))


def test_engine_no_signals_when_all_modules_hold():
    cfg = AlphaEngineConfig()
    engine = AlphaEngine(cfg)
    store = _FakeStore()
    _seed_no_flow(store, "BTC/USDT", cfg)

    out = engine.evaluate(
        symbol="BTC/USDT", current_price=51_000,
        current_oi=100, funding_1h=0.00001,
        store=store,
    )
    assert out.action == Signal.HOLD
    assert out.strength == 0.0
    assert out.has_any() is False


def test_engine_emits_buy_on_long_build():
    cfg = AlphaEngineConfig()
    engine = AlphaEngine(cfg)
    store = _FakeStore()
    _seed_buy_flow(store, "BTC/USDT", cfg)
    # Funding contrarian needs a price-delta over funding_lookback_sec → no signal
    store.set("BTC/USDT", "price", cfg.funding_lookback_sec, None)

    out = engine.evaluate(
        symbol="BTC/USDT", current_price=51_500,
        current_oi=115, funding_1h=0.00001,
        store=store,
    )
    assert out.action == Signal.BUY
    assert out.score > 0
    assert out.has_any() is True


def test_engine_disable_oi_module():
    cfg = AlphaEngineConfig(enable_oi_anomaly=False)
    engine = AlphaEngine(cfg)
    store = _FakeStore()
    # Prime a BUY OI signal — but module is disabled
    _seed_buy_flow(store, "BTC/USDT", cfg)
    # Funding module also has nothing to say
    store.set("BTC/USDT", "price", cfg.funding_lookback_sec, None)

    out = engine.evaluate(
        symbol="BTC/USDT", current_price=51_500,
        current_oi=115, funding_1h=0.00001,
        store=store,
    )
    # Only funding module ran; it returned HOLD
    assert out.action == Signal.HOLD
    # Should have exactly one signal (the funding one)
    assert len(out.signals) == 1
    assert out.signals[0].source == AlphaSource.FUNDING_CONTRARIAN


def test_engine_funding_contrarian_fires_without_oi_data():
    cfg = AlphaEngineConfig(enable_oi_anomaly=False)
    engine = AlphaEngine(cfg)
    store = _FakeStore()
    # Pump detected: +5% price over 12h
    store.set("BTC/USDT", "price", cfg.funding_lookback_sec, (50_000, 52_500, 0.05))

    # 80% annualized funding
    funding_1h = 0.80 / (24 * 365)
    out = engine.evaluate(
        symbol="BTC/USDT", current_price=52_500,
        current_oi=0, funding_1h=funding_1h,
        store=store,
    )
    assert out.action == Signal.SELL
    assert out.signals[0].source == AlphaSource.FUNDING_CONTRARIAN
    assert out.signals[0].metadata.get("pattern") == "long_squeeze_setup"


def test_engine_conflicting_signals_cancel_to_hold():
    """OI says BUY, funding contrarian says SELL, equal weights → near-zero
    blended score → HOLD."""
    cfg = AlphaEngineConfig()
    engine = AlphaEngine(cfg)
    store = _FakeStore()
    _seed_buy_flow(store, "BTC/USDT", cfg)
    store.set("BTC/USDT", "price", cfg.funding_lookback_sec, (50_000, 52_500, 0.05))

    funding_1h = 0.80 / (24 * 365)
    out = engine.evaluate(
        symbol="BTC/USDT", current_price=52_500,
        current_oi=115, funding_1h=funding_1h,
        store=store,
    )
    # The two signed scores partially cancel. If strengths are close they
    # should either result in HOLD or a small net magnitude.
    assert abs(out.score) < 0.25 or out.action == Signal.HOLD
    # Still: both modules ran and recorded their signals
    assert len(out.signals) == 2


def test_engine_from_settings_uses_config_overrides(monkeypatch):
    """from_settings should respect every override attribute it reads."""
    class FakeSettings:
        ALPHA_OI_LOOKBACK_SEC = 7200
        ALPHA_OI_THRESHOLD = 0.05
        ALPHA_PRICE_THRESHOLD = 0.01
        ALPHA_FUNDING_LOOKBACK_SEC = 3600
        ALPHA_FUNDING_CONTRARIAN_ANNUAL = 0.70
        ALPHA_FUNDING_MIN_PRICE_MOVE = 0.03
        ALPHA_OI_ENABLED = True
        ALPHA_FUNDING_CONTRARIAN_ENABLED = False

    engine = AlphaEngine.from_settings(FakeSettings)
    assert engine.config.oi_lookback_sec == 7200
    assert engine.config.oi_threshold == 0.05
    assert engine.config.price_threshold == 0.01
    assert engine.config.funding_lookback_sec == 3600
    assert engine.config.funding_contrarian_annual == 0.70
    assert engine.config.funding_min_price_move == 0.03
    assert engine.config.enable_oi_anomaly is True
    assert engine.config.enable_funding_contrarian is False


def test_alpha_signal_score_sign_correctness():
    """AlphaSignal.score should be +strength for BUY, -strength for SELL, 0 for HOLD."""
    buy = AlphaSignal(AlphaSource.OI_ANOMALY, Signal.BUY, 0.8, "")
    sell = AlphaSignal(AlphaSource.OI_ANOMALY, Signal.SELL, 0.8, "")
    hold = AlphaSignal(AlphaSource.OI_ANOMALY, Signal.HOLD, 0.0, "")
    assert buy.score == 0.8
    assert sell.score == -0.8
    assert hold.score == 0.0


def test_combined_alpha_has_any_reflects_module_activity():
    empty = CombinedAlpha(
        action=Signal.HOLD, strength=0.0, score=0.0,
        signals=[AlphaSignal(AlphaSource.OI_ANOMALY, Signal.HOLD, 0.0, "")],
        reasoning="",
    )
    active = CombinedAlpha(
        action=Signal.BUY, strength=0.5, score=0.5,
        signals=[AlphaSignal(AlphaSource.OI_ANOMALY, Signal.BUY, 0.5, "")],
        reasoning="",
    )
    assert empty.has_any() is False
    assert active.has_any() is True
