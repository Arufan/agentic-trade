"""Unit tests for the Tavily TTL cache + monthly budget tracker.

The whole point of sentiment_cache is to protect the 2000-credit/month
Tavily budget from being burned in <48h by a tight live loop. These
tests lock in:

  - Cache hits don't charge the budget.
  - TTL expiry forces a re-fetch and a new charge.
  - Circuit breaker opens at the configured threshold and stays open
    until the month rolls over.
  - Month rollover resets the counter.
  - Thread-safety: no counter drift under concurrent calls.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.strategy import sentiment_cache as sc


# --------------------------------------------------------------------------- #
#  Fixtures                                                                   #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect state.json to a per-test temp file and wipe in-memory state."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(sc, "STATE_PATH", str(state_path))
    sc._reset_for_tests()
    yield
    sc._reset_for_tests()


@pytest.fixture
def short_ttl(monkeypatch):
    """Run with a 1-second TTL for easy expiry testing."""
    class _S:
        TAVILY_TTL_SECONDS = 1
        TAVILY_MONTHLY_BUDGET = 2000
        TAVILY_CIRCUIT_THRESHOLD = 1800
    monkeypatch.setattr(sc, "settings", _S)


@pytest.fixture
def tiny_budget(monkeypatch):
    """Run with a 3-call circuit threshold."""
    class _S:
        TAVILY_TTL_SECONDS = 60
        TAVILY_MONTHLY_BUDGET = 10
        TAVILY_CIRCUIT_THRESHOLD = 3
    monkeypatch.setattr(sc, "settings", _S)


# --------------------------------------------------------------------------- #
#  Cache behaviour                                                            #
# --------------------------------------------------------------------------- #

def test_cache_hit_does_not_charge_budget(short_ttl):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"results": [{"title": "hello"}]}

    r1, s1 = sc.cached_tavily_search("BTC news", fetch)
    r2, s2 = sc.cached_tavily_search("BTC news", fetch)

    assert s1 == "miss"
    assert s2 == "hit"
    assert calls["n"] == 1
    assert r1 is r2
    snap = sc.get_budget_snapshot()
    assert snap["used"] == 1


def test_ttl_expiry_triggers_refetch(short_ttl):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"results": [{"title": f"call-{calls['n']}"}]}

    r1, s1 = sc.cached_tavily_search("ETH news", fetch)
    time.sleep(1.1)  # exceeds 1-second TTL
    r2, s2 = sc.cached_tavily_search("ETH news", fetch)

    assert s1 == "miss" and s2 == "miss"
    assert calls["n"] == 2
    assert sc.get_budget_snapshot()["used"] == 2


def test_network_failure_not_charged(short_ttl):
    def fetch():
        return None  # simulate Tavily failure

    r, s = sc.cached_tavily_search("DOGE news", fetch)
    assert r is None
    assert s == "network"
    assert sc.get_budget_snapshot()["used"] == 0


def test_fetch_exception_not_charged(short_ttl):
    def fetch():
        raise RuntimeError("boom")

    r, s = sc.cached_tavily_search("SOL news", fetch)
    assert r is None
    assert s == "network"
    assert sc.get_budget_snapshot()["used"] == 0


# --------------------------------------------------------------------------- #
#  Circuit breaker                                                            #
# --------------------------------------------------------------------------- #

def test_circuit_breaker_opens_at_threshold(tiny_budget):
    """With threshold=3, the 4th unique query must be refused."""
    def fetch_ok():
        return {"results": []}

    for i in range(3):
        r, s = sc.cached_tavily_search(f"q-{i}", fetch_ok)
        assert s == "miss", f"call {i} should succeed"

    # 4th unique query triggers the breaker
    r, s = sc.cached_tavily_search("q-overflow", fetch_ok)
    assert r is None
    assert s == "budget"
    # Counter not bumped past threshold
    assert sc.get_budget_snapshot()["used"] == 3


def test_cache_hit_still_works_after_circuit_open(tiny_budget):
    """An already-cached query must keep serving from cache even when
    the breaker is open for new queries. Otherwise the bot would lose
    all sentiment mid-month."""
    def fetch_ok():
        return {"results": [{"title": "cached"}]}

    # Prime cache on one query
    sc.cached_tavily_search("popular", fetch_ok)
    # Fill budget with other queries
    sc.cached_tavily_search("other1", fetch_ok)
    sc.cached_tavily_search("other2", fetch_ok)
    # Breaker is now at threshold; cached query still serves
    r, s = sc.cached_tavily_search("popular", fetch_ok)
    assert s == "hit"
    assert r == {"results": [{"title": "cached"}]}


# --------------------------------------------------------------------------- #
#  Persistence                                                                #
# --------------------------------------------------------------------------- #

def test_budget_persists_across_process_restart(tiny_budget, tmp_path, monkeypatch):
    """State.json must carry the counter across a sim process restart."""
    def fetch_ok():
        return {"results": []}

    sc.cached_tavily_search("a", fetch_ok)
    sc.cached_tavily_search("b", fetch_ok)

    state_file = Path(sc.STATE_PATH)
    assert state_file.exists()
    persisted = json.loads(state_file.read_text())
    assert persisted["tavily_budget"]["used"] == 2

    # Simulate restart — wipe in-memory, re-read disk
    sc._reset_for_tests()
    snap = sc.get_budget_snapshot()
    assert snap["used"] == 2


def test_month_rollover_resets_counter(tiny_budget, monkeypatch):
    """When the UTC month changes, the counter must reset."""
    def fetch_ok():
        return {"results": []}

    sc.cached_tavily_search("a", fetch_ok)
    sc.cached_tavily_search("b", fetch_ok)
    assert sc.get_budget_snapshot()["used"] == 2

    # Forge the persisted month to last month, wipe in-memory, then
    # a fresh call must trigger rollover.
    sc._reset_for_tests()
    persisted = json.loads(Path(sc.STATE_PATH).read_text())
    persisted["tavily_budget"]["month"] = "2020-01"
    Path(sc.STATE_PATH).write_text(json.dumps(persisted))

    # Now load — rollover should reset to 0
    snap = sc.get_budget_snapshot()
    assert snap["used"] == 0
    assert snap["month"] != "2020-01"


def test_state_json_preserves_other_keys(tiny_budget, tmp_path):
    """We must not clobber peak_balance or trailing state when writing."""
    state_file = Path(sc.STATE_PATH)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "peak_balance": 123.45,
        "trailing": {"BTC/USDC": {"side": "buy", "sl": 60000}},
    }))

    def fetch_ok():
        return {"results": []}

    sc.cached_tavily_search("q", fetch_ok)

    persisted = json.loads(state_file.read_text())
    assert persisted["peak_balance"] == 123.45
    assert persisted["trailing"]["BTC/USDC"]["sl"] == 60000
    assert persisted["tavily_budget"]["used"] == 1


# --------------------------------------------------------------------------- #
#  Concurrency                                                                #
# --------------------------------------------------------------------------- #

def test_concurrent_calls_do_not_drift_counter(short_ttl):
    """20 threads each firing a distinct query → counter must equal 20
    (or fewer if any triggered the breaker — with budget=1800 it won't)."""
    def fetch(i):
        return lambda: {"results": [{"title": f"t{i}"}]}

    def worker(i):
        sc.cached_tavily_search(f"query-{i}", fetch(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sc.get_budget_snapshot()["used"] == 20
