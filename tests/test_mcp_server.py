"""Tests for MCP helper logic that does not require the MCP SDK runtime."""

from datetime import date, timedelta

import pytest

from event_harvester.config import AppConfig
from event_harvester.servers.mcp_server import (
    _accumulate_events,
    _days_until,
    _filter_by_period,
    _load_latest_events,
    _mark_cached_event,
    _parse_source_filter,
    _save_latest_events,
    _source_kwargs,
    _summarize_config,
)

_FUT = (date.today() + timedelta(days=10)).isoformat()
_FUT2 = (date.today() + timedelta(days=11)).isoformat()
_PAST = (date.today() - timedelta(days=3)).isoformat()


def test_parse_source_filter_accepts_csv_and_arrays():
    assert _parse_source_filter("discord, gmail") == {"discord", "gmail"}
    assert _parse_source_filter(["Telegram", "web"]) == {"telegram", "web"}
    assert _parse_source_filter("") is None


def test_parse_source_filter_rejects_unknown_sources():
    with pytest.raises(ValueError, match="Unknown source"):
        _parse_source_filter("discord,facebook")


def test_source_kwargs_uses_config_defaults():
    cfg = AppConfig()
    cfg.sources.web = False

    kwargs = _source_kwargs(cfg)

    assert kwargs["no_web"] is True
    assert kwargs["no_discord"] is False


def test_source_kwargs_only_overrides_config_defaults():
    cfg = AppConfig()
    cfg.sources.web = False

    kwargs = _source_kwargs(cfg, only="web")

    assert kwargs["no_web"] is False
    assert kwargs["no_discord"] is True


def test_source_kwargs_rejects_only_and_skip_together():
    with pytest.raises(ValueError, match="either 'only' or 'skip'"):
        _source_kwargs(AppConfig(), only="discord", skip="web")


def test_latest_event_cache_round_trip(tmp_path, monkeypatch):
    cache_file = tmp_path / ".mcp_events.json"
    monkeypatch.setattr("event_harvester.servers.mcp_server.LATEST_EVENTS_FILE", cache_file)
    events = [{"title": "AI Meetup", "date": "2026-06-01"}]

    _save_latest_events(
        events,
        stats={"events": 1},
        source_counts={"discord": 1},
        links=[],
    )
    marked = _mark_cached_event(0, "declined")
    loaded = _load_latest_events()

    assert marked["_status"] == "declined"
    assert loaded["events"][0]["title"] == "AI Meetup"
    assert loaded["events"][0]["_status"] == "declined"


# ── accumulation (keep prior future events, union the new run, expire past) ──

def test_accumulate_unions_new_with_prior_future_events():
    new = [{"title": "Fresh Gig", "date": _FUT, "link": "https://x/new"}]
    prior = [{"title": "Standing Night", "date": _FUT2, "link": "https://x/old"}]
    merged = _accumulate_events(new, prior)
    titles = {e["title"] for e in merged}
    assert titles == {"Fresh Gig", "Standing Night"}


def test_accumulate_expires_past_prior_events():
    new = [{"title": "Fresh", "date": _FUT, "link": "https://x/n"}]
    prior = [{"title": "Gone By", "date": _PAST, "link": "https://x/old"}]
    merged = _accumulate_events(new, prior)
    assert [e["title"] for e in merged] == ["Fresh"]   # past prior dropped


def test_accumulate_keeps_undated_prior():
    prior = [{"title": "Someday", "date": None, "link": "https://x/someday"}]
    merged = _accumulate_events([], prior)
    assert [e["title"] for e in merged] == ["Someday"]  # can't expire undated


def test_accumulate_collapses_the_same_event_seen_twice():
    ev = {"title": "Repeat", "date": _FUT, "link": "https://x/same"}
    merged = _accumulate_events([dict(ev)], [dict(ev)])
    assert len(merged) == 1


def test_accumulate_preserves_a_prior_decline_on_a_re_extracted_event():
    prior = [{"title": "No Thanks", "date": _FUT, "link": "https://x/nt",
              "_status": "declined"}]
    # the new run re-extracts the same event as fresh ("new")
    new = [{"title": "No Thanks", "date": _FUT, "link": "https://x/nt"}]
    merged = _accumulate_events(new, prior)
    assert len(merged) == 1
    assert merged[0]["_status"] == "declined"   # decision survives the union


def test_save_latest_events_accumulates_against_the_existing_cache(tmp_path, monkeypatch):
    cache_file = tmp_path / ".mcp_events.json"
    monkeypatch.setattr("event_harvester.servers.mcp_server.LATEST_EVENTS_FILE", cache_file)
    monkeypatch.delenv("EH_ACCUMULATE", raising=False)  # default = on
    _save_latest_events([{"title": "First", "date": _FUT, "link": "https://x/1"}],
                        stats={"events": 1}, source_counts={}, links=[])
    # a second, disjoint run must not wipe the first
    _save_latest_events([{"title": "Second", "date": _FUT2, "link": "https://x/2"}],
                        stats={"events": 1}, source_counts={}, links=[])
    loaded = _load_latest_events()
    assert {e["title"] for e in loaded["events"]} == {"First", "Second"}


def test_accumulation_can_be_disabled(tmp_path, monkeypatch):
    cache_file = tmp_path / ".mcp_events.json"
    monkeypatch.setattr("event_harvester.servers.mcp_server.LATEST_EVENTS_FILE", cache_file)
    monkeypatch.setenv("EH_ACCUMULATE", "0")
    _save_latest_events([{"title": "First", "date": _FUT, "link": "https://x/1"}],
                        stats={"events": 1}, source_counts={}, links=[])
    _save_latest_events([{"title": "Second", "date": _FUT2, "link": "https://x/2"}],
                        stats={"events": 1}, source_counts={}, links=[])
    loaded = _load_latest_events()
    assert {e["title"] for e in loaded["events"]} == {"Second"}   # wholesale replace


# ── list_latest_events period filter (today / this week / this month) ──────

_TODAY = date(2026, 9, 3)


def _iso(delta_days: int) -> str:
    return (_TODAY + timedelta(days=delta_days)).isoformat()


def test_days_until_parses_and_handles_ranges_and_junk():
    assert _days_until(_iso(0), _TODAY) == 0
    assert _days_until(_iso(5), _TODAY) == 5
    assert _days_until(_iso(-2), _TODAY) == -2
    assert _days_until("2026-09-10 to 2026-09-12", _TODAY) == 7   # takes the start
    assert _days_until(None, _TODAY) is None
    assert _days_until("TBD", _TODAY) is None
    assert _days_until("someday", _TODAY) is None


def _events():
    return [
        {"title": "past", "date": _iso(-1)},
        {"title": "today", "date": _iso(0)},
        {"title": "in3", "date": _iso(3)},
        {"title": "in7", "date": _iso(7)},
        {"title": "in10", "date": _iso(10)},
        {"title": "in30", "date": _iso(30)},
        {"title": "in45", "date": _iso(45)},
        {"title": "undated", "date": None},
    ]


def _titles(events):
    return [e["title"] for e in events]


def test_period_today_is_just_today():
    assert _titles(_filter_by_period(_events(), "today", _TODAY)) == ["today"]


def test_period_this_week_is_today_through_seven_days():
    assert _titles(_filter_by_period(_events(), "this_week", _TODAY)) == ["today", "in3", "in7"]


def test_period_this_month_is_today_through_thirty_days():
    assert _titles(_filter_by_period(_events(), "this_month", _TODAY)) == \
        ["today", "in3", "in7", "in10", "in30"]


def test_period_upcoming_is_everything_future():
    assert _titles(_filter_by_period(_events(), "upcoming", _TODAY)) == \
        ["today", "in3", "in7", "in10", "in30", "in45"]


def test_period_always_excludes_past_and_undated():
    for p in ("today", "this_week", "this_month", "upcoming"):
        kept = _titles(_filter_by_period(_events(), p, _TODAY))
        assert "past" not in kept and "undated" not in kept


def test_summarize_config_has_mcp_client_relevant_fields():
    summary = _summarize_config(AppConfig())

    assert "sources" in summary
    assert "caps" in summary
    assert "configured" in summary
    assert summary["llm"]["model"]
