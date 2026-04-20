"""Unit tests for src/execution/risk.py.

Focus areas:
  - drawdown persistence (peak_balance round-trips through state.json)
  - correlation cluster cap (BTC/ETH/SOL share the L1_MAJOR cluster)
  - trade-size cap + minimum notional
  - direction and position count limits
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pytest

from config import settings
from src.exchanges.base import Position
from src.execution import risk as risk_mod
from src.execution.risk import RiskManager, _cluster_of


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #

def _pos(symbol: str, side: str = "buy", size: float = 0.01, price: float = 100.0) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        size=size,
        entry_price=price,
        unrealized_pnl=0.0,
    )


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect risk state persistence to a temp file per test."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(risk_mod, "STATE_PATH", str(state_file))
    return state_file


# --------------------------------------------------------------------------- #
#  Clustering                                                                 #
# --------------------------------------------------------------------------- #

def test_cluster_of_known_symbol():
    assert _cluster_of("BTC/USDT") == "L1_MAJOR"
    assert _cluster_of("ETH-USDC") == "L1_MAJOR"
    assert _cluster_of("DOGE/USDT") == "MEME"
    assert _cluster_of("ARB/USDT") == "L2_ROLLUP"


def test_cluster_of_unknown_symbol_is_other():
    assert _cluster_of("RANDOM/USDT") == "OTHER"


# --------------------------------------------------------------------------- #
#  Cluster cap                                                                #
# --------------------------------------------------------------------------- #

def test_cluster_limit_blocks_third_major(tmp_state):
    rm = RiskManager(max_per_cluster=2, persist=False)
    positions = [_pos("BTC/USDT"), _pos("ETH/USDT")]
    # Adding SOL (also L1_MAJOR) should be rejected — cluster already at 2
    assert rm.check_cluster_limit(positions, "SOL/USDT") is False


def test_cluster_limit_allows_different_cluster(tmp_state):
    rm = RiskManager(max_per_cluster=2, persist=False)
    positions = [_pos("BTC/USDT"), _pos("ETH/USDT")]
    # DOGE is in MEME cluster, L1_MAJOR limit doesn't apply
    assert rm.check_cluster_limit(positions, "DOGE/USDT") is True


# --------------------------------------------------------------------------- #
#  Drawdown persistence                                                       #
# --------------------------------------------------------------------------- #

def test_peak_balance_persists_across_restart(tmp_state):
    rm = RiskManager(persist=True)
    # Simulate balance climbing to 120 then drawing down
    assert rm.check_drawdown(100.0) is False  # initialized
    assert rm.check_drawdown(120.0) is False  # new peak persisted

    # State file should now contain peak_balance=120
    assert tmp_state.exists()
    saved = json.loads(tmp_state.read_text())
    assert saved["peak_balance"] == pytest.approx(120.0)

    # New RiskManager reads persisted peak
    rm2 = RiskManager(persist=True)
    assert rm2._peak_balance == pytest.approx(120.0)


def test_drawdown_trip_returns_true(tmp_state):
    rm = RiskManager(max_drawdown_pct=10.0, persist=False)
    rm._peak_balance = 100.0
    assert rm.check_drawdown(95.0) is False       # 5% dd — ok
    assert rm.check_drawdown(89.0) is True        # 11% dd — trips


# --------------------------------------------------------------------------- #
#  Pre-trade check                                                            #
# --------------------------------------------------------------------------- #

def test_pre_trade_check_rejects_hold(tmp_state):
    rm = RiskManager(persist=False)
    allowed, reason = rm.pre_trade_check(
        action="hold", positions=[], balance={"total": 100, "free": 100},
        notional=10.0, symbol="BTC/USDT",
    )
    assert allowed is False
    assert "hold" in reason


def test_pre_trade_check_rejects_below_min_size(tmp_state, monkeypatch):
    rm = RiskManager(persist=False, max_trade_size_usdt=1000)
    monkeypatch.setattr(settings, "MIN_TRADE_SIZE_USDT", 5.0)
    allowed, reason = rm.pre_trade_check(
        action="buy", positions=[], balance={"total": 100, "free": 100},
        notional=2.0, symbol="BTC/USDT",
    )
    assert allowed is False
    assert "MIN_TRADE_SIZE_USDT" in reason


def test_pre_trade_check_blocks_cluster_full(tmp_state):
    rm = RiskManager(persist=False, max_per_cluster=1, max_positions=10,
                     max_same_direction=10, max_trade_size_usdt=1000)
    positions = [_pos("BTC/USDT")]
    allowed, reason = rm.pre_trade_check(
        action="buy", positions=positions,
        balance={"total": 1000, "free": 1000},
        notional=50.0, symbol="ETH/USDT",
    )
    assert allowed is False
    assert "cluster" in reason.lower()


def test_pre_trade_check_allows_valid_trade(tmp_state, monkeypatch):
    monkeypatch.setattr(settings, "MIN_TRADE_SIZE_USDT", 5.0)
    rm = RiskManager(
        persist=False, max_per_cluster=5, max_positions=10,
        max_same_direction=10, max_trade_size_usdt=1000,
        max_total_exposure=5.0,
    )
    allowed, reason = rm.pre_trade_check(
        action="buy", positions=[],
        balance={"total": 1000, "free": 1000},
        notional=50.0, symbol="BTC/USDT",
    )
    assert allowed is True
    assert reason == "ok"


# --------------------------------------------------------------------------- #
#  Direction / position count                                                 #
# --------------------------------------------------------------------------- #

def test_direction_limit(tmp_state):
    rm = RiskManager(persist=False, max_same_direction=2)
    positions = [_pos("BTC/USDT", "buy"), _pos("DOGE/USDT", "buy")]
    assert rm.check_direction_limit(positions, "buy") is False
    assert rm.check_direction_limit(positions, "sell") is True


def test_cap_trade_size(tmp_state):
    rm = RiskManager(persist=False, max_trade_size_usdt=50.0)
    assert rm.cap_trade_size(10.0) == 10.0
    assert rm.cap_trade_size(100.0) == 50.0


# --------------------------------------------------------------------------- #
#  vol_target_size                                                            #
# --------------------------------------------------------------------------- #

def test_vol_target_size_returns_zero_for_short_series(tmp_state):
    rm = RiskManager(persist=False, max_trade_size_usdt=10_000)
    closes = [100.0] * 5
    assert rm.vol_target_size(1000.0, closes, bars_per_day=24) == 0.0


def test_vol_target_size_returns_zero_for_flat_series(tmp_state):
    rm = RiskManager(persist=False, max_trade_size_usdt=10_000)
    # Flat closes → zero realized vol → function returns 0 so caller falls back
    closes = [100.0] * 100
    assert rm.vol_target_size(1000.0, closes, bars_per_day=24) == 0.0


def test_vol_target_size_produces_reasonable_notional(tmp_state):
    """With ~1 %/bar vol and a 1 %/day target, notional should be a small
    fraction of the balance (because daily vol ≈ 1% * sqrt(24) ≈ 4.9%)."""
    import numpy as np
    rng = np.random.default_rng(7)
    # Log-normal walk with ~1 % per-bar std so daily vol ≈ 4.9 %
    n = 100
    rets = rng.normal(0, 0.01, n)
    closes = (100 * np.exp(np.cumsum(rets))).tolist()

    rm = RiskManager(persist=False, max_trade_size_usdt=10_000)
    notional = rm.vol_target_size(
        balance=1000.0, closes=closes,
        target_daily_vol_pct=1.0, bars_per_day=24,
    )
    # With daily_vol ≈ 0.049, risk=10, notional ≈ 10 / 0.049 ≈ 204.
    # It should land clearly between 50 and 500 for this seed.
    assert 50 < notional < 500


def test_vol_target_size_scales_with_balance(tmp_state):
    """Double the balance → double the notional (holding vol constant)."""
    import numpy as np
    rng = np.random.default_rng(11)
    rets = rng.normal(0, 0.01, 80)
    closes = (100 * np.exp(np.cumsum(rets))).tolist()

    rm = RiskManager(persist=False, max_trade_size_usdt=1_000_000)
    n1 = rm.vol_target_size(1000.0, closes, bars_per_day=24)
    n2 = rm.vol_target_size(2000.0, closes, bars_per_day=24)
    assert n1 > 0 and n2 > 0
    # Allow small drift due to 50%-of-balance cap; should be close to 2x
    assert 1.8 < n2 / n1 < 2.2


def test_vol_target_size_respects_max_trade_size(tmp_state):
    """Even with tiny vol the cap kicks in."""
    import numpy as np
    rng = np.random.default_rng(3)
    rets = rng.normal(0, 0.0005, 80)     # very low vol
    closes = (100 * np.exp(np.cumsum(rets))).tolist()
    rm = RiskManager(persist=False, max_trade_size_usdt=50.0)
    notional = rm.vol_target_size(10_000.0, closes, bars_per_day=24)
    assert notional <= 50.0
