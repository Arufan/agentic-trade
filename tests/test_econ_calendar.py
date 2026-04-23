"""Unit tests for the economic calendar module.

Covers parsing, blackout window, size modifier, next-event picker,
cache roundtrip, and the stale-fallback behaviour when all fetches
fail. These are the contracts the live loop relies on — they MUST
remain stable.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from src.strategy.econ_calendar import (
    CalendarSnapshot,
    EconEvent,
    FF_JSON_URL,
    _parse_faireconomy_payload,
    _parse_faireconomy_row,
    fetch_events,
    fetch_from_faireconomy,
    fetch_via_firecrawl,
    format_event_for_log,
    get_size_modifier,
    is_high_impact_title,
    is_in_blackout,
    load_or_refresh,
    load_snapshot,
    next_event,
    save_snapshot,
)


# --------------------------------------------------------------------------- #
#  Factories                                                                  #
# --------------------------------------------------------------------------- #

UTC = timezone.utc


def _mk_event(offset_h: float, *,
              title: str = "FOMC Rate Decision",
              currency: str = "USD",
              impact: str = "High",
              base: datetime | None = None) -> EconEvent:
    base = base or datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    return EconEvent(
        timestamp_utc=base + timedelta(hours=offset_h),
        currency=currency,
        title=title,
        impact=impact,
    )


# --------------------------------------------------------------------------- #
#  Title whitelist                                                            #
# --------------------------------------------------------------------------- #

class TestHighImpactTitle:

    def test_fomc_matches(self):
        assert is_high_impact_title("FOMC Rate Decision")
        assert is_high_impact_title("FOMC Meeting Minutes")

    def test_cpi_matches(self):
        assert is_high_impact_title("Core CPI m/m")
        assert is_high_impact_title("CPI y/y")

    def test_nfp_matches(self):
        assert is_high_impact_title("Non-Farm Payrolls")
        assert is_high_impact_title("Nonfarm Employment Change")

    def test_pce_matches(self):
        assert is_high_impact_title("Core PCE Price Index m/m")

    def test_noise_does_not_match(self):
        assert not is_high_impact_title("TIPP Economic Optimism")
        assert not is_high_impact_title("")
        assert not is_high_impact_title(None)  # type: ignore[arg-type]

    def test_case_insensitive(self):
        assert is_high_impact_title("fomc press conference")
        assert is_high_impact_title("FOMC PRESS CONFERENCE")


# --------------------------------------------------------------------------- #
#  Row parser                                                                 #
# --------------------------------------------------------------------------- #

class TestRowParser:

    def test_valid_row(self):
        row = {
            "title": "Core CPI m/m",
            "country": "USD",
            "date": "2026-04-10T08:30:00-04:00",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.4%",
            "actual": "",
        }
        ev = _parse_faireconomy_row(row)
        assert ev is not None
        assert ev.currency == "USD"
        assert ev.title == "Core CPI m/m"
        assert ev.impact == "High"
        # Converted to UTC (adds 4h to the -04:00 offset)
        assert ev.timestamp_utc == datetime(2026, 4, 10, 12, 30, tzinfo=UTC)
        assert ev.forecast == "0.3%"
        assert ev.actual is None  # empty string coerced to None

    def test_missing_date_returns_none(self):
        assert _parse_faireconomy_row({"title": "x", "country": "USD"}) is None

    def test_empty_currency_returns_none(self):
        row = {"title": "x", "country": "", "date": "2026-04-10T08:30:00Z"}
        assert _parse_faireconomy_row(row) is None

    def test_empty_title_returns_none(self):
        row = {"title": "", "country": "USD", "date": "2026-04-10T08:30:00Z"}
        assert _parse_faireconomy_row(row) is None

    def test_bad_date_returns_none(self):
        row = {"title": "x", "country": "USD", "date": "not-a-date"}
        assert _parse_faireconomy_row(row) is None

    def test_fallback_timezone_is_utc(self):
        """A naive date string shouldn't crash; we default to UTC."""
        # fromisoformat accepts "2026-04-10T08:30:00" and returns naive dt
        # which our parser should coerce to UTC.
        row = {"title": "CPI", "country": "USD", "date": "2026-04-10T08:30:00"}
        ev = _parse_faireconomy_row(row)
        assert ev is not None
        assert ev.timestamp_utc.tzinfo is timezone.utc


# --------------------------------------------------------------------------- #
#  Payload parser (handles list / json-string / wrapped html)                 #
# --------------------------------------------------------------------------- #

class TestPayloadParser:

    _SAMPLE = [
        {"title": "FOMC Rate Decision", "country": "USD",
         "date": "2026-05-01T14:00:00-04:00", "impact": "High"},
        {"title": "German Ifo", "country": "EUR",
         "date": "2026-05-02T08:00:00+01:00", "impact": "Medium"},
    ]

    def test_accepts_list(self):
        out = _parse_faireconomy_payload(self._SAMPLE)
        assert len(out) == 2
        assert out[0].title == "FOMC Rate Decision"

    def test_accepts_json_string(self):
        out = _parse_faireconomy_payload(json.dumps(self._SAMPLE))
        assert len(out) == 2

    def test_accepts_html_wrapped_json(self):
        wrapped = f"<html><body><pre>{json.dumps(self._SAMPLE)}</pre></body></html>"
        out = _parse_faireconomy_payload(wrapped)
        assert len(out) == 2
        assert out[1].currency == "EUR"

    def test_empty_string_returns_empty(self):
        assert _parse_faireconomy_payload("") == []

    def test_garbage_returns_empty(self):
        assert _parse_faireconomy_payload("not json at all") == []

    def test_skips_broken_rows(self):
        mixed = self._SAMPLE + [{"title": "", "country": "USD", "date": ""}]
        out = _parse_faireconomy_payload(mixed)
        assert len(out) == 2  # bad row dropped, not crashed


# --------------------------------------------------------------------------- #
#  Direct HTTP fetch (mocked)                                                 #
# --------------------------------------------------------------------------- #

class TestFetchFromFaireconomy:

    def test_returns_parsed_events(self):
        sample = [
            {"title": "FOMC Rate Decision", "country": "USD",
             "date": "2026-05-01T14:00:00-04:00", "impact": "High"},
        ]
        resp = MagicMock()
        resp.json.return_value = sample
        resp.raise_for_status = MagicMock()
        session = MagicMock()
        session.get.return_value = resp
        events = fetch_from_faireconomy(session=session)
        assert len(events) == 1
        assert events[0].currency == "USD"
        session.get.assert_called_once()
        url_arg = session.get.call_args[0][0]
        assert url_arg == FF_JSON_URL

    def test_raises_on_http_error(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("boom")
        session = MagicMock()
        session.get.return_value = resp
        with pytest.raises(Exception):
            fetch_from_faireconomy(session=session)


class TestFirecrawlFallback:

    def test_empty_key_returns_empty(self):
        assert fetch_via_firecrawl("") == []

    def test_success_parses_payload(self):
        sample = [{"title": "Core CPI m/m", "country": "USD",
                   "date": "2026-04-10T08:30:00-04:00", "impact": "High"}]
        payload = {
            "success": True,
            "data": {"rawHtml": f"<pre>{json.dumps(sample)}</pre>"},
        }
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        session = MagicMock()
        session.post.return_value = resp
        events = fetch_via_firecrawl("fc-test-key", session=session)
        assert len(events) == 1
        assert events[0].title == "Core CPI m/m"
        # Check API key got forwarded correctly
        headers = session.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer fc-test-key"

    def test_firecrawl_success_false_returns_empty(self):
        resp = MagicMock()
        resp.json.return_value = {"success": False, "error": "quota"}
        resp.raise_for_status = MagicMock()
        session = MagicMock()
        session.post.return_value = resp
        assert fetch_via_firecrawl("fc-test-key", session=session) == []


class TestFetchEventsStrategy:

    def test_primary_success_skips_firecrawl(self):
        sample = [{"title": "FOMC", "country": "USD",
                   "date": "2026-05-01T14:00:00-04:00", "impact": "High"}]
        resp_get = MagicMock()
        resp_get.json.return_value = sample
        resp_get.raise_for_status = MagicMock()
        session = MagicMock()
        session.get.return_value = resp_get
        events, source = fetch_events(firecrawl_key="fc-key", session=session)
        assert len(events) == 1
        assert source == "faireconomy_json"
        session.post.assert_not_called()  # never went to Firecrawl

    def test_primary_fails_falls_back_to_firecrawl(self):
        # faireconomy HTTP raises
        resp_get = MagicMock()
        resp_get.raise_for_status.side_effect = Exception("blocked")
        # Firecrawl returns valid payload
        sample = [{"title": "CPI", "country": "USD",
                   "date": "2026-04-10T08:30:00-04:00", "impact": "High"}]
        resp_post = MagicMock()
        resp_post.json.return_value = {
            "success": True, "data": {"rawHtml": json.dumps(sample)},
        }
        resp_post.raise_for_status = MagicMock()
        session = MagicMock()
        session.get.return_value = resp_get
        session.post.return_value = resp_post
        events, source = fetch_events(firecrawl_key="fc-key", session=session)
        assert len(events) == 1
        assert source == "firecrawl"

    def test_all_sources_fail_returns_empty(self):
        resp_get = MagicMock()
        resp_get.raise_for_status.side_effect = Exception("nope")
        resp_post = MagicMock()
        resp_post.raise_for_status.side_effect = Exception("also nope")
        session = MagicMock()
        session.get.return_value = resp_get
        session.post.return_value = resp_post
        events, source = fetch_events(firecrawl_key="fc-key", session=session)
        assert events == []
        assert source == "none"


# --------------------------------------------------------------------------- #
#  Cache roundtrip                                                            #
# --------------------------------------------------------------------------- #

class TestCacheIO:

    def test_roundtrip_preserves_event_fields(self, tmp_path):
        path = tmp_path / "cal.json"
        events = [_mk_event(2.0, title="FOMC Rate Decision"),
                  _mk_event(6.0, title="Core CPI m/m", currency="USD")]
        snap = CalendarSnapshot(
            events=events,
            fetched_at=datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
            source="faireconomy_json",
        )
        save_snapshot(snap, str(path))
        loaded = load_snapshot(str(path))
        assert loaded is not None
        assert len(loaded.events) == 2
        assert loaded.events[0].title == "FOMC Rate Decision"
        assert loaded.events[0].timestamp_utc.tzinfo is not None
        assert loaded.fetched_at == snap.fetched_at
        assert loaded.source == "faireconomy_json"

    def test_missing_file_returns_none(self, tmp_path):
        assert load_snapshot(str(tmp_path / "absent.json")) is None

    def test_corrupt_file_returns_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_snapshot(str(p)) is None

    def test_save_is_atomic(self, tmp_path):
        """save_snapshot writes to .tmp then renames — intermediate file
        must not be left behind on success."""
        path = tmp_path / "cal.json"
        snap = CalendarSnapshot(
            events=[_mk_event(1.0)],
            fetched_at=datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
            source="test",
        )
        save_snapshot(snap, str(path))
        assert path.exists()
        assert not (tmp_path / "cal.json.tmp").exists()


class TestIsStale:

    def test_fresh_is_not_stale(self):
        snap = CalendarSnapshot(
            events=[], fetched_at=datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
            source="test",
        )
        assert not snap.is_stale(datetime(2026, 4, 23, 22, 0, tzinfo=UTC), refresh_hours=24)

    def test_past_refresh_window_is_stale(self):
        snap = CalendarSnapshot(
            events=[], fetched_at=datetime(2026, 4, 22, 10, 0, tzinfo=UTC),
            source="test",
        )
        assert snap.is_stale(datetime(2026, 4, 23, 11, 0, tzinfo=UTC), refresh_hours=24)


# --------------------------------------------------------------------------- #
#  load_or_refresh behaviour                                                  #
# --------------------------------------------------------------------------- #

class TestLoadOrRefresh:

    def test_fresh_cache_skips_fetch(self, tmp_path):
        path = tmp_path / "cal.json"
        base = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
        cached = CalendarSnapshot(
            events=[_mk_event(4.0, base=base)],
            fetched_at=base,
            source="faireconomy_json",
        )
        save_snapshot(cached, str(path))
        with patch("src.strategy.econ_calendar.fetch_events") as m:
            snap = load_or_refresh(
                cache_path=str(path),
                refresh_hours=24,
                now=base + timedelta(hours=3),
            )
            m.assert_not_called()
        assert len(snap.events) == 1
        assert snap.source == "faireconomy_json"

    def test_stale_cache_triggers_fetch(self, tmp_path):
        path = tmp_path / "cal.json"
        base = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
        save_snapshot(
            CalendarSnapshot(events=[], fetched_at=base, source="faireconomy_json"),
            str(path),
        )
        new_events = [_mk_event(5.0, base=base + timedelta(hours=25))]
        with patch("src.strategy.econ_calendar.fetch_events",
                   return_value=(new_events, "faireconomy_json")) as m:
            snap = load_or_refresh(
                cache_path=str(path),
                refresh_hours=24,
                now=base + timedelta(hours=25),
            )
            m.assert_called_once()
        assert len(snap.events) == 1

    def test_fetch_failure_returns_stale_cache(self, tmp_path):
        """Critical ops invariant: last-known-good > silent blindness."""
        path = tmp_path / "cal.json"
        base = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
        cached = CalendarSnapshot(
            events=[_mk_event(1.0, title="FOMC")],
            fetched_at=base, source="faireconomy_json",
        )
        save_snapshot(cached, str(path))
        with patch("src.strategy.econ_calendar.fetch_events",
                   return_value=([], "none")):
            snap = load_or_refresh(
                cache_path=str(path),
                refresh_hours=24,
                now=base + timedelta(hours=48),  # forces refresh
            )
        assert len(snap.events) == 1
        assert snap.source == "faireconomy_json"  # stale, but still usable
        assert snap.fetched_at == base            # unchanged

    def test_no_cache_no_fetch_returns_empty_snapshot(self, tmp_path):
        path = tmp_path / "cal.json"
        now = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
        with patch("src.strategy.econ_calendar.fetch_events",
                   return_value=([], "none")):
            snap = load_or_refresh(cache_path=str(path), now=now)
        assert snap.events == []
        assert snap.source == "none"


# --------------------------------------------------------------------------- #
#  Blackout window                                                            #
# --------------------------------------------------------------------------- #

class TestIsInBlackout:

    def _now(self):
        return datetime(2026, 4, 23, 12, 0, tzinfo=UTC)

    def test_event_within_window_triggers_blackout(self):
        events = [_mk_event(0.4, base=self._now())]   # 24 min ahead
        hit, ev = is_in_blackout(self._now(), events, blackout_min=30)
        assert hit is True
        assert ev is not None and ev.title == "FOMC Rate Decision"

    def test_event_outside_window_does_not_trigger(self):
        events = [_mk_event(2.0, base=self._now())]   # 2h ahead
        hit, _ = is_in_blackout(self._now(), events, blackout_min=30)
        assert hit is False

    def test_past_event_does_not_trigger(self):
        events = [_mk_event(-0.2, base=self._now())]  # 12 min ago
        hit, _ = is_in_blackout(self._now(), events, blackout_min=30)
        assert hit is False

    def test_wrong_currency_filtered_out(self):
        events = [_mk_event(0.2, currency="EUR", base=self._now())]
        hit, _ = is_in_blackout(
            self._now(), events, blackout_min=30, currencies=["USD"],
        )
        assert hit is False

    def test_medium_impact_filtered_out_by_default(self):
        events = [_mk_event(0.2, impact="Medium", base=self._now())]
        hit, _ = is_in_blackout(self._now(), events, blackout_min=30)
        assert hit is False

    def test_non_whitelisted_title_filtered(self):
        """Even HIGH impact USD event is skipped if title is off-whitelist.
        This is what keeps random bank holiday markers from blocking trades."""
        events = [_mk_event(0.2, title="Flash Manufacturing PMI",
                            base=self._now())]
        hit, _ = is_in_blackout(self._now(), events, blackout_min=30)
        assert hit is False

    def test_picks_soonest_when_multiple(self):
        now = self._now()
        events = [
            _mk_event(0.4, title="FOMC Rate Decision", base=now),
            _mk_event(0.2, title="Core CPI m/m", base=now),
        ]
        _, ev = is_in_blackout(now, events, blackout_min=60)
        assert ev is not None
        assert ev.title == "Core CPI m/m"

    def test_zero_blackout_disables(self):
        events = [_mk_event(0.1, base=self._now())]
        hit, _ = is_in_blackout(self._now(), events, blackout_min=0)
        assert hit is False


# --------------------------------------------------------------------------- #
#  Size modifier window                                                        #
# --------------------------------------------------------------------------- #

class TestGetSizeModifier:

    def _now(self):
        return datetime(2026, 4, 23, 12, 0, tzinfo=UTC)

    def test_within_window_returns_mult(self):
        events = [_mk_event(1.5, base=self._now())]   # 1.5h ahead
        mult, ev = get_size_modifier(
            self._now(), events, window_h=2.0, size_mult=0.5,
        )
        assert mult == 0.5
        assert ev is not None

    def test_just_past_event_still_in_window(self):
        """2h window is symmetric: 1h after event still applies size mod."""
        events = [_mk_event(-1.0, base=self._now())]
        mult, _ = get_size_modifier(
            self._now(), events, window_h=2.0, size_mult=0.5,
        )
        assert mult == 0.5

    def test_outside_window_returns_one(self):
        events = [_mk_event(5.0, base=self._now())]
        mult, ev = get_size_modifier(
            self._now(), events, window_h=2.0, size_mult=0.5,
        )
        assert mult == 1.0
        assert ev is None

    def test_zero_window_disables(self):
        events = [_mk_event(0.5, base=self._now())]
        mult, ev = get_size_modifier(
            self._now(), events, window_h=0.0, size_mult=0.5,
        )
        assert mult == 1.0
        assert ev is None

    def test_empty_events_returns_one(self):
        mult, ev = get_size_modifier(
            self._now(), [], window_h=2.0, size_mult=0.5,
        )
        assert mult == 1.0 and ev is None


# --------------------------------------------------------------------------- #
#  next_event picker                                                          #
# --------------------------------------------------------------------------- #

class TestNextEvent:

    def _now(self):
        return datetime(2026, 4, 23, 12, 0, tzinfo=UTC)

    def test_picks_soonest_future(self):
        now = self._now()
        events = [
            _mk_event(5.0, title="NFP", base=now),
            _mk_event(2.0, title="CPI", base=now),
            _mk_event(-1.0, title="PPI", base=now),  # past
        ]
        ev = next_event(now, events)
        assert ev is not None and ev.title == "CPI"

    def test_respects_within_hours(self):
        now = self._now()
        events = [_mk_event(48.0, title="FOMC", base=now)]
        assert next_event(now, events, within_hours=24) is None
        ev = next_event(now, events, within_hours=72)
        assert ev is not None

    def test_empty_if_only_past_events(self):
        now = self._now()
        events = [_mk_event(-2.0, title="FOMC", base=now)]
        assert next_event(now, events) is None

    def test_respects_currency_filter(self):
        now = self._now()
        events = [_mk_event(1.0, title="ECB Rate Decision",
                            currency="EUR", base=now)]
        assert next_event(now, events, currencies=["USD"]) is None
        ev = next_event(now, events, currencies=["EUR"])
        # ECB isn't in whitelist either but "Rate Decision" is
        assert ev is not None


# --------------------------------------------------------------------------- #
#  format_event_for_log — purely cosmetic but tested for stability            #
# --------------------------------------------------------------------------- #

class TestFormat:

    def test_future_event_includes_hours(self):
        now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
        ev = _mk_event(3.5, base=now)
        s = format_event_for_log(ev, now)
        assert "USD" in s
        assert "FOMC" in s
        assert "in 3.5h" in s

    def test_soon_event_uses_minutes(self):
        now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
        ev = _mk_event(0.2, base=now)  # 12 min
        s = format_event_for_log(ev, now)
        assert "in 12m" in s

    def test_past_event_reads_as_ago(self):
        now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
        ev = _mk_event(-1.5, base=now)
        s = format_event_for_log(ev, now)
        assert "ago" in s
