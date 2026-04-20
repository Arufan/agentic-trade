"""Rolling time-series store for per-symbol market state.

Keeps a bounded deque of (ts_ms, price, open_interest, funding_1h) snapshots
per symbol, persisted to a JSON file so the series survives restarts.

Why: alpha modules (OI anomaly, funding contrarian) need deltas over a window
— absolute OI says nothing, a 15% OI jump in 4 hours without price moving
says a lot. Without persistence the bot would need several cycles after every
restart before the deltas stabilise.

Contract:
    - append(symbol, price, oi, funding_1h)       — push a new snapshot
    - get_series(symbol)                          — list[MarketStateSnapshot]
    - delta(symbol, field, lookback_sec)          — (oldest, newest, pct_change)
    - latest(symbol)                              — last snapshot or None

Storage: a single JSON file at `data/market_state.json` with shape:
    {"BTC/USDC": [[ts, price, oi, funding], ...], "ETH/USDC": [...]}

Retention: MAX_SNAPSHOTS (default 500) per symbol. At 5-min cycles that's ~42h
of history, enough for 4-24h lookback windows used by the alpha layer.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from src.utils.logger import logger


# Allow tests to override via monkeypatch
STATE_PATH: str = str(Path(__file__).resolve().parents[2] / "data" / "market_state.json")

MAX_SNAPSHOTS: int = 500


@dataclass
class MarketStateSnapshot:
    ts_ms: int
    price: float
    open_interest: float
    funding_1h: float

    def as_row(self) -> list:
        return [self.ts_ms, self.price, self.open_interest, self.funding_1h]

    @staticmethod
    def from_row(row: list) -> "MarketStateSnapshot":
        return MarketStateSnapshot(
            ts_ms=int(row[0]),
            price=float(row[1]),
            open_interest=float(row[2]),
            funding_1h=float(row[3]),
        )


class MarketStateStore:
    """Lightweight JSON-backed rolling store for per-symbol market state.

    Thread-safety: not enforced. The live loop touches this from a single
    thread (the main strategy loop); integration tests use isolated instances.
    """

    def __init__(self, path: Optional[str] = None, max_snapshots: int = MAX_SNAPSHOTS):
        self.path = path or STATE_PATH
        self.max_snapshots = max_snapshots
        self._data: dict[str, list[list]] = {}
        self._load()

    # ---- persistence ---- #

    def _load(self):
        if not os.path.exists(self.path):
            self._data = {}
            return
        try:
            with open(self.path, "r") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                # Validate shape: each value must be a list of 4-tuples
                self._data = {}
                for sym, rows in raw.items():
                    if isinstance(rows, list):
                        self._data[sym] = [r for r in rows if isinstance(r, list) and len(r) == 4]
            else:
                self._data = {}
        except Exception as e:
            logger.warning(f"MarketStateStore load failed ({self.path}): {e}")
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"MarketStateStore save failed ({self.path}): {e}")

    # ---- writes ---- #

    def append(self, symbol: str, price: float, open_interest: float, funding_1h: float,
               ts_ms: Optional[int] = None) -> MarketStateSnapshot:
        """Push a new snapshot for `symbol`. Drops oldest if over capacity."""
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        snap = MarketStateSnapshot(ts, float(price), float(open_interest), float(funding_1h))
        rows = self._data.setdefault(symbol, [])
        rows.append(snap.as_row())
        # Trim from the front if over capacity
        if len(rows) > self.max_snapshots:
            del rows[: len(rows) - self.max_snapshots]
        self._save()
        return snap

    # ---- reads ---- #

    def get_series(self, symbol: str) -> list[MarketStateSnapshot]:
        return [MarketStateSnapshot.from_row(r) for r in self._data.get(symbol, [])]

    def latest(self, symbol: str) -> Optional[MarketStateSnapshot]:
        rows = self._data.get(symbol)
        if not rows:
            return None
        return MarketStateSnapshot.from_row(rows[-1])

    def delta(self, symbol: str, field: str, lookback_sec: int) -> Optional[tuple[float, float, float]]:
        """Return (oldest_value, newest_value, pct_change) over the lookback
        window, or None if insufficient history.

        `field` must be one of "price", "open_interest", "funding_1h".
        pct_change is (new - old) / old, or 0.0 if old is zero.
        """
        # Validate field upfront so callers get a clear error regardless of
        # history state (tests rely on this eagerness).
        getter = {
            "price": lambda s: s.price,
            "open_interest": lambda s: s.open_interest,
            "funding_1h": lambda s: s.funding_1h,
        }.get(field)
        if getter is None:
            raise ValueError(f"Unknown field: {field}")

        series = self.get_series(symbol)
        if len(series) < 2:
            return None
        now_ms = series[-1].ts_ms
        cutoff_ms = now_ms - lookback_sec * 1000

        # Find the earliest snapshot at or after cutoff
        baseline = None
        for snap in series:
            if snap.ts_ms >= cutoff_ms:
                baseline = snap
                break
        if baseline is None or baseline is series[-1]:
            return None

        new_snap = series[-1]
        old_val = getter(baseline)
        new_val = getter(new_snap)
        pct = (new_val - old_val) / old_val if old_val else 0.0
        return (old_val, new_val, pct)

    # ---- utility ---- #

    def clear(self, symbol: Optional[str] = None):
        """Wipe all history (symbol=None) or a single symbol's history."""
        if symbol is None:
            self._data = {}
        else:
            self._data.pop(symbol, None)
        self._save()


# Module-level singleton for convenience (tests override STATE_PATH before import)
_store: Optional[MarketStateStore] = None


def get_store() -> MarketStateStore:
    """Lazy singleton. Respects the current STATE_PATH (useful for tests that
    monkeypatch the module constant before the first access)."""
    global _store
    if _store is None or _store.path != STATE_PATH:
        _store = MarketStateStore(STATE_PATH)
    return _store


def reset_store():
    """Force the next get_store() call to re-read STATE_PATH. Test helper."""
    global _store
    _store = None
