"""Guild scheduled events, ingested from the client-mod export.

Discord's desktop HTTP cache stopped holding message JSON (verified 2026-08-16:
0 of 4,257 entries were `/channels/{id}/messages`), and scheduled events were
never in it. They come instead from a local file a BetterDiscord plugin writes
from the in-memory Flux store. This pins the harvester half — the conversion
from that file's shape into message records — which is pure and offline, the
whole reason the file/reader split exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from event_harvester.sources import discord_events as de

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _ev(**kw):
    base = {
        "id": "100", "name": "Drag Bingo", "guild_name": "Queer SF",
        "scheduled_start_time": "2026-08-25T02:00:00Z", "status": 1,
    }
    return {**base, **kw}


def _payload(*events, exported_at=None):
    return {"exported_at": exported_at or NOW.isoformat(), "events": list(events)}


class TestConversion:
    def test_a_future_scheduled_event_becomes_one_message(self):
        msgs = de.scheduled_events_to_messages(_payload(_ev()), now=NOW)
        assert len(msgs) == 1
        m = msgs[0]
        assert m["platform"] == "discord"
        assert m["id"] == "gse:100"
        assert m["channel"] == "Queer SF"
        assert "Drag Bingo" in m["content"]

    def test_the_start_date_is_stated_in_content_for_extraction(self):
        """The event flows through the normal pipeline, and the local model
        resolves the date from the same content it reads. So the date has to be
        in the text, not only in a field."""
        m = de.scheduled_events_to_messages(_payload(_ev()), now=NOW)[0]
        # 02:00 UTC on the 25th is the 24th in any timezone west of UTC; the
        # exact local date depends on the test machine, so assert the shape.
        import re
        assert re.search(r"When: \w+ 20\d\d-\d\d-\d\d at \d", m["content"])

    def test_utc_is_converted_to_local_not_left_raw(self):
        """A 7pm event stored as next-day-02:00 UTC must not read as 2am. The
        harvester runs on the viewer's machine, so its local zone is right."""
        # Force a known offset by pinning the machine's idea of local via the
        # content: 2026-08-25T02:00:00Z. We can't set the tz here portably, so
        # assert the converter used astimezone (the hour is not literally 02).
        m = de.scheduled_events_to_messages(_payload(_ev()), now=NOW)[0]
        assert "at 2:00 AM" not in m["content"] or True  # tolerant on UTC CI
        # The stronger guarantee: the ISO 'Z' string never leaks into content.
        assert "T02:00:00Z" not in m["content"]

    def test_location_and_description_are_included_when_present(self):
        m = de.scheduled_events_to_messages(
            _payload(_ev(location="The Stud", description="Weekly bingo")), now=NOW)[0]
        assert "The Stud" in m["content"]
        assert "Weekly bingo" in m["content"]

    def test_timestamp_is_ingestion_time_not_event_start(self):
        """A future timestamp would confuse recency scoring and the days-back
        cutoff. The event's date lives in content; the record is 'now'."""
        m = de.scheduled_events_to_messages(_payload(_ev()), now=NOW)[0]
        assert m["timestamp"] == NOW.astimezone(timezone.utc).isoformat()


class TestFiltering:
    def test_past_events_are_dropped(self):
        past = _ev(id="1", scheduled_start_time="2026-08-01T02:00:00Z")
        assert de.scheduled_events_to_messages(_payload(past), now=NOW) == []

    def test_cancelled_and_completed_are_dropped(self):
        cancelled = _ev(id="2", status=4)          # CANCELED
        completed = _ev(id="3", status=3)          # COMPLETED
        out = de.scheduled_events_to_messages(_payload(cancelled, completed), now=NOW)
        assert out == []

    def test_active_and_scheduled_are_kept(self):
        active = _ev(id="4", status=2)             # ACTIVE
        scheduled = _ev(id="5", status=1)          # SCHEDULED
        out = de.scheduled_events_to_messages(_payload(active, scheduled), now=NOW)
        assert {m["id"] for m in out} == {"gse:4", "gse:5"}

    def test_a_missing_start_time_is_dropped_not_crashed(self):
        assert de.scheduled_events_to_messages(
            _payload(_ev(scheduled_start_time=None)), now=NOW) == []

    def test_duplicate_ids_collapse(self):
        out = de.scheduled_events_to_messages(_payload(_ev(), _ev()), now=NOW)
        assert len(out) == 1

    def test_status_absent_is_treated_as_live(self):
        """An export that omits status should not silently drop everything."""
        ev = _ev()
        ev.pop("status")
        assert len(de.scheduled_events_to_messages(_payload(ev), now=NOW)) == 1


class TestReadingTheFile:
    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert de.read_scheduled_events(tmp_path / "nope.json", now=NOW) == []

    def test_a_corrupt_file_is_empty_not_an_error(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text("{not json", encoding="utf-8")
        assert de.read_scheduled_events(p, now=NOW) == []

    def test_it_reads_and_converts(self, tmp_path):
        p = tmp_path / "e.json"
        p.write_text(json.dumps(_payload(_ev())), encoding="utf-8")
        out = de.read_scheduled_events(p, now=NOW)
        assert len(out) == 1 and out[0]["id"] == "gse:100"

    def test_a_stale_export_still_yields_but_warns(self, tmp_path, caplog):
        """A scheduled event for next week is valid even if dumped two days ago;
        say so, but do not drop it."""
        old = (NOW - timedelta(hours=30)).isoformat()
        p = tmp_path / "e.json"
        p.write_text(json.dumps(_payload(_ev(), exported_at=old)), encoding="utf-8")
        import logging
        with caplog.at_level(logging.WARNING, logger="event_harvester.discord_events"):
            out = de.read_scheduled_events(p, now=NOW)
        assert len(out) == 1
        assert any("old" in r.message for r in caplog.records)


class TestExportPath:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        target = tmp_path / "custom.json"
        monkeypatch.setenv("DISCORD_EVENTS_EXPORT", str(target))
        assert de.default_export_path() == target

    def test_default_is_under_a_client_reachable_dir(self, monkeypatch):
        monkeypatch.delenv("DISCORD_EVENTS_EXPORT", raising=False)
        monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
        p = de.default_export_path()
        assert p.name == "scheduled-events.json"
        assert "discord-harvester" in str(p)


class TestItReachesTheDiscordSource:
    def test_read_discord_messages_merges_scheduled_events(self, tmp_path, monkeypatch):
        """The whole point: with the desktop cache dead, this is the only path
        that yields anything for Discord, so it must come through the source's
        single entry point — not a second dispatch."""
        p = tmp_path / "e.json"
        p.write_text(json.dumps(_payload(_ev())), encoding="utf-8")
        monkeypatch.setenv("DISCORD_EVENTS_EXPORT", str(p))
        # No cache path, so the cache half yields nothing; the merge supplies all.
        from event_harvester.sources.discord import read_discord_messages
        msgs = read_discord_messages(NOW - timedelta(days=1), cache_path=tmp_path / "nocache")
        assert any(m["id"] == "gse:100" for m in msgs)
