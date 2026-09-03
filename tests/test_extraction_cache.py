"""Tests for the LLM extraction cache (pipeline/extraction_cache.py).

The load-bearing property is the safety rule: a message is cached as *no
event* only when its whole batch was empty, and cached *with* events only for
events confidently linked to it. Everything else must be re-extracted, so no
real event can silently stop appearing.
"""

from __future__ import annotations

import time

import pytest

from event_harvester.pipeline import extraction_cache as xcache


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(xcache, "CACHE_FILE", tmp_path / ".extraction_cache.json")
    monkeypatch.delenv("EH_EXTRACTION_CACHE", raising=False)


def _msg(mid, content, platform="discord"):
    return {"id": mid, "content": content, "platform": platform}


# ── keying ──────────────────────────────────────────────────────────────

def test_key_is_stable_and_content_sensitive():
    a = _msg("1", "Rope jam Friday 7pm")
    assert xcache.message_key(a) == xcache.message_key(dict(a))
    assert xcache.message_key(a) != xcache.message_key(_msg("1", "Rope jam Saturday 7pm"))


def test_key_separates_platforms_and_versions(monkeypatch):
    same_text = "identical text"
    assert xcache.message_key(_msg("1", same_text, "discord")) != \
        xcache.message_key(_msg("1", same_text, "telegram"))
    k1 = xcache.message_key(_msg("1", same_text))
    monkeypatch.setattr(xcache, "_VERSION", "2")
    assert xcache.message_key(_msg("1", same_text)) != k1  # version bump invalidates


# ── enabled flag ────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,on", [("0", False), ("false", False), ("OFF", False),
                                    ("1", True), ("", True), ("yes-ish", True)])
def test_enabled_flag(monkeypatch, val, on):
    monkeypatch.setenv("EH_EXTRACTION_CACHE", val)
    assert xcache.enabled() is on


# ── partition ───────────────────────────────────────────────────────────

def test_partition_splits_hits_from_misses():
    hit, miss = _msg("1", "known"), _msg("2", "novel")
    cache = {xcache.message_key(hit): {"events": [{"title": "Known Event"}], "ts": time.time()}}
    to_extract, cached_events, hits = xcache.partition([hit, miss], cache)
    assert [m["id"] for m in to_extract] == ["2"]
    assert hits == 1
    assert [e["title"] for e in cached_events] == ["Known Event"]


def test_partition_counts_negative_hit_but_yields_no_events():
    neg = _msg("1", "just chatter")
    cache = {xcache.message_key(neg): {"events": [], "ts": time.time()}}
    to_extract, cached_events, hits = xcache.partition([neg], cache)
    assert to_extract == [] and cached_events == [] and hits == 1  # skipped the LLM, no event


def test_partition_returns_deep_copies():
    hit = _msg("1", "known")
    stored = {"title": "E", "_source_ids": ["1"]}
    cache = {xcache.message_key(hit): {"events": [stored], "ts": time.time()}}
    _, cached_events, _ = xcache.partition([hit], cache)
    cached_events[0]["title"] = "MUTATED"
    assert stored["title"] == "E"  # cache untouched by downstream mutation


# ── record: the safety rule ───────────────────────────────────────────────

def test_record_negative_only_for_empty_batches():
    m = _msg("1", "not an event")
    cache = {}
    xcache.record(cache, [m], events=[], rejects={"llm_no_events": [m], "llm_errors": []})
    assert cache[xcache.message_key(m)]["events"] == []


def test_record_positive_for_linked_event():
    m = _msg("1", "Munch tonight")
    ev = {"title": "Munch", "_source_ids": ["1"]}
    cache = {}
    xcache.record(cache, [m], events=[ev], rejects={"llm_no_events": [], "llm_errors": []})
    assert [e["title"] for e in cache[xcache.message_key(m)]["events"]] == ["Munch"]


def test_record_does_not_cache_errored_message():
    # An outage must never be remembered as 'no event' — it has to be retried.
    m = _msg("1", "reached nobody")
    cache = {}
    xcache.record(cache, [m], events=[], rejects={"llm_no_events": [], "llm_errors": [m]})
    assert xcache.message_key(m) not in cache


def test_record_does_not_cache_ambiguous_event_bearing_message():
    # In a batch that produced an event, a message the event did NOT link to is
    # left uncached: we cannot prove it was empty, so we re-extract it.
    linked, other = _msg("1", "the event msg"), _msg("2", "sibling in same batch")
    ev = {"title": "E", "_source_ids": ["1"]}
    cache = {}
    xcache.record(cache, [linked, other], events=[ev],
                  rejects={"llm_no_events": [], "llm_errors": []})
    assert xcache.message_key(linked) in cache
    assert xcache.message_key(other) not in cache  # not proven empty -> re-extract


def test_record_then_partition_round_trip():
    m = _msg("1", "recurring rope jam")
    ev = {"title": "Rope Jam", "_source_ids": ["1"]}
    cache = {}
    xcache.record(cache, [m], events=[ev], rejects={"llm_no_events": [], "llm_errors": []})
    to_extract, cached_events, hits = xcache.partition([m], cache)
    assert to_extract == [] and hits == 1
    assert [e["title"] for e in cached_events] == ["Rope Jam"]


# ── save: pruning ─────────────────────────────────────────────────────────

def test_save_prunes_by_ttl(monkeypatch):
    now = time.time()
    cache = {
        "fresh": {"events": [], "ts": now},
        "stale": {"events": [], "ts": now - xcache._TTL_SECONDS - 1},
    }
    xcache.save(cache)
    assert "fresh" in cache and "stale" not in cache
    assert xcache.load() == cache


def test_save_caps_total_size(monkeypatch):
    monkeypatch.setattr(xcache, "_MAX_ENTRIES", 3)
    now = time.time()
    cache = {f"k{i}": {"events": [], "ts": now + i} for i in range(6)}
    xcache.save(cache)
    assert len(cache) == 3
    assert set(cache) == {"k3", "k4", "k5"}  # oldest (k0..k2) evicted


# ── wiring: extract_events_llm skips the LLM on the second run ─────────────

def test_extract_events_llm_reuses_cache_across_runs(monkeypatch):
    from types import SimpleNamespace

    from event_harvester.config import LLMConfig
    from event_harvester.pipeline import analysis

    msgs = [_msg("1", "EVENT rope jam"), _msg("2", "hello chatter"), _msg("3", "EVENT munch")]

    # Filtering is out of scope here — pass every message straight through.
    monkeypatch.setattr(analysis, "filter_for_extraction",
                        lambda messages, caps, label=None:
                        SimpleNamespace(messages=list(messages), rejects={}))

    sent: list[list[str]] = []

    def fake_cloud(messages, days_back, cfg, batch_size=5):
        sent.append([m["id"] for m in messages])
        events, no_ev = [], []
        for m in messages:
            if "EVENT" in m["content"]:
                events.append({"title": m["content"], "_source_ids": [m["id"]],
                               "date": "2099-01-01"})
            else:
                no_ev.append(m)  # empty batch member -> safe negative
        return "sum", events, {"llm_past": [], "llm_no_events": no_ev, "llm_errors": []}

    monkeypatch.setattr(analysis, "_extract_events_cloud", fake_cloud)

    cfg = LLMConfig(model="qwen3-8b", api_base="http://local", backend="litellm")

    r1 = analysis.extract_events_llm(msgs, 7, cfg)
    assert sent[0] == ["1", "2", "3"]  # cold cache: everything extracted
    assert {e["title"] for e in r1.events} == {"EVENT rope jam", "EVENT munch"}

    r2 = analysis.extract_events_llm(msgs, 7, cfg)
    assert sent[1] == []  # warm cache: not one message re-sent to the LLM
    assert {e["title"] for e in r2.events} == {"EVENT rope jam", "EVENT munch"}

    # Edit message 2 into an event: only it should reach the LLM the third run.
    msgs[1]["content"] = "EVENT newly added"
    r3 = analysis.extract_events_llm(msgs, 7, cfg)
    assert sent[2] == ["2"]
    assert {e["title"] for e in r3.events} == {"EVENT rope jam", "EVENT munch", "EVENT newly added"}
