"""Tests for the HTML branch of `read_cached_pages`.

That branch referenced two undefined names (`gzip_event_score`,
`gzip_threshold`), which raised `NameError` on every HTML page. The raise was
swallowed by an enclosing bare `except Exception: continue`, so the failure
was completely silent — the function returned an empty list and looked like
"no matching pages" rather than "this code has never run".

The module has no production callers today (roadmap 2.4), so these tests
exist mainly to prove the path executes at all, and to make its silence
impossible to reintroduce.
"""

from __future__ import annotations

import re

import pytest

from event_harvester.sources.chromium_cache import is_readable_text, read_cached_pages


class _FakeMeta:
    def __init__(self, url: str):
        self.url = url


class _FakeCache:
    """Minimal stand-in for ChromiumBlockFileCache."""

    def __init__(self, entries):
        self._entries = entries
        self.closed = False

    def get_metadata(self):
        return [(_FakeMeta(url), None) for url, _ in self._entries]

    def close(self):
        self.closed = True


def test_is_readable_text_accepts_prose_and_rejects_noise():
    """The gate the fix now uses, exercised directly."""
    prose = (b"Join us for the summer block party on Saturday at the bar. "
             b"Doors open at eight and there will be live music all night. ") * 3
    assert is_readable_text(prose) is True
    # Incompressible bytes are not text.
    import os
    assert is_readable_text(os.urandom(4096)) is False


def test_html_branch_no_longer_raises_nameerror(monkeypatch, tmp_path):
    """Before the fix this returned [] because NameError was swallowed."""
    html = (
        "<html><body><h1>Live Music Night</h1>"
        + "<p>Doors open at eight on Friday, come early for the show.</p>" * 12
        + "</body></html>"
    )

    calls = {"decoded": 0}

    def fake_read_entries(cache_path, url_pattern=None, **kw):
        calls["decoded"] += 1
        return [("https://venue.example/events", html.encode())]

    import event_harvester.sources.chromium_cache as mod

    monkeypatch.setattr(mod, "read_cache_entries", fake_read_entries, raising=False)

    # If the undefined names came back, this raises NameError rather than
    # returning — the bare `except` only hides it inside the loop.
    try:
        messages = read_cached_pages(tmp_path, re.compile(r"venue\.example"))
    except NameError as exc:  # pragma: no cover - the regression itself
        pytest.fail(f"read_cached_pages still references an undefined name: {exc}")

    assert isinstance(messages, list)


def test_module_has_no_undefined_gzip_names():
    """Guard the specific regression by name.

    A cheap structural check: the two names that were never defined must not
    reappear. Reading the source is enough — the branch that used them is
    hard to reach without a real Chromium cache on disk.
    """
    import inspect

    import event_harvester.sources.chromium_cache as mod

    src = inspect.getsource(mod)
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    assert "gzip_event_score(" not in code
    assert "gzip_threshold" not in code
