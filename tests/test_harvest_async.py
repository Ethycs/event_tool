"""Tests for the concurrent refactor of ``harvest_messages``.

Each test patches every source function at the harvest module's
namespace so the real network/disk-backed implementations never run,
then calls ``harvest_messages`` with ``skip_cache=True`` to force a
fresh harvest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import patch

import pytest

from event_harvester.config import AppConfig
from event_harvester.pipeline import harvest as harvest_mod


def _make_msg(platform: str) -> dict:
    return {
        "platform": platform,
        "id": f"{platform}-1",
        "timestamp": "2026-05-13T00:00:00+00:00",
        "author": "tester",
        "channel": "c",
        "content": f"hello from {platform}",
    }


@pytest.fixture
def cfg() -> AppConfig:
    """Default AppConfig — fields are dataclasses with sensible defaults."""
    return AppConfig()


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Redirect the cache file to a tmp path so tests don't read/write
    the real ``data/.message_cache.json``."""
    monkeypatch.setattr(harvest_mod, "CACHE_FILE", tmp_path / ".message_cache.json")
    yield


def _patch_all_sources(
    *,
    discord=None,
    telegram=None,
    gmail=None,
    signal=None,
    web=None,
):
    """Return a context manager stack patching every source in harvest_mod.

    ``telegram`` and ``web`` (well, telegram is native-async; web is sync
    in this codebase) are replaced just like the rest — we only care
    about the call-shape from inside the wrappers.
    """
    # Sensible defaults: return a single tagged message per source.
    discord = discord or (lambda *a, **kw: [_make_msg("discord")])
    gmail = gmail or (lambda *a, **kw: [_make_msg("gmail")])
    signal = signal or (lambda *a, **kw: [_make_msg("signal")])
    web = web or (lambda *a, **kw: [_make_msg("web")])

    async def default_telegram(*a, **kw):
        return [_make_msg("telegram")]

    telegram = telegram or default_telegram

    return [
        patch.object(harvest_mod, "read_discord_messages", side_effect=discord),
        patch.object(harvest_mod, "read_telegram_messages", side_effect=telegram),
        patch.object(harvest_mod, "fetch_gmail_messages", side_effect=gmail),
        patch.object(harvest_mod, "read_signal_messages", side_effect=signal),
        patch.object(harvest_mod, "fetch_web_sources", side_effect=web),
    ]


def _run_harvest(cfg: AppConfig) -> list[dict]:
    """Synchronous wrapper around ``harvest_messages`` for plain test fns."""
    return asyncio.run(harvest_mod.harvest_messages(cfg, skip_cache=True))


def test_all_five_sources_ran(cfg):
    patches = _patch_all_sources()
    for p in patches:
        p.start()
    try:
        result = _run_harvest(cfg)
    finally:
        for p in patches:
            p.stop()

    platforms = {m["platform"] for m in result}
    assert platforms == {"discord", "telegram", "gmail", "signal", "web"}
    assert len(result) == 5


def test_aggregation_order_is_deterministic(cfg):
    """Sources complete in shuffled order; the aggregated list must
    still be (discord, telegram, gmail, signal, web)."""

    # Each sync source sleeps a different amount before returning.
    def discord(*a, **kw):
        time.sleep(0.20)
        return [_make_msg("discord")]

    def gmail(*a, **kw):
        time.sleep(0.05)
        return [_make_msg("gmail")]

    def signal(*a, **kw):
        time.sleep(0.15)
        return [_make_msg("signal")]

    def web(*a, **kw):
        time.sleep(0.10)
        return [_make_msg("web")]

    async def telegram(*a, **kw):
        await asyncio.sleep(0.08)
        return [_make_msg("telegram")]

    patches = _patch_all_sources(
        discord=discord, telegram=telegram, gmail=gmail, signal=signal, web=web,
    )
    for p in patches:
        p.start()
    try:
        result = _run_harvest(cfg)
    finally:
        for p in patches:
            p.stop()

    assert [m["platform"] for m in result] == [
        "discord", "telegram", "gmail", "signal", "web",
    ]


def test_one_failure_does_not_kill_the_rest(cfg, caplog):
    """If gmail blows up, the other four sources should still contribute,
    and an error log mentioning gmail should be emitted."""

    def gmail_boom(*a, **kw):
        raise RuntimeError("gmail exploded")

    patches = _patch_all_sources(gmail=gmail_boom)
    for p in patches:
        p.start()
    try:
        with caplog.at_level(logging.ERROR, logger="event_harvester"):
            result = _run_harvest(cfg)
    finally:
        for p in patches:
            p.stop()

    platforms = {m["platform"] for m in result}
    assert platforms == {"discord", "telegram", "signal", "web"}
    assert any(
        "gmail" in rec.getMessage().lower() for rec in caplog.records
    ), f"expected an error log mentioning gmail, got: {[r.getMessage() for r in caplog.records]}"


def test_wall_time_is_roughly_max_not_sum(cfg):
    """Five sources each sleeping 0.5s should finish well under the
    2.5s a sequential implementation would take."""
    delay = 0.5

    def slow_sync(*a, **kw):
        time.sleep(delay)
        # Tag by inspection isn't easy here — just return a placeholder.
        return [_make_msg("x")]

    async def slow_async(*a, **kw):
        await asyncio.sleep(delay)
        return [_make_msg("telegram")]

    patches = _patch_all_sources(
        discord=slow_sync, telegram=slow_async,
        gmail=slow_sync, signal=slow_sync, web=slow_sync,
    )
    for p in patches:
        p.start()
    try:
        start = time.monotonic()
        _run_harvest(cfg)
        elapsed = time.monotonic() - start
    finally:
        for p in patches:
            p.stop()

    # Sequential would be ~2.5s. Concurrent should be ~0.5s plus
    # thread-pool startup; allow up to 1.5s for slow CI / cold pools.
    assert elapsed < 1.5, f"harvest took {elapsed:.2f}s, expected < 1.5s"


# ── web-source name filtering ────────────────────────────────────────


class TestWebSourceFilter:
    """A name filter that matches nothing must harvest nothing.

    `fetch_web_sources(sources=None)` means "load them all", so the old code
    — which simply never set `sources` when zero entries matched — inverted
    the request. The shipped `no-web` profile (`web_sources: ["__none__"]`)
    ran a *full* web harvest, and so did any typo'd `--web-source`.
    """

    def _run_with_filter(self, cfg, monkeypatch, *, all_sources, **harvest_kw):
        seen: dict = {}

        def fake_fetch(**kw):
            seen["sources"] = kw.get("sources", "UNSET")
            return []

        monkeypatch.setattr(
            "event_harvester.sources.web_fetch._load_web_sources",
            lambda: all_sources,
        )
        patches = _patch_all_sources(web=lambda **kw: fake_fetch(**kw))
        for p in patches:
            p.start()
        try:
            asyncio.run(harvest_mod.harvest_messages(
                cfg, skip_cache=True,
                no_discord=True, no_telegram=True, no_gmail=True, no_signal=True,
                **harvest_kw,
            ))
        finally:
            for p in patches:
                p.stop()
        return seen.get("sources", "UNSET")

    def test_matching_name_passes_only_that_source(self, cfg, monkeypatch):
        all_src = [{"name": "luma", "url": "u1"}, {"name": "erobay", "url": "u2"}]
        got = self._run_with_filter(
            cfg, monkeypatch, all_sources=all_src, web_source="luma",
        )
        assert [s["name"] for s in got] == ["luma"]

    def test_zero_match_passes_empty_list_not_everything(self, cfg, monkeypatch):
        """The regression: an unmatched filter must not mean 'all sources'."""
        all_src = [{"name": "luma", "url": "u1"}, {"name": "erobay", "url": "u2"}]
        got = self._run_with_filter(
            cfg, monkeypatch, all_sources=all_src, web_source="__none__",
        )
        assert got == [], "a zero-match filter must harvest no web sources"

    def test_typo_does_not_silently_harvest_all(self, cfg, monkeypatch):
        all_src = [{"name": "luma", "url": "u1"}]
        got = self._run_with_filter(
            cfg, monkeypatch, all_sources=all_src, web_source="lumaa",
        )
        assert got == []

    def test_no_filter_leaves_sources_unset(self, cfg, monkeypatch):
        """With no filter at all, fetch_web_sources must load them itself."""
        all_src = [{"name": "luma", "url": "u1"}]
        got = self._run_with_filter(cfg, monkeypatch, all_sources=all_src)
        assert got == "UNSET"
