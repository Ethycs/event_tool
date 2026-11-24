"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_job_queue(tmp_path, monkeypatch):
    """No test may touch the real harvest queue.

    This is not hypothetical. A suite run on 2026-08-04 submitted a real job
    (`{'days': 7}`, submitted_by=timer) into `data/.jobs/`, where it has been
    sitting queued ever since — armed to run a full unattended harvest the
    moment a runner starts. The same run then blocked for 30 minutes on
    `wait_for`, which is how it was noticed at all.

    The cause was a test that exercised `site._collect` after it grew a submit
    path, while patching only `Pipeline`. Autouse, because remembering to
    patch `JOBS_DIR` in the right test is exactly the discipline that failed.
    """
    from event_harvester import jobs

    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "_jobs")


@pytest.fixture(autouse=True)
def _isolate_extraction_cache(tmp_path, monkeypatch):
    """No test may read or write the real extraction cache.

    `extract_events_llm` now memoizes to `data/.extraction_cache.json`; without
    this, any test that reaches the extractor pollutes that file and the next
    test's hit/miss behaviour depends on run order. Autouse for the same reason
    as the job queue above — remembering to patch it per test is the discipline
    that fails.
    """
    from event_harvester.pipeline import extraction_cache

    monkeypatch.setattr(extraction_cache, "CACHE_FILE",
                        tmp_path / ".extraction_cache.json")


@pytest.fixture(autouse=True)
def _isolate_web_source_report(tmp_path, monkeypatch):
    """No test may append to the real per-source run report.

    `fetch_web_sources` writes `data/.web_source_report.json` at the end of
    every run, and several tests call it with fakes. Left alone, a test run
    would land in the history a person reads to see which sources are failing.
    """
    from event_harvester.sources import web_fetch

    monkeypatch.setattr(web_fetch, "_REPORT_FILE", tmp_path / ".web_source_report.json")


@pytest.fixture(autouse=True)
def _isolate_web_access_log(tmp_path, monkeypatch):
    """No test may write the real access log or move a source's recorded state.

    The log is how the length of a passed bot check gets measured; a test run
    that flipped `erobay` to `blocked` and back would be read as an
    observation of the site.
    """
    from event_harvester.sources import web_access_log

    monkeypatch.setattr(web_access_log, "LOG_FILE", tmp_path / ".web_access_log.jsonl")
    monkeypatch.setattr(web_access_log, "STATE_FILE", tmp_path / ".web_access_state.json")


@pytest.fixture(autouse=True)
def _no_real_native_browser(monkeypatch):
    """No test may reach the Chrome a person keeps open on the native port,
    the UC service's native window named in this machine's ``.env``, or the
    desktop's X server.

    On 2026-11-24 a test that named the open-browser route without stubbing
    the page preparation opened a tab in the owner's live browser and held it
    for fifteen seconds. Tests that want any of these say so themselves.
    """
    from event_harvester.sources import native_window, uc_browser

    monkeypatch.setattr(uc_browser, "_port_open", lambda port: False)
    monkeypatch.setattr(native_window, "native_window_state", lambda uc_kwargs: None)
    monkeypatch.setenv("WEB_UC_NATIVE_WINDOW", "")


@pytest.fixture
def sample_messages():
    """A set of sample messages from both platforms."""
    return [
        {
            "platform": "discord",
            "id": "1001",
            "timestamp": "2026-03-15T10:00:00+00:00",
            "author": "alice",
            "channel": "123456",
            "content": "Hey, can someone review the PR for the auth fix?",
        },
        {
            "platform": "discord",
            "id": "1002",
            "timestamp": "2026-03-15T10:05:00+00:00",
            "author": "bob",
            "channel": "123456",
            "content": "Sure, I'll take a look after lunch.",
        },
        {
            "platform": "telegram",
            "id": "2001",
            "timestamp": "2026-03-15T11:00:00+00:00",
            "author": "charlie",
            "channel": "Project Updates",
            "content": "Deploy to staging is done. Need someone to run smoke tests before 5pm.",
        },
        {
            "platform": "telegram",
            "id": "2002",
            "timestamp": "2026-03-15T11:30:00+00:00",
            "author": "alice",
            "channel": "Project Updates",
            "content": "Nice weather today!",
        },
        {
            "platform": "discord",
            "id": "1003",
            "timestamp": "2026-03-16T09:00:00+00:00",
            "author": "dave",
            "channel": "789012",
            "content": "The API rate limiter config needs updating before the launch next Tuesday.",
        },
    ]
