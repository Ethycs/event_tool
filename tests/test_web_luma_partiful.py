"""Tests for the `luma` and `partiful` modes.

Both exist because a browser-driven scrape of a dense listing yields one event
however many it lists. Measured 2026-08-10, the pre-existing `luma` source
(`mode: "calendar"` on `lu.ma/discover`) returned **1 message and 1 event**
from a page whose own directory read "San Francisco 80 Events". Reading the
structured payload these platforms already publish returned **60**.

The tests concentrate on the two ways this class of source goes quietly wrong:
picking the wrong *city*, and inventing a venue that was never given.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from event_harvester.sources.web_modes import luma, partiful
from event_harvester.sources.web_utils import parse_next_data

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _next_html(payload: dict) -> str:
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}</script></body></html>"
    )


class TestParseNextData:
    def test_it_reads_the_inlined_props(self):
        assert parse_next_data(_next_html({"a": 1})) == {"a": 1}

    def test_a_page_without_the_tag_is_none(self):
        assert parse_next_data("<html>nothing</html>") is None

    def test_malformed_json_is_none_not_an_exception(self):
        assert parse_next_data(
            '<script id="__NEXT_DATA__">{"a":</script>',
        ) is None

    def test_empty_input_is_none(self):
        assert parse_next_data("") is None


# ── Luma ─────────────────────────────────────────────────────────────


def _luma_event(name="Party", start="2026-08-12T01:00:00.000Z", **over):
    ev = {
        "api_id": "evt-1", "name": name, "start_at": start,
        "timezone": "America/Los_Angeles",
        "url": "slug1", "geo_address_info": {
            "address": "550 Laguna", "city_state": "San Francisco, CA",
        },
    }
    ev.update(over)
    return {"event": ev}


class TestLumaMessages:
    def test_the_shape_carries_the_facts(self):
        msg = luma._message(_luma_event(), "luma")
        assert msg["platform"] == "web"
        assert msg["id"] == "luma:evt-1"
        assert msg["channel"] == "luma"
        # 01:00Z in Pacific is 18:00 the previous day — the date/time are the
        # event's local wall-clock, not the raw UTC slice.
        assert "Date: 2026-08-11" in msg["content"]
        assert "Time: 18:00" in msg["content"]
        assert "Venue: 550 Laguna, San Francisco, CA" in msg["content"]
        assert "https://luma.com/slug1" in msg["content"]

    def test_the_time_is_converted_from_utc_to_the_events_zone(self):
        """The 'shows at 3am, a day early' bug: an 8pm-Pacific event is stored
        03:00Z the next day, and must render as 8pm the correct day."""
        msg = luma._message(
            _luma_event(start="2026-09-11T03:00:00.000Z"), "luma")
        assert "Date: 2026-09-10" in msg["content"]
        assert "Time: 20:00" in msg["content"]
        assert "UTC" not in msg["content"]     # no longer labelled/left in UTC

    def test_a_missing_zone_falls_back_to_pacific(self):
        ev = _luma_event(start="2026-09-11T03:00:00.000Z")
        del ev["event"]["timezone"]
        msg = luma._message(ev, "luma")
        assert "Date: 2026-09-10" in msg["content"] and "Time: 20:00" in msg["content"]

    def test_the_title_leads(self):
        """build_prompt keeps 400 chars; the name must not be buried."""
        assert luma._message(_luma_event(name="Sewing"), "luma")["content"] \
            .startswith("Sewing")

    def test_an_event_with_no_name_is_skipped(self):
        assert luma._message(_luma_event(name=""), "luma") is None

    def test_an_event_with_no_start_is_skipped(self):
        assert luma._message(_luma_event(start=""), "luma") is None

    def test_ids_are_stable_across_runs(self):
        a = luma._message(_luma_event(), "luma")["id"]
        b = luma._message(_luma_event(), "luma")["id"]
        assert a == b


class TestLumaVenue:
    """An obfuscated address is a real state, not a missing field."""

    def test_a_full_address_is_used(self):
        assert luma._venue({"geo_address_info": {
            "address": "1 Market St", "city_state": "San Francisco, CA"}}
        ) == "1 Market St, San Francisco, CA"

    def test_an_obfuscated_event_gives_the_city_only(self):
        got = luma._venue({"geo_address_info": {
            "mode": "obfuscated", "city_state": "San Francisco, CA"}})
        assert got == "San Francisco, CA"

    def test_a_repeated_venue_name_is_not_doubled(self):
        assert luma._venue({"geo_address_info": {
            "address": "DG717", "city_state": "DG717"}}) == "DG717"

    def test_a_virtual_event_says_online(self):
        assert luma._venue({"location_type": "virtual"}) == "Online"

    def test_no_location_at_all_is_empty(self):
        assert luma._venue({}) == ""


class TestLumaPagination:
    @staticmethod
    def _patch(monkeypatch, pages):
        calls = []

        class R:
            def __init__(self, body):
                self._b = body

            def raise_for_status(self):
                return None

            def json(self):
                return self._b

        def fake_get(url, params=None, **kw):
            calls.append(params or {})
            return R(pages[len(calls) - 1])

        monkeypatch.setattr(luma.requests, "get", fake_get)
        return calls

    def test_it_follows_the_cursor(self, monkeypatch):
        calls = self._patch(monkeypatch, [
            {"entries": [_luma_event()], "has_more": True, "next_cursor": "C1"},
            {"entries": [_luma_event()], "has_more": False, "next_cursor": None},
        ])
        got = luma.fetch_entries("place-1", max_events=50)
        assert len(got) == 2
        assert calls[1]["pagination_cursor"] == "C1"

    def test_it_stops_when_has_more_is_false(self, monkeypatch):
        calls = self._patch(monkeypatch, [
            {"entries": [_luma_event()], "has_more": False, "next_cursor": "C1"},
        ])
        luma.fetch_entries("place-1", max_events=50)
        assert len(calls) == 1

    def test_it_stops_at_max_events(self, monkeypatch):
        self._patch(monkeypatch, [
            {"entries": [_luma_event()] * 40, "has_more": True, "next_cursor": "C"},
            {"entries": [_luma_event()] * 40, "has_more": True, "next_cursor": "C"},
        ])
        assert len(luma.fetch_entries("place-1", max_events=50)) == 50

    def test_an_api_failure_returns_what_it_had(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(luma.requests, "get", boom)
        assert luma.fetch_entries("place-1", max_events=50) == []


class TestLumaPlaceId:
    def test_it_is_read_off_the_city_page(self, monkeypatch):
        html = _next_html({"props": {"pageProps": {"initialData": {"data": {
            "place": {"api_id": "discplace-XYZ", "slug": "sf"}}}}}})

        class R:
            text = html

            def raise_for_status(self):
                return None

        monkeypatch.setattr(luma.requests, "get", lambda *a, **k: R())
        assert luma.resolve_place_id("https://luma.com/sf") == "discplace-XYZ"

    def test_a_page_with_no_place_block_is_none(self, monkeypatch):
        class R:
            text = _next_html({"props": {"pageProps": {"initialData": {"data": {}}}}})

            def raise_for_status(self):
                return None

        monkeypatch.setattr(luma.requests, "get", lambda *a, **k: R())
        assert luma.resolve_place_id("https://luma.com/discover") is None

    def test_a_configured_id_skips_the_page_fetch(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not fetch when place_api_id is configured")

        monkeypatch.setattr(luma, "resolve_place_id", boom)
        monkeypatch.setattr(luma, "fetch_entries", lambda *a, **k: [])
        luma._do_luma_inner(None, {"url": "x", "name": "luma",
                                   "place_api_id": "discplace-CONFIGURED"})


class TestLumaWindow:
    def test_events_beyond_the_horizon_are_dropped(self, monkeypatch):
        monkeypatch.setattr(luma, "fetch_entries", lambda *a, **k: [
            _luma_event(name="soon", start="2026-08-12T01:00:00.000Z"),
            _luma_event(name="far", start="2027-08-12T01:00:00.000Z"),
        ])
        out = luma._do_luma_inner(
            None, {"url": "x", "name": "luma", "place_api_id": "p",
                   "forward_days": 21}, now=NOW,
        )
        assert [m["content"].split("\n")[0] for m in out] == ["soon"]

    def test_no_place_id_yields_nothing_rather_than_raising(self, monkeypatch):
        monkeypatch.setattr(luma, "resolve_place_id", lambda *a, **k: None)
        assert luma._do_luma_inner(None, {"url": "x", "name": "luma"}) == []


# ── Partiful ─────────────────────────────────────────────────────────


def _partiful_page(**cities):
    return _next_html({"props": {"pageProps": {"trendingSections": {
        city: {"items": [{"event": e} for e in events]}
        for city, events in cities.items()
    }}}})


def _partiful_event(title="Party", start="2026-08-16T21:00:00.000Z", **over):
    ev = {
        "id": "abc123", "title": title, "startDate": start,
        "locationInfo": {"mapsInfo": {
            "name": "Robin Williams Meadow",
            "addressLines": ["Kezar Drive", "San Francisco, CA 94117"],
        }},
    }
    ev.update(over)
    return ev


class TestPartifulCity:
    """`/explore` lists NYC first. Taking the first section harvests New York."""

    def test_the_configured_city_is_the_one_used(self, monkeypatch):
        page = _partiful_page(
            NYC=[_partiful_event(title="Manhattan Walk")],
            LA=[_partiful_event(title="LA Popup")],
            SF=[_partiful_event(title="SF Fiber Arts")],
        )
        _patch_fetch(monkeypatch, page)
        out = _run(city="SF")
        assert [m["content"].split("\n")[0] for m in out] == ["SF Fiber Arts"]

    def test_a_missing_city_yields_nothing_rather_than_another_city(
        self, monkeypatch, caplog,
    ):
        """Falling back would silently fill an SF calendar with New York."""
        _patch_fetch(monkeypatch, _partiful_page(NYC=[_partiful_event()]))
        assert _run(city="SF") == []
        assert "NYC" in caplog.text

    def test_the_sections_are_discovered_not_hardcoded(self):
        got = partiful.city_sections(
            _partiful_page(NYC=[_partiful_event()], SF=[_partiful_event()]),
        )
        assert sorted(got) == ["NYC", "SF"]

    def test_a_page_without_sections_is_empty(self):
        assert partiful.city_sections(_next_html({"props": {"pageProps": {}}})) == {}


class TestPartifulMessages:
    def test_the_shape_carries_the_facts(self, monkeypatch):
        _patch_fetch(monkeypatch, _partiful_page(SF=[_partiful_event()]))
        msg = _run()[0]
        assert msg["platform"] == "web"
        assert msg["id"] == "partiful:abc123"
        assert msg["channel"] == "partiful-sf"
        # 21:00Z is 14:00 PDT the same day — local wall-clock, not the UTC slice.
        assert "Date: 2026-08-16" in msg["content"]
        assert "Time: 14:00" in msg["content"]
        assert "UTC" not in msg["content"]
        assert "Robin Williams Meadow, Kezar Drive" in msg["content"]
        assert "https://partiful.com/e/abc123" in msg["content"]

    def test_the_time_is_converted_to_the_configured_citys_zone(self, monkeypatch):
        """An 8pm-Pacific SF event is stored 03:00Z the next day; the per-event
        `timezone` field is unreliable (reads NY for SF), so the configured city
        is the zone that gets it back to 8pm on the right day."""
        _patch_fetch(monkeypatch, _partiful_page(SF=[_partiful_event(
            start="2026-08-20T03:00:00.000Z", timezone="America/New_York")]))
        msg = _run(city="SF")[0]
        assert "Date: 2026-08-19" in msg["content"]     # 03:00Z -> 20:00 PDT prev day
        assert "Time: 20:00" in msg["content"]

    def test_the_description_is_included_and_bounded(self, monkeypatch):
        _patch_fetch(monkeypatch, _partiful_page(
            SF=[_partiful_event(description="x " * 5000)]))
        assert len(_run()[0]["content"]) < 1200

    def test_a_private_event_is_skipped(self, monkeypatch):
        _patch_fetch(monkeypatch, _partiful_page(
            SF=[_partiful_event(isPublic=False)]))
        assert _run() == []

    def test_an_unstructured_location_still_yields_a_venue(self, monkeypatch):
        _patch_fetch(monkeypatch, _partiful_page(SF=[_partiful_event(
            locationInfo={"address": "Somewhere in the Mission"})]))
        assert "Somewhere in the Mission" in _run()[0]["content"]

    def test_a_fetch_failure_is_empty_not_an_exception(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(partiful.requests, "get", boom)
        assert _run() == []


# ── helpers ──────────────────────────────────────────────────────────


def _patch_fetch(monkeypatch, html: str):
    class R:
        text = html

        def raise_for_status(self):
            return None

    monkeypatch.setattr(partiful.requests, "get", lambda *a, **k: R())


def _run(city: str = "SF"):
    return partiful._do_partiful_inner(
        None,
        {"url": "https://partiful.com/explore", "name": "partiful",
         "city": city, "forward_days": 30},
        now=NOW,
    )


@pytest.fixture(autouse=True)
def _log_level(caplog):
    import logging
    caplog.set_level(logging.ERROR, logger="event_harvester.web_fetch")
