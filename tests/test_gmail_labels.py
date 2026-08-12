"""Tests for labelling the mail that produced an event.

This is the only part of the harvest that **writes to a real mailbox**, so the
tests lean on the conservative side of the design: a message is labelled only
when its link back to an event is unambiguous, `--dry-run` writes nothing, and
the operation is additive so re-running cannot compound a mistake.

The back-reference itself (`_source_ids`) did not exist before 2026-08-10 --
`EH-STD-EVENT-v0` §3.1 says `source` is free text and explicitly "NOT a
structured back-reference" -- so `attach_source_ids` is tested here too.
"""

from __future__ import annotations

import pytest

from event_harvester.config import GmailConfig
from event_harvester.pipeline.analysis import attach_source_ids
from event_harvester.pipeline.event_match import dedup_events
from event_harvester.sources import gmail


def _m(mid: str, author: str = "a@x.com", content: str = "") -> dict:
    return {"platform": "gmail", "id": mid, "author": author, "content": content}


class TestAttachSourceIds:
    def test_a_link_in_the_body_identifies_the_message(self):
        batch = [_m("m1", content="RSVP at https://lu.ma/abcdef1234"), _m("m2")]
        events = [{"title": "T", "link": "https://lu.ma/abcdef1234"}]
        attach_source_ids(events, batch)
        assert events[0]["_source_ids"] == ["m1"]

    def test_the_author_identifies_the_message(self):
        batch = [_m("m1", author="Ops <events@venue.com>"), _m("m2", author="b@y.com")]
        events = [{"title": "T", "source": "@events@venue.com in #INBOX"}]
        attach_source_ids(events, batch)
        assert events[0]["_source_ids"] == ["m1"]

    def test_two_equal_matches_resolve_to_nothing(self):
        """Ambiguity must not be guessed at -- this writes to a mailbox."""
        url = "https://lu.ma/abcdef1234"
        batch = [_m("m1", content=url), _m("m2", content=url)]
        events = [{"title": "T", "link": url}]
        attach_source_ids(events, batch)
        assert events[0]["_source_ids"] == []

    def test_no_match_leaves_an_empty_list_not_a_missing_key(self):
        """Callers must be able to tell 'unlinked' from 'never attempted'."""
        events = [{"title": "T", "link": "https://lu.ma/zzz"}]
        attach_source_ids(events, [_m("m1")])
        assert events[0]["_source_ids"] == []

    def test_a_trivially_short_link_is_not_a_match(self):
        """'TBD' or a bare slash would otherwise match half the mailbox."""
        batch = [_m("m1", content="see TBD for details")]
        events = [{"title": "T", "link": "TBD"}]
        attach_source_ids(events, batch)
        assert events[0]["_source_ids"] == []

    def test_titles_are_never_used_to_match(self):
        """LLM titles are rewritten, not quoted -- an unverifiable signal."""
        batch = [_m("m1", content="Join the Summer Fest Energy party")]
        events = [{"title": "Summer Fest Energy", "link": "", "source": ""}]
        attach_source_ids(events, batch)
        assert events[0]["_source_ids"] == []


class TestDedupKeepsProvenance:
    """Two emails announcing one event are still two emails."""

    def test_a_collapsed_duplicate_donates_its_source_id(self):
        url = "https://lu.ma/samething123"
        events = [
            {"title": "Meetup", "date": "2026-09-01", "link": url, "_source_ids": ["m1"]},
            {"title": "Meetup", "date": "2026-09-01", "link": url, "_source_ids": ["m2"]},
        ]
        out = dedup_events(events)
        assert len(out) == 1
        assert out[0]["_source_ids"] == ["m1", "m2"]

    def test_a_fuzzy_duplicate_also_donates(self):
        events = [
            {"title": "AI Infra Meetup", "date": "2026-09-01",
             "location": "SF", "_source_ids": ["m1"]},
            {"title": "AI Infra Meetup", "date": "2026-09-01",
             "location": "SF", "_source_ids": ["m2"]},
        ]
        out = dedup_events(events)
        assert len(out) == 1
        assert set(out[0]["_source_ids"]) == {"m1", "m2"}

    def test_ids_are_not_duplicated(self):
        url = "https://lu.ma/samething123"
        events = [
            {"title": "M", "link": url, "_source_ids": ["m1"]},
            {"title": "M", "link": url, "_source_ids": ["m1"]},
        ]
        assert dedup_events(events)[0]["_source_ids"] == ["m1"]

    def test_events_without_provenance_still_dedup(self):
        url = "https://lu.ma/samething123"
        out = dedup_events([{"title": "M", "link": url}, {"title": "M", "link": url}])
        assert len(out) == 1


class _FakeService:
    """Minimal stand-in for the Gmail client, recording what it was asked."""

    def __init__(self, labels=None, fail_create=False):
        self._labels = labels or []
        self._fail_create = fail_create
        self.created: list[dict] = []
        self.modified: list[dict] = []

    # -- fluent chain the real client uses --
    def users(self):
        return self

    def labels(self):
        return _Labels(self)

    def messages(self):
        return _Messages(self)


class _Labels:
    def __init__(self, svc):
        self.svc = svc

    def list(self, userId):  # noqa: N803 -- Google's parameter name
        return _Exec({"labels": self.svc._labels})

    def create(self, userId, body):  # noqa: N803
        if self.svc._fail_create:
            return _Exec(None, boom=True)
        self.svc.created.append(body)
        return _Exec({"id": "NEW", "name": body["name"]})


class _Messages:
    def __init__(self, svc):
        self.svc = svc

    def batchModify(self, userId, body):  # noqa: N802,N803 -- Google's names
        self.svc.modified.append(body)
        return _Exec({})


class _Exec:
    def __init__(self, result, boom=False):
        self._result = result
        self._boom = boom

    def execute(self):
        if self._boom:
            raise RuntimeError("api said no")
        return self._result


@pytest.fixture
def cfg():
    return GmailConfig()


def _patch_service(monkeypatch, svc):
    monkeypatch.setattr(gmail, "_get_service", lambda cfg: svc)


class TestEnsureLabel:
    def test_an_existing_label_is_reused(self, cfg, monkeypatch):
        svc = _FakeService(labels=[{"id": "L1", "name": "events"}])
        _patch_service(monkeypatch, svc)
        assert gmail.ensure_label(cfg, "events") == "L1"
        assert svc.created == [], "must not create a duplicate"

    def test_the_lookup_is_case_insensitive(self, cfg, monkeypatch):
        """Gmail rejects a label that differs only in case, so adopt it."""
        svc = _FakeService(labels=[{"id": "L1", "name": "Events"}])
        _patch_service(monkeypatch, svc)
        assert gmail.ensure_label(cfg, "events") == "L1"
        assert svc.created == []

    def test_a_missing_label_is_created_and_visible(self, cfg, monkeypatch):
        svc = _FakeService(labels=[{"id": "L9", "name": "other"}])
        _patch_service(monkeypatch, svc)
        assert gmail.ensure_label(cfg, "events") == "NEW"
        assert svc.created[0]["name"] == "events"
        assert svc.created[0]["labelListVisibility"] == "labelShow"

    def test_a_creation_failure_is_none_not_an_exception(self, cfg, monkeypatch):
        _patch_service(monkeypatch, _FakeService(labels=[], fail_create=True))
        assert gmail.ensure_label(cfg, "events") is None

    def test_no_credentials_is_none(self, cfg, monkeypatch):
        monkeypatch.setattr(gmail, "_get_service", lambda cfg: None)
        assert gmail.ensure_label(cfg, "events") is None


class TestApplyLabel:
    def test_the_call_is_additive(self, cfg, monkeypatch):
        """It must add a label -- never remove one, never leave the inbox."""
        svc = _FakeService()
        _patch_service(monkeypatch, svc)
        gmail.apply_label(cfg, ["a", "b"], "L1")
        body = svc.modified[0]
        assert body["addLabelIds"] == ["L1"]
        assert "removeLabelIds" not in body

    def test_it_batches_rather_than_calling_per_message(self, cfg, monkeypatch):
        svc = _FakeService()
        _patch_service(monkeypatch, svc)
        assert gmail.apply_label(cfg, [str(i) for i in range(50)], "L1") == 50
        assert len(svc.modified) == 1

    def test_more_than_a_thousand_ids_are_chunked(self, cfg, monkeypatch):
        """batchModify's documented ceiling is 1000 ids per call."""
        svc = _FakeService()
        _patch_service(monkeypatch, svc)
        n = gmail.apply_label(cfg, [str(i) for i in range(2500)], "L1")
        assert n == 2500
        assert [len(b["ids"]) for b in svc.modified] == [1000, 1000, 500]

    def test_nothing_to_do_makes_no_call(self, cfg, monkeypatch):
        svc = _FakeService()
        _patch_service(monkeypatch, svc)
        assert gmail.apply_label(cfg, [], "L1") == 0
        assert gmail.apply_label(cfg, ["a"], "") == 0
        assert svc.modified == []


class TestLabelEventMessages:
    def test_only_linked_messages_are_labelled(self, cfg, monkeypatch):
        svc = _FakeService(labels=[{"id": "L1", "name": "events"}])
        _patch_service(monkeypatch, svc)
        events = [
            {"title": "a", "_source_ids": ["m1"]},
            {"title": "b", "_source_ids": []},
            {"title": "c"},
        ]
        assert gmail.label_event_messages(cfg, events) == 1
        assert svc.modified[0]["ids"] == ["m1"]

    def test_ids_are_deduplicated_across_events(self, cfg, monkeypatch):
        svc = _FakeService(labels=[{"id": "L1", "name": "events"}])
        _patch_service(monkeypatch, svc)
        events = [{"_source_ids": ["m1"]}, {"_source_ids": ["m1", "m2"]}]
        gmail.label_event_messages(cfg, events)
        assert svc.modified[0]["ids"] == ["m1", "m2"]

    def test_no_linked_events_makes_no_label_at_all(self, cfg, monkeypatch):
        """Do not create an empty label as a side effect of finding nothing."""
        svc = _FakeService(labels=[])
        _patch_service(monkeypatch, svc)
        assert gmail.label_event_messages(cfg, [{"title": "a"}]) == 0
        assert svc.created == []
        assert svc.modified == []


class TestTheDryRunContract:
    """This edits a mailbox; --dry-run must not."""

    def test_dry_run_writes_nothing(self, monkeypatch, capsys):
        from event_harvester.cli.commands import harvest as harvest_cmd

        def boom(*a, **k):
            raise AssertionError("--dry-run must not touch Gmail")

        monkeypatch.setattr(gmail, "label_event_messages", boom)
        cfg = type("C", (), {"gmail": GmailConfig()})()
        harvest_cmd._label_event_mail(
            cfg, [{"_source_ids": ["m1"]}], dry_run=True,
        )
        assert "would tag 1" in capsys.readouterr().out

    def test_an_empty_label_name_disables_it(self, monkeypatch):
        from event_harvester.cli.commands import harvest as harvest_cmd

        def boom(*a, **k):
            raise AssertionError("an empty label name must disable labelling")

        monkeypatch.setattr(gmail, "label_event_messages", boom)
        cfg = type("C", (), {"gmail": GmailConfig(events_label="")})()
        harvest_cmd._label_event_mail(cfg, [{"_source_ids": ["m1"]}], dry_run=False)

    def test_a_real_run_labels(self, monkeypatch):
        from event_harvester.cli.commands import harvest as harvest_cmd

        seen = {}
        monkeypatch.setattr(
            gmail, "label_event_messages",
            lambda c, e, name: seen.setdefault("name", name) and 0 or 1,
        )
        cfg = type("C", (), {"gmail": GmailConfig()})()
        harvest_cmd._label_event_mail(cfg, [{"_source_ids": ["m1"]}], dry_run=False)
        assert seen["name"] == "events"
