"""An LLM outage must not read as an empty night, nor destroy the last good run.

On 2026-08-12 the OpenAI account ran out of credit. Every extraction batch
returned `429 insufficient_quota`; every one of those batches had its messages
filed under `llm_no_events` — "the model read these and found nothing" — so the
run reported `ok: true, events: 0` and wrote that over a cache holding **251
events, which were unrecoverable**. Nothing in the summary said the model had
never been reached.

Two independent faults, pinned separately here because either alone is enough
to lose a day's harvest:

* `TestErrorsAreNotEmptyAnswers` — the reject bucket. Axiom 3 says every drop is
  attributable, and a drop attributed to the *wrong stage* is worse than an
  unattributed one, because it reads as a finding.
* `TestTheCacheSurvives` — the write. A total outage leaves the previous cache
  alone. With accumulation (the default), a genuine empty night and a partial
  outage both keep the standing future events rather than replacing them.
"""

from __future__ import annotations

import json

import pytest

from event_harvester.pipeline.analysis import REJECT_BUCKETS


class TestErrorsAreNotEmptyAnswers:
    def test_there_is_a_bucket_for_never_answered(self):
        assert "llm_errors" in REJECT_BUCKETS

    def test_the_local_path_separates_them_too(self, monkeypatch):
        """Both backends had the same conflation, so both are pinned."""
        from event_harvester.pipeline import analysis

        def boom(*a, **k):
            raise RuntimeError("429 insufficient_quota")

        monkeypatch.setattr(analysis, "chat_completion", boom)
        messages = [{"content": "a party on friday", "channel": "x", "id": "1"}]
        _summary, events, rejects = analysis._extract_events_local(
            messages, days_back=7, cfg=object(), batch_size=10,
        )
        assert events == []
        assert len(rejects["llm_errors"]) == 1
        assert not rejects["llm_no_events"], \
            "a 429 was filed as 'the model found no events'"

    def test_the_cloud_path_separates_them(self, monkeypatch):
        from event_harvester.pipeline import analysis

        def boom(*a, **k):
            raise RuntimeError("429 insufficient_quota")

        monkeypatch.setattr(analysis, "chat_completion", boom)
        messages = [{"content": "a party on friday", "channel": "x", "id": str(i)}
                    for i in range(3)]
        _s, events, rejects = analysis._extract_events_cloud(
            messages, cfg=object(), days_back=7, batch_size=2,
        )
        assert events == []
        assert len(rejects["llm_errors"]) == 3
        assert rejects["llm_no_events"] == []


class TestTheCacheSurvives:
    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        from event_harvester.servers import mcp_server

        path = tmp_path / ".mcp_events.json"
        monkeypatch.setattr(mcp_server, "LATEST_EVENTS_FILE", path)
        path.write_text(json.dumps({
            "events": [{"title": "Queer Night"}, {"title": "AI Meetup"}],
            "stats": {}, "source_counts": {}, "links": [],
        }), encoding="utf-8")
        return path

    def _save(self, events, errors, **kw):
        from event_harvester.servers.mcp_server import _save_latest_events

        _save_latest_events(events, stats={"rejects": {"llm_errors": errors}},
                            source_counts={}, links=[], **kw)

    def test_a_total_outage_leaves_the_cache_alone(self, cache):
        """The exact 2026-08-12 shape: zero events, every batch errored."""
        self._save([], 814)
        held = json.loads(cache.read_text(encoding="utf-8"))["events"]
        assert len(held) == 2, "an outage overwrote the last good harvest"

    def test_a_genuine_empty_night_keeps_standing_events(self, cache):
        """Nothing found, nothing failed. Under accumulation this no longer wipes
        the board — a night that harvested nothing new keeps the standing future
        events rather than clearing them (the old wholesale-replace flicker)."""
        self._save([], 0)
        held = json.loads(cache.read_text(encoding="utf-8"))["events"]
        assert {e["title"] for e in held} == {"Queer Night", "AI Meetup"}

    def test_a_partial_outage_unions_with_prior(self, cache):
        """Some batches answered — what came back is unioned onto the standing
        events, not written over them."""
        self._save([{"title": "Only one got through"}], 400)
        held = json.loads(cache.read_text(encoding="utf-8"))["events"]
        assert {e["title"] for e in held} == {
            "Queer Night", "AI Meetup", "Only one got through"}

    def test_an_empty_cache_is_not_protected(self, tmp_path, monkeypatch):
        """There is nothing to lose, so the write must proceed and record the
        stats — otherwise a first run during an outage leaves no trace."""
        from event_harvester.servers import mcp_server

        path = tmp_path / "empty.json"
        monkeypatch.setattr(mcp_server, "LATEST_EVENTS_FILE", path)
        self._save([], 814)
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["events"] == []

    def test_it_accepts_counts_or_lists(self, cache):
        """`stats["rejects"]` is counts at this call site and lists upstream."""
        self._save([], [{"id": "1"}] * 814)
        assert len(json.loads(cache.read_text(encoding="utf-8"))["events"]) == 2

    def test_it_says_so_loudly(self, cache, caplog):
        import logging

        with caplog.at_level(logging.ERROR, logger="event_harvester.mcp"):
            self._save([], 814)
        assert any("Refusing to overwrite" in r.message for r in caplog.records)
