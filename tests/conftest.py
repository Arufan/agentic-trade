"""Shared test fixtures.

- MockExchange: a minimal BaseExchange implementation that records every call
  and returns configurable synthetic data, so integration tests don't need
  network access to Hyperliquid / Binance.
- patched_services: silences journal / telegram singletons so tests don't
  spam disk or a real bot token.
- tmp_state: redirects risk / trailing state persistence to a tmp_path.
- neutral_sentiment: monkeypatches analyze_sentiment to a deterministic
  neutral reading (otherwise the function tries to hit Tavily + an LLM).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from src.exchanges.base import BaseExchange, Order, OrderSide, OrderType, Position


# --------------------------------------------------------------------------- #
#  MockExchange                                                               #
# --------------------------------------------------------------------------- #

@dataclass
class MockExchange(BaseExchange):
    """Minimal BaseExchange for integration tests.

    Behaviour is entirely controlled by the attributes you set after
    construction. The `calls` dict records every invocation so tests can
    assert on interactions (orders placed, funding queried, etc.).
    """
    ohlcv_map: dict[str, list] = field(default_factory=dict)
    balance: dict = field(default_factory=lambda: {"free": 10_000.0, "used": 0.0, "total": 10_000.0})
    positions: list[Position] = field(default_factory=list)
    ticker_last: float = 50_000.0
    funding_rate_1h: float = 0.0
    open_interest_value: float = 0.0
    calls: dict[str, list[Any]] = field(default_factory=lambda: {
        "fetch_ohlcv": [], "fetch_balance": [], "place_order": [],
        "cancel_order": [], "get_positions": [], "get_ticker": [],
        "get_funding_rate": [], "place_sl_tp": [], "get_open_interest": [],
    })
    # next values to return from place_order (FIFO); default: synthesized filled.
    next_orders: list[Order] = field(default_factory=list)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        self.calls["fetch_ohlcv"].append((symbol, timeframe, limit))
        data = self.ohlcv_map.get(symbol) or self.ohlcv_map.get(("default",)) or []
        return data[-limit:] if limit else data

    def fetch_balance(self) -> dict:
        self.calls["fetch_balance"].append(())
        return dict(self.balance)

    def place_order(self, symbol: str, side, amount: float,
                    order_type=OrderType.MARKET, price=None) -> Order:
        self.calls["place_order"].append((symbol, side, amount, order_type, price))
        if self.next_orders:
            return self.next_orders.pop(0)
        side_val = getattr(side, "value", side)
        return Order(
            id=f"mock-{len(self.calls['place_order'])}",
            symbol=symbol,
            side=OrderSide(side_val) if isinstance(side_val, str) else side,
            type=order_type,
            price=price or self.ticker_last,
            amount=amount,
            status="filled",
        )

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        self.calls["cancel_order"].append((order_id, symbol))
        return {"status": "ok", "id": order_id}

    def get_positions(self) -> list[Position]:
        self.calls["get_positions"].append(())
        return list(self.positions)

    def get_ticker(self, symbol: str) -> dict:
        self.calls["get_ticker"].append((symbol,))
        return {"last": self.ticker_last}

    def get_funding_rate(self, symbol: str) -> float:
        self.calls["get_funding_rate"].append((symbol,))
        return self.funding_rate_1h

    def get_open_interest(self, symbol: str) -> float:
        self.calls["get_open_interest"].append((symbol,))
        return self.open_interest_value

    def place_sl_tp(self, symbol: str, close_side: str, amount: float,
                    sl_price: float, tp_price: float) -> dict:
        self.calls["place_sl_tp"].append((symbol, close_side, amount, sl_price, tp_price))
        return {"status": "ok", "sl": sl_price, "tp": tp_price}


# --------------------------------------------------------------------------- #
#  OHLCV builders                                                             #
# --------------------------------------------------------------------------- #

def _bars_from_closes(closes: list[float], t0_ms: int | None = None) -> list[list]:
    """Build OHLCV rows from a close series. Highs/lows are nudged off close
    so indicators that want non-zero ranges are happy."""
    if t0_ms is None:
        t0_ms = int(time.time() * 1000) - len(closes) * 3_600_000
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) * 1.002
        l = min(o, c) * 0.998
        bars.append([t0_ms + i * 3_600_000, o, h, l, c, 1000.0])
        prev = c
    return bars


@pytest.fixture
def make_uptrend_ohlcv():
    """Factory: returns a list of OHLCV rows trending gently upward with mild noise."""
    def _build(n: int = 100, start: float = 50_000, end: float = 55_000, seed: int = 1):
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, start * 0.003, n)
        closes = np.linspace(start, end, n) + noise
        return _bars_from_closes(closes.tolist())
    return _build


@pytest.fixture
def make_downtrend_ohlcv():
    def _build(n: int = 100, start: float = 55_000, end: float = 50_000, seed: int = 2):
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, start * 0.003, n)
        closes = np.linspace(start, end, n) + noise
        return _bars_from_closes(closes.tolist())
    return _build


@pytest.fixture
def make_flat_ohlcv():
    def _build(n: int = 100, mid: float = 50_000, seed: int = 3):
        rng = np.random.default_rng(seed)
        # ~0.3 % per-bar vol so daily vol ≈ 1.5 % (realistic)
        rets = rng.normal(0, 0.003, n)
        closes = (mid * np.exp(np.cumsum(rets))).tolist()
        return _bars_from_closes(closes)
    return _build


# --------------------------------------------------------------------------- #
#  Singletons silencing                                                       #
# --------------------------------------------------------------------------- #

@pytest.fixture
def patched_services(monkeypatch, tmp_path):
    """Silence global singletons: no Telegram, no journal disk writes, no risk state."""
    # Telegram
    from src.utils import telegram as telegram_mod
    monkeypatch.setattr(telegram_mod.telegram, "enabled", False, raising=False)

    # Trade journal — redirect to tmp
    from src.utils import trade_journal
    monkeypatch.setattr(trade_journal.journal, "path", str(tmp_path / "trades.json"), raising=False)

    # Risk / trailing state — redirect to tmp_path *if* the module exposes
    # a STATE_PATH constant. We guard with a try/except because some builds
    # may not have it (e.g. older risk.py variants in dev sandboxes).
    for modname in ("src.execution.risk", "src.execution.trailing", "src.data.market_state"):
        try:
            import importlib
            mod = importlib.import_module(modname)
            if getattr(mod, "STATE_PATH", None) is not None:
                monkeypatch.setattr(mod, "STATE_PATH",
                                    str(tmp_path / f"{modname.split('.')[-1]}.json"),
                                    raising=False)
        except Exception:
            pass
    yield tmp_path


@pytest.fixture
def neutral_sentiment(monkeypatch):
    """Replace analyze_sentiment with a deterministic neutral reading so the
    combined-signal path doesn't try to reach Tavily/LLM."""
    from src.strategy import sentiment as sent_mod
    from src.strategy import combined as combined_mod
    from src.strategy.sentiment import SentimentResult, Sentiment

    def _fake(symbol: str) -> SentimentResult:
        return SentimentResult(
            sentiment=Sentiment.NEUTRAL,
            confidence=0.0,
            summary="neutral (test fixture)",
            sources=[],
        )

    monkeypatch.setattr(sent_mod, "analyze_sentiment", _fake)
    # Also patch the name imported into combined.py
    monkeypatch.setattr(combined_mod, "analyze_sentiment", _fake)
    yield


@pytest.fixture
def mock_exchange(make_uptrend_ohlcv):
    ex = MockExchange()
    ex.ohlcv_map["BTC/USDT"] = make_uptrend_ohlcv()
    ex.ohlcv_map["ETH/USDT"] = make_uptrend_ohlcv(start=3_000, end=3_300)
    return ex
