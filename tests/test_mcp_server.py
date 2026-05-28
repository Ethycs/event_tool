"""Tests for MCP helper logic that does not require the MCP SDK runtime."""

import pytest

from event_harvester.config import AppConfig
from event_harvester.mcp_server import (
    _load_latest_events,
    _mark_cached_event,
    _parse_source_filter,
    _save_latest_events,
    _source_kwargs,
    _summarize_config,
)


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
    monkeypatch.setattr("event_harvester.mcp_server.LATEST_EVENTS_FILE", cache_file)
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


def test_summarize_config_has_mcp_client_relevant_fields():
    summary = _summarize_config(AppConfig())

    assert "sources" in summary
    assert "caps" in summary
    assert "configured" in summary
    assert summary["llm"]["model"]
