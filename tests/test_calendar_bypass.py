"""Tests for the row-as-event bypass (analysis._calendar_rows_to_events).

A web-calendar message that already states title/date/time/location/url is an
event; it skips the LLM (which cannot dedup and only risks mis-attaching the
link) and is emitted directly. These tests pin the conversion, the drops, the
time parsing, and that structured rows never reach the extractor.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from event_harvester.pipeline import analysis
from event_harvester.pipeline.analysis import (
    _calendar_rows_to_events,
    _row_time_to_24h,
    extract_events_llm,
)

FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=5)).isoformat()


def _row(title, d, *, time="8:00pm - 1:00am", loc="Somewhere",
         url="https://forbiddentickets.com/events/stopgap/abc123", mid="m1"):
    body = f"{title}\ndate: {d}\ntime: {time}\nlocation: {loc}\n"
    return {"platform": "web", "id": mid, "author": "erobay.com",
            "timestamp": f"{d}T00:00:00+00:00", "content": body,
            "channel": url, "url": url}


# ── time parsing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    ("8:00pm - 1:00am", "20:00"),
    ("7pm", "19:00"),
    ("7:30pm", "19:30"),
    ("19:00", "19:00"),
    ("12am", "00:00"),
    ("12pm", "12:00"),
    ("9:00 AM", "09:00"),
    ("", None),
    ("all day", None),
])
def test_row_time_to_24h(line, expected):
    assert _row_time_to_24h(line) == expected


# ── conversion ────────────────────────────────────────────────────────────

def test_structured_row_becomes_event():
    url = "https://forbiddentickets.com/events/stopgap/bf9c203bc2"
    bypass, remaining = _calendar_rows_to_events([_row("House Party", FUTURE, url=url)])
    assert remaining == []
    assert len(bypass) == 1
    e = bypass[0]
    assert e["title"] == "House Party"
    assert e["date"] == FUTURE
    assert e["time"] == "20:00"
    assert e["location"] == "Somewhere"
    assert e["link"] == url          # its OWN url, deterministically
    assert e["_source_ids"] == ["m1"]
    assert e["_platforms"] == ["web"]


def test_past_row_is_dropped_not_passed_on():
    bypass, remaining = _calendar_rows_to_events([_row("Old Thing", PAST)])
    assert bypass == [] and remaining == []   # dropped entirely, not sent to LLM


def test_noise_title_is_dropped():
    bypass, remaining = _calendar_rows_to_events([_row("RESERVED", FUTURE)])
    assert bypass == [] and remaining == []


def test_web_row_without_url_goes_to_llm():
    m = _row("Recurring Thing", FUTURE, url="")
    m["url"] = ""
    m["channel"] = ""
    bypass, remaining = _calendar_rows_to_events([m])
    assert bypass == []
    assert remaining == [m]           # no authoritative URL -> let the LLM handle it


def test_url_in_channel_and_title_after_day_header():
    # ma.to shape: the URL is only in `channel` (not `url`), and a `date: 3 Sep`
    # day header precedes the real title. Both must be handled.
    m = {
        "platform": "web", "id": "mt1", "author": "ma.to",
        "timestamp": f"{FUTURE}T00:00:00+00:00",
        "content": (f"date: 3 Sep\nOdoo Experience Americas\ndate: {FUTURE}\n"
                    "time: 09:00 - 18:00\nlocation: Pier 27, San Francisco\nblurb"),
        "channel": "https://ma.to/event/odoo-experience-americas",
        "url": None,
    }
    bypass, remaining = _calendar_rows_to_events([m])
    assert remaining == [] and len(bypass) == 1
    e = bypass[0]
    assert e["title"] == "Odoo Experience Americas"     # not 'date: 3 Sep'
    assert e["date"] == FUTURE                           # the ISO line, not '3 Sep'
    assert e["time"] == "09:00"
    assert e["link"] == "https://ma.to/event/odoo-experience-americas"  # from channel


def test_non_web_message_untouched():
    chat = {"platform": "discord", "id": "d1", "author": "alice",
            "content": "event tonight\ndate: " + FUTURE, "channel": "123"}
    bypass, remaining = _calendar_rows_to_events([chat])
    assert bypass == [] and remaining == [chat]


def test_row_without_date_line_goes_to_llm():
    m = {"platform": "web", "id": "w1", "author": "site",
         "content": "Some Title\nno date here", "url": "https://x.com/e/1",
         "channel": "https://x.com/e/1"}
    bypass, remaining = _calendar_rows_to_events([m])
    assert bypass == [] and remaining == [m]


# ── wiring: structured rows never reach the LLM ───────────────────────────

def test_bypass_rows_skip_the_extractor(monkeypatch):
    rows = [_row("Play Party", FUTURE, mid="a"),
            _row("Rope Jam", FUTURE, mid="b",
                 url="https://erobay.com/calendar/Calcium40.pl?ID=9")]
    chat = {"platform": "discord", "id": "c", "author": "bob",
            "content": "casual chatter", "channel": "123", "timestamp": f"{FUTURE}T00:00:00+00:00"}

    seen = {}
    monkeypatch.setattr(analysis, "filter_for_extraction",
                        lambda messages, caps, label=None: (
                            seen.__setitem__("filtered", list(messages))
                            or SimpleNamespace(messages=list(messages), rejects={})))

    sent = []
    def fake_cloud(messages, days_back, cfg, batch_size=5):
        sent.append([m["id"] for m in messages])
        return "sum", [], {"llm_past": [], "llm_no_events": list(messages), "llm_errors": []}
    monkeypatch.setattr(analysis, "_extract_events_cloud", fake_cloud)
    monkeypatch.setenv("EH_EXTRACTION_CACHE", "0")

    from event_harvester.config import LLMConfig
    cfg = LLMConfig(model="qwen3-8b", api_base="http://local", backend="litellm")
    res = extract_events_llm(rows + [chat], 7, cfg)

    # Only the chat message reached filtering / the LLM; the two rows bypassed.
    assert [m["id"] for m in seen["filtered"]] == ["c"]
    assert sent == [["c"]]
    titles = {e["title"] for e in res.events}
    assert {"Play Party", "Rope Jam"} <= titles
