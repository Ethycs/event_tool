"""Tests for the local-worker interlock's I/O timeout.

`RyzenSession._recv` used to call `readline()` on the worker's stdout with no
timeout. That recovers from a *clean* worker exit (stdout closes, readline
returns "") but not from a worker that is alive and wedged — hung on NPU init,
stuck in a generate loop, deadlocked. In that case `chat_completion(backend=
"local")` hung indefinitely, with no timeout above it to break the wait.

These tests use a fake process object rather than spawning a real worker, so
they run anywhere and take milliseconds.
"""

from __future__ import annotations

import threading
import time

import pytest

from event_harvester.interlock import InterlockError, RyzenSession


class _FakeStdout:
    """stdout whose readline() blocks until released (or forever)."""

    def __init__(self, release: threading.Event | None = None, line: str = ""):
        self.release = release
        self.line = line
        self.readline_started = threading.Event()

    def readline(self) -> str:
        self.readline_started.set()
        if self.release is None:
            # Simulate a wedged worker: block until the test process exits.
            # Daemon thread, so it cannot keep pytest alive.
            time.sleep(3600)
            return ""
        self.release.wait(timeout=10)
        return self.line


class _FakeProc:
    def __init__(self, stdout: _FakeStdout):
        self.stdout = stdout
        self.stdin = None
        self.terminated = False
        self.killed = False
        self._returncode: int | None = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):  # pragma: no cover - only on escalation
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode


def _session(proc) -> RyzenSession:
    sess = RyzenSession.__new__(RyzenSession)  # bypass __init__/model checks
    sess._proc = proc
    sess.io_timeout_s = 0.2
    return sess


def test_wedged_worker_raises_instead_of_hanging():
    """The whole point: a bounded failure, not an unbounded wait."""
    proc = _FakeProc(_FakeStdout(release=None))
    sess = _session(proc)

    started = time.monotonic()
    with pytest.raises(InterlockError, match="timed out"):
        sess._recv()
    elapsed = time.monotonic() - started

    # Must fail on roughly the configured timeout, not sit there.
    assert elapsed < 5, f"_recv took {elapsed:.1f}s — it is still blocking"


def test_wedged_worker_is_killed():
    """A session whose reply we abandoned is desynchronised — don't reuse it."""
    proc = _FakeProc(_FakeStdout(release=None))
    sess = _session(proc)

    with pytest.raises(InterlockError):
        sess._recv()

    assert proc.terminated is True


def test_responsive_worker_still_works():
    release = threading.Event()
    release.set()
    proc = _FakeProc(_FakeStdout(release=release, line='{"ok": true}\n'))
    sess = _session(proc)

    assert sess._recv() == {"ok": True}
    assert proc.terminated is False


def test_slow_but_within_timeout_succeeds():
    """A worker that is merely slow must not be killed."""
    release = threading.Event()
    proc = _FakeProc(_FakeStdout(release=release, line='{"ok": 1}\n'))
    sess = _session(proc)
    sess.io_timeout_s = 5.0

    threading.Timer(0.15, release.set).start()
    assert sess._recv() == {"ok": 1}
    assert proc.terminated is False


def test_timeout_can_be_disabled():
    """0 restores the old blocking behaviour for debugging under a breakpoint."""
    release = threading.Event()
    release.set()
    proc = _FakeProc(_FakeStdout(release=release, line='{"ok": 2}\n'))
    sess = _session(proc)
    sess.io_timeout_s = 0

    assert sess._recv() == {"ok": 2}


def test_clean_exit_still_reports_closed_stdout():
    """The pre-existing failure mode must keep its clearer message."""
    release = threading.Event()
    release.set()
    proc = _FakeProc(_FakeStdout(release=release, line=""))
    sess = _session(proc)

    with pytest.raises(InterlockError, match="closed stdout"):
        sess._recv()
