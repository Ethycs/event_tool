"""What code is this process actually running?

A service that cannot answer that is a service you have to do archaeology on.
On 2026-08-05 establishing that `EventHarvesterSite` had been running four-day
-old code took process enumeration, start-time comparison and log inspection —
because `/healthz` reported events and refresh counts and nothing about
itself.

Every long-lived surface now reports `build_info()`: version, git SHA, when
this process started, and its PID. `is_stale()` compares the SHA the process
booted with against what is checked out now, which turns "is the running
service current?" into one field instead of an investigation.

The git SHA is read straight out of `.git/`, not by shelling out. A service
should not spawn a subprocess to answer a health check, and under LocalSystem
`git` may not even be on PATH.
"""

from __future__ import annotations

import functools
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_harvester import __version__
from event_harvester.utils import REPO_ROOT

logger = logging.getLogger("event_harvester.buildinfo")

#: Import time is close enough to process start, and it is the only moment we
#: can observe without asking the OS.
PROCESS_STARTED_AT = datetime.now(timezone.utc)


def _read_head_sha(repo_root: Path | None = None) -> str | None:
    """Resolve HEAD to a SHA by reading `.git/`, without running git.

    Handles the three shapes that turn up: a detached SHA in HEAD, a symbolic
    ref pointing at a loose ref file, and a ref that only exists in
    `packed-refs` (which is the normal state after a clone).
    """
    git_dir = Path(repo_root or REPO_ROOT) / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not head.startswith("ref:"):
        return head or None                       # detached HEAD

    ref = head.partition("ref:")[2].strip()
    loose = git_dir / ref
    try:
        return loose.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass

    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha.strip()
    except OSError:
        pass
    return None


@functools.lru_cache(maxsize=1)
def booted_sha() -> str | None:
    """The SHA this process started on. Cached — deliberately.

    Cached because it must *not* follow the working tree. The whole point is
    to notice that the checkout moved and this process did not.
    """
    return _read_head_sha()


def current_sha() -> str | None:
    """The SHA checked out right now. Uncached, read every call."""
    return _read_head_sha()


def is_stale() -> bool | None:
    """True when the checkout has moved since this process booted.

    None when the SHA cannot be determined at either end — absent information
    must not read as "healthy".
    """
    booted, current = booted_sha(), current_sha()
    if not booted or not current:
        return None
    return booted != current


def uptime_seconds(now: datetime | None = None) -> float:
    return ((now or datetime.now(timezone.utc)) - PROCESS_STARTED_AT).total_seconds()


def build_info(now: datetime | None = None) -> dict[str, Any]:
    """The identity block every long-lived surface reports."""
    booted = booted_sha()
    return {
        "version": __version__,
        "sha": (booted or "")[:12] or None,
        "sha_current": (current_sha() or "")[:12] or None,
        "stale": is_stale(),
        "pid": os.getpid(),
        "started_at": PROCESS_STARTED_AT.isoformat(),
        "uptime_seconds": round(uptime_seconds(now), 1),
    }


def describe() -> str:
    """One line, for logs and CLI output."""
    info = build_info()
    sha = info["sha"] or "unknown"
    flag = " STALE" if info["stale"] else ""
    return f"v{info['version']} @{sha} pid={info['pid']}{flag}"
