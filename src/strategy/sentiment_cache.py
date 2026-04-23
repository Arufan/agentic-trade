"""Tavily cache + monthly budget tracker.

Rationale
---------
Tavily plans ship with a hard monthly credit ceiling (the live-test plan
has 2000/month). Without caching, the live loop was calling Tavily once
per symbol per cycle PLUS once per cycle for the BTC macro regime — at
3 pairs + 1 macro × 12 cycles/hour that's ~48 calls/hour, enough to
burn the entire monthly budget in less than two days.

This module wraps Tavily calls with:

  1. **TTL cache** (default 90 min): repeated lookups for the same query
     within the TTL window are served from memory. Headlines don't move
     that fast; 90 min is a sensible floor for 1H-bar trading.

  2. **Monthly budget counter**: each successful Tavily call increments a
     persisted counter scoped to the current calendar month (UTC). The
     counter rolls over automatically at month boundary.

  3. **Circuit breaker**: once the counter hits `circuit_threshold` (a
     soft cap below the true plan ceiling, default 1800), further calls
     short-circuit and return None. This prevents accidentally hitting
     402 errors in the middle of a trading day.

Thread-safety is important because Telegram command listener runs in a
background thread and may trigger manual screenings that call this in
parallel with the main loop. A single module-level lock serializes all
state access.

Public surface (only what callers need):
  - cached_tavily_search(query, fetch_fn) -> (result, status)
  - get_budget_snapshot() -> dict for observability / Telegram digest
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from config import settings
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATE_PATH = os.path.join(os.getcwd(), "data", "state.json")

# Defaults mirror settings; the actual settings lookup is deferred into
# accessor functions so tests can monkey-patch settings at runtime.
_DEFAULT_TTL = 5400          # 90 min
_DEFAULT_BUDGET = 2000       # Tavily free plan
_DEFAULT_CIRCUIT = 1800      # leave 10% headroom by default


def _ttl_seconds() -> int:
    return int(getattr(settings, "TAVILY_TTL_SECONDS", _DEFAULT_TTL))


def _monthly_budget() -> int:
    return int(getattr(settings, "TAVILY_MONTHLY_BUDGET", _DEFAULT_BUDGET))


def _circuit_threshold() -> int:
    # Default to 10% headroom below budget if not explicitly set.
    default = max(1, int(_monthly_budget() * 0.9))
    return int(getattr(settings, "TAVILY_CIRCUIT_THRESHOLD", default))


def _month_key(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# State (in-memory cache + persisted budget)
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# query -> (expiry_epoch, result_dict)
_cache: dict[str, tuple[float, dict]] = {}

# Cache of budget state to avoid re-reading state.json every call. The
# actual source of truth is state.json so multi-process runs don't diverge.
_budget_mem: dict[str, int | str] = {"month": "", "used": 0}


def _load_budget_from_disk() -> dict[str, int | str]:
    """Read tavily_budget block from state.json, defaulting to current month."""
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r") as f:
                state = json.load(f) or {}
        else:
            state = {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"tavily_cache: failed to read state.json: {e}")
        state = {}

    block = state.get("tavily_budget") or {}
    month = block.get("month") or _month_key()
    used = int(block.get("used") or 0)
    return {"month": month, "used": used}


def _save_budget_to_disk(block: dict[str, int | str]) -> None:
    """Merge budget block into state.json without clobbering other keys."""
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r") as f:
                state = json.load(f) or {}
        else:
            state = {}
    except (json.JSONDecodeError, OSError):
        state = {}

    state["tavily_budget"] = block
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.warning(f"tavily_cache: failed to persist budget: {e}")


def _ensure_budget_loaded() -> None:
    """Populate _budget_mem on first access; roll month if needed."""
    current_month = _month_key()
    if not _budget_mem["month"]:
        loaded = _load_budget_from_disk()
        _budget_mem.update(loaded)
    # Month rollover — always reset at the boundary.
    if _budget_mem["month"] != current_month:
        logger.info(
            f"tavily_cache: month rollover {_budget_mem['month']} → {current_month}"
            f" (used was {_budget_mem['used']}); resetting counter"
        )
        _budget_mem["month"] = current_month
        _budget_mem["used"] = 0
        _save_budget_to_disk(dict(_budget_mem))


def _budget_remaining() -> int:
    _ensure_budget_loaded()
    return max(0, _circuit_threshold() - int(_budget_mem["used"]))


def _increment_budget() -> None:
    _ensure_budget_loaded()
    _budget_mem["used"] = int(_budget_mem["used"]) + 1
    _save_budget_to_disk(dict(_budget_mem))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cached_tavily_search(
    query: str,
    fetch_fn: Callable[[], Optional[dict]],
) -> tuple[Optional[dict], str]:
    """Return (result, status) for the given query.

    ``fetch_fn`` is called on cache miss + budget-available to do the
    actual Tavily request; on success its return value is cached and the
    monthly counter is bumped by one. On failure the counter is NOT
    bumped (we only charge for answered calls).

    Status codes:
      - "hit"      : served from memory cache
      - "miss"     : cache miss, network call happened, result cached
      - "budget"   : circuit breaker open, returned None without calling
      - "network"  : fetch_fn returned None (network / auth / quota error)
    """
    now = time.time()
    ttl = _ttl_seconds()

    with _lock:
        entry = _cache.get(query)
        if entry is not None and entry[0] > now:
            return entry[1], "hit"

        remaining = _budget_remaining()
        if remaining <= 0:
            used = int(_budget_mem["used"])
            logger.warning(
                f"tavily_cache: circuit breaker OPEN for '{query[:40]}' — "
                f"used={used} >= threshold={_circuit_threshold()} "
                f"(budget={_monthly_budget()})"
            )
            return None, "budget"

    # Do the actual network call OUTSIDE the lock so a slow request
    # doesn't block other threads from reading cache.
    try:
        result = fetch_fn()
    except Exception as e:
        logger.warning(f"tavily_cache: fetch_fn raised for '{query[:40]}': {e}")
        result = None

    if result is None:
        return None, "network"

    with _lock:
        _cache[query] = (now + ttl, result)
        _increment_budget()
        remaining_after = _budget_remaining()

    # Soft warning at 50% and 80% of the circuit threshold.
    threshold = _circuit_threshold()
    used = int(_budget_mem["used"])
    if threshold > 0:
        pct = used / threshold
        if 0.5 <= pct < 0.55 or 0.8 <= pct < 0.82:
            logger.warning(
                f"tavily_cache: usage {used}/{threshold} "
                f"({pct*100:.0f}% of circuit threshold)"
            )

    return result, "miss"


def get_budget_snapshot() -> dict[str, Any]:
    """Read-only snapshot for observability (Telegram digest, stats log)."""
    with _lock:
        _ensure_budget_loaded()
        used = int(_budget_mem["used"])
        threshold = _circuit_threshold()
        return {
            "month": _budget_mem["month"],
            "used": used,
            "threshold": threshold,
            "budget": _monthly_budget(),
            "remaining": max(0, threshold - used),
            "cached_queries": len(_cache),
        }


def _reset_for_tests() -> None:
    """Wipe in-memory state — test-only helper."""
    with _lock:
        _cache.clear()
        _budget_mem["month"] = ""
        _budget_mem["used"] = 0
