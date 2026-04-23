"""Economic calendar awareness for the entry gate + position sizing.

Fetches a week's worth of high-impact macro events once per 24h, caches
locally, and exposes pure helpers used by main.py:

    load_or_refresh()         – get current snapshot (refresh if stale)
    is_in_blackout()          – hard-skip new entries T-N min before event
    get_size_modifier()       – shrink notional ± window_h hours around event
    next_event()              – soonest upcoming event (for Telegram warning)

Two fetch paths:
    1. Primary – direct HTTPS to ff_calendar_thisweek.json (free, 0 auth)
    2. Fallback – Firecrawl proxy to the SAME URL (when direct fetch is
       blocked by ISP / corp firewall). Uses <1 credit/day.

Design invariants (locked by tests/test_econ_calendar.py):
    * All EconEvent.timestamp_utc are UTC-aware
    * is_stale() compares timezone-aware datetimes only
    * Parse failures SKIP the row, never crash the whole fetch
    * When all fetches fail AND a cache exists, we return the stale cache
      rather than an empty result (better last-known-good than nothing).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Iterable, Optional

import requests

from src.utils.logger import logger


# --------------------------------------------------------------------------- #
#  Event whitelist — only these titles are treated as market-moving           #
# --------------------------------------------------------------------------- #

# Keyword match is case-insensitive substring. Ordered roughly by
# expected BTC impact. Any event whose title contains ANY of these
# keywords is considered tradable-risk.
HIGH_IMPACT_TITLES: tuple[str, ...] = (
    # Rate decisions — the big one
    "FOMC", "Federal Funds", "Rate Decision", "Interest Rate",
    # Fed communications
    "FOMC Minutes", "FOMC Statement", "Press Conference",
    "Fed Chair", "Powell", "Jerome Powell",
    # Inflation
    "CPI", "Core CPI", "PPI", "Core PPI",
    "PCE", "Core PCE", "Price Index",
    # Labor
    "Non-Farm", "Nonfarm", "NFP", "Unemployment Rate",
    "Average Hourly Earnings", "Employment Change",
    # Growth
    "GDP", "Advance GDP",
    # Consumer / activity
    "Retail Sales",
    # Manufacturing
    "ISM Manufacturing", "ISM Services", "ISM Non-Manufacturing",
)


DEFAULT_CURRENCIES: tuple[str, ...] = ("USD",)   # crypto follows USD macro
DEFAULT_IMPACTS: tuple[str, ...] = ("High",)     # skip Medium/Low noise

DEFAULT_CACHE_PATH = "data/econ_calendar.json"

# ForexFactory's unofficial-but-stable weekly JSON feed. No API key.
FF_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


# --------------------------------------------------------------------------- #
#  Data classes                                                               #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EconEvent:
    timestamp_utc: datetime   # always tz-aware UTC
    currency: str             # "USD"
    title: str                # "Core CPI m/m"
    impact: str               # "High" | "Medium" | "Low" | "Holiday"
    forecast: Optional[str] = None
    previous: Optional[str] = None
    actual: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp_utc"] = self.timestamp_utc.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EconEvent":
        ts = d["timestamp_utc"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return cls(
            timestamp_utc=ts,
            currency=str(d.get("currency", "")).upper(),
            title=str(d.get("title", "")),
            impact=str(d.get("impact", "")).title(),
            forecast=d.get("forecast") or None,
            previous=d.get("previous") or None,
            actual=d.get("actual") or None,
        )


@dataclass
class CalendarSnapshot:
    events: list[EconEvent]
    fetched_at: datetime      # tz-aware UTC
    source: str               # "faireconomy_json" | "firecrawl" | "cache"

    def is_stale(self, now: datetime, refresh_hours: int) -> bool:
        return (now - self.fetched_at) > timedelta(hours=refresh_hours)


# --------------------------------------------------------------------------- #
#  Parsing                                                                    #
# --------------------------------------------------------------------------- #

def is_high_impact_title(title: str) -> bool:
    """True if the event title matches any whitelisted keyword."""
    if not title:
        return False
    t = title.lower()
    return any(kw.lower() in t for kw in HIGH_IMPACT_TITLES)


def _parse_faireconomy_row(raw: dict) -> Optional[EconEvent]:
    """Parse one faireconomy JSON row. Returns None on unrecoverable errors.

    Feed shape (stable since ~2019):
        {
          "title":   "Core CPI m/m",
          "country": "USD",
          "date":    "2026-04-10T08:30:00-04:00",
          "impact":  "High",
          "forecast":"0.3%", "previous":"0.4%", "actual":""
        }
    """
    try:
        date_str = raw.get("date", "")
        if not date_str:
            return None
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            # faireconomy always includes tz offset; bare-naive is malformed
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        ccy = str(raw.get("country") or raw.get("currency") or "").upper().strip()
        if not ccy:
            return None
        title = str(raw.get("title") or "").strip()
        if not title:
            return None
        impact = str(raw.get("impact") or "Low").title()
        return EconEvent(
            timestamp_utc=dt,
            currency=ccy,
            title=title,
            impact=impact,
            forecast=(raw.get("forecast") or None) or None,
            previous=(raw.get("previous") or None) or None,
            actual=(raw.get("actual") or None) or None,
        )
    except Exception as e:
        logger.debug(f"skip unparseable calendar row {raw!r}: {e}")
        return None


def _parse_faireconomy_payload(payload) -> list[EconEvent]:
    """Accept either a raw JSON list, a JSON text string, or a Firecrawl
    rawHtml wrapper containing a JSON array. Returns parsed events."""
    data = payload
    if isinstance(payload, str):
        # Firecrawl rawHtml for a JSON endpoint often double-wraps:
        #   <html><body><pre>[{...}]</pre></body></html>
        m = re.search(r"\[\s*\{.*?\}\s*\]", payload, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return []
        else:
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return []
    if not isinstance(data, list):
        return []
    events: list[EconEvent] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        ev = _parse_faireconomy_row(raw)
        if ev is not None:
            events.append(ev)
    return events


# --------------------------------------------------------------------------- #
#  Fetchers                                                                   #
# --------------------------------------------------------------------------- #

def fetch_from_faireconomy(
    url: str = FF_JSON_URL,
    timeout: int = 15,
    session: Optional[requests.Session] = None,
) -> list[EconEvent]:
    """Direct GET of the ForexFactory weekly JSON. No API key needed.

    Raises on HTTP errors so the caller can fall back to Firecrawl.
    """
    sess = session or requests.Session()
    resp = sess.get(
        url, timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (agentic-trade)"},
    )
    resp.raise_for_status()
    events = _parse_faireconomy_payload(resp.json())
    logger.info(f"econ_calendar: fetched {len(events)} events via faireconomy JSON")
    return events


def fetch_via_firecrawl(
    api_key: str,
    url: str = FF_JSON_URL,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> list[EconEvent]:
    """Firecrawl proxy fetch — used when direct HTTP is blocked.

    Scrapes the same JSON URL via Firecrawl v1/scrape and pulls the rawHtml
    back. Costs 1 credit per call; we cache for 24h so monthly usage is ~30.
    """
    if not api_key:
        return []
    sess = session or requests.Session()
    resp = sess.post(
        "https://api.firecrawl.dev/v1/scrape",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": url, "formats": ["rawHtml", "markdown"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success", False):
        logger.warning(f"Firecrawl returned success=false: {payload}")
        return []
    data = payload.get("data", {}) or {}
    raw = data.get("rawHtml") or data.get("html") or data.get("markdown") or ""
    events = _parse_faireconomy_payload(raw)
    logger.info(f"econ_calendar: fetched {len(events)} events via Firecrawl proxy")
    return events


def fetch_events(
    firecrawl_key: str = "",
    prefer_firecrawl: bool = False,
    session: Optional[requests.Session] = None,
) -> tuple[list[EconEvent], str]:
    """Try sources in order; return (events, source_tag). Empty list on total
    failure — caller decides whether to fall back to stale cache.
    """
    sources = (
        ("firecrawl", "faireconomy_json")
        if prefer_firecrawl
        else ("faireconomy_json", "firecrawl")
    )
    last_err: Optional[Exception] = None
    for src in sources:
        try:
            if src == "faireconomy_json":
                events = fetch_from_faireconomy(session=session)
            else:
                if not firecrawl_key:
                    continue
                events = fetch_via_firecrawl(firecrawl_key, session=session)
            if events:
                return events, src
        except Exception as e:
            logger.warning(f"econ_calendar: {src} fetch failed: {e}")
            last_err = e
    if last_err is None:
        logger.warning("econ_calendar: no source configured returned events")
    return [], "none"


# --------------------------------------------------------------------------- #
#  Cache I/O                                                                  #
# --------------------------------------------------------------------------- #

def load_snapshot(path: str = DEFAULT_CACHE_PATH) -> Optional[CalendarSnapshot]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"econ_calendar: failed to read {path}: {e}")
        return None
    try:
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        events = [EconEvent.from_dict(e) for e in data.get("events", [])]
        return CalendarSnapshot(
            events=events,
            fetched_at=fetched_at,
            source=str(data.get("source", "cache")),
        )
    except Exception as e:
        logger.warning(f"econ_calendar: corrupt cache {path}: {e}")
        return None


def save_snapshot(snapshot: CalendarSnapshot, path: str = DEFAULT_CACHE_PATH) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    payload = {
        "fetched_at": snapshot.fetched_at.isoformat(),
        "source": snapshot.source,
        "events": [e.to_dict() for e in snapshot.events],
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def load_or_refresh(
    firecrawl_key: str = "",
    refresh_hours: int = 24,
    cache_path: str = DEFAULT_CACHE_PATH,
    prefer_firecrawl: bool = False,
    now: Optional[datetime] = None,
    session: Optional[requests.Session] = None,
) -> CalendarSnapshot:
    """Return a live snapshot. Refresh only when cache is missing or stale.

    On total fetch failure, returns the stale cache rather than an empty
    snapshot — operators prefer last-known-good over silent blindness.
    """
    now = now or datetime.now(timezone.utc)
    cached = load_snapshot(cache_path)

    if cached is not None and not cached.is_stale(now, refresh_hours):
        return cached

    events, source = fetch_events(
        firecrawl_key=firecrawl_key,
        prefer_firecrawl=prefer_firecrawl,
        session=session,
    )
    if not events:
        if cached is not None:
            logger.warning(
                f"econ_calendar: refresh failed; using stale cache "
                f"({(now - cached.fetched_at).total_seconds() / 3600:.1f}h old)"
            )
            return cached
        # No cache AND no fetch — return empty snapshot so callers don't crash
        return CalendarSnapshot(events=[], fetched_at=now, source="none")

    snap = CalendarSnapshot(events=events, fetched_at=now, source=source)
    try:
        save_snapshot(snap, cache_path)
    except Exception as e:
        logger.warning(f"econ_calendar: cache write failed: {e}")
    return snap


# --------------------------------------------------------------------------- #
#  Pure decision helpers — used by gate + sizing                              #
# --------------------------------------------------------------------------- #

def _event_matches_filter(
    ev: EconEvent,
    currencies: set[str],
    impacts: set[str],
) -> bool:
    if ev.currency.upper() not in currencies:
        return False
    if ev.impact.lower() not in impacts:
        return False
    if not is_high_impact_title(ev.title):
        return False
    return True


def is_in_blackout(
    now: datetime,
    events: Iterable[EconEvent],
    blackout_min: int,
    currencies: Iterable[str] = DEFAULT_CURRENCIES,
    impacts: Iterable[str] = DEFAULT_IMPACTS,
) -> tuple[bool, Optional[EconEvent]]:
    """True iff any matching event is within [now, now + blackout_min).

    Rationale: T-N min before a HIGH USD event liquidity dries up and spread
    widens 5-10x — entering a position in that window is net-negative for
    trend/chop retail. We hard-reject new entries and leave SL/TP to manage
    existing ones.
    """
    if blackout_min <= 0 or not events:
        return False, None
    curr = {c.upper() for c in currencies}
    imp = {i.lower() for i in impacts}
    end = now + timedelta(minutes=blackout_min)
    soonest: Optional[EconEvent] = None
    for ev in events:
        if not _event_matches_filter(ev, curr, imp):
            continue
        if now <= ev.timestamp_utc <= end:
            if soonest is None or ev.timestamp_utc < soonest.timestamp_utc:
                soonest = ev
    return (soonest is not None), soonest


def get_size_modifier(
    now: datetime,
    events: Iterable[EconEvent],
    window_h: float,
    size_mult: float,
    currencies: Iterable[str] = DEFAULT_CURRENCIES,
    impacts: Iterable[str] = DEFAULT_IMPACTS,
) -> tuple[float, Optional[EconEvent]]:
    """If any matching event is within ±window_h of now, return size_mult
    and the triggering event. Else return (1.0, None)."""
    if window_h <= 0 or not events:
        return 1.0, None
    curr = {c.upper() for c in currencies}
    imp = {i.lower() for i in impacts}
    window_s = window_h * 3600.0
    for ev in events:
        if not _event_matches_filter(ev, curr, imp):
            continue
        if abs((ev.timestamp_utc - now).total_seconds()) <= window_s:
            return float(size_mult), ev
    return 1.0, None


def next_event(
    now: datetime,
    events: Iterable[EconEvent],
    currencies: Iterable[str] = DEFAULT_CURRENCIES,
    impacts: Iterable[str] = DEFAULT_IMPACTS,
    within_hours: Optional[float] = None,
) -> Optional[EconEvent]:
    """Soonest MATCHING event strictly in the future, or None."""
    curr = {c.upper() for c in currencies}
    imp = {i.lower() for i in impacts}
    cutoff = now + timedelta(hours=within_hours) if within_hours else None
    soonest: Optional[EconEvent] = None
    for ev in events:
        if ev.timestamp_utc <= now:
            continue
        if cutoff is not None and ev.timestamp_utc > cutoff:
            continue
        if not _event_matches_filter(ev, curr, imp):
            continue
        if soonest is None or ev.timestamp_utc < soonest.timestamp_utc:
            soonest = ev
    return soonest


def format_event_for_log(ev: EconEvent, now: Optional[datetime] = None) -> str:
    """Human-readable one-liner for logs + Telegram."""
    now = now or datetime.now(timezone.utc)
    delta = ev.timestamp_utc - now
    total_s = delta.total_seconds()
    if total_s >= 0:
        hrs = total_s / 3600.0
        when = f"in {hrs:.1f}h" if hrs >= 1 else f"in {total_s/60.0:.0f}m"
    else:
        hrs = -total_s / 3600.0
        when = f"{hrs:.1f}h ago"
    return f"{ev.currency} {ev.title} ({ev.impact}) — {when}"
