"""Is the native browser's window actually on screen?

A Chrome whose window is minimized does not draw its pages. A page that is not
drawn does not finish a bot check, and need not finish loading at all.

**Measured 2026-11-24 on ``ra-sf``**, same route, same 21 detail links: with
the browser's window iconified the source produced nothing in 1200 s and was
declared hung; with the window mapped it returned 20 messages in 81 s. One run
each side. Nothing in event_tool or in the Universal Controller service
minimizes that window, and what did was not found, so the state is checked
here, before native work starts, and a window that is not drawn is refused
with the reason. It is never mapped or focused from here: the desktop is the
owner's.

Everything is read from outside the browser: its debugging port from the route,
its process from ``/proc``, its window state from the X server through
``xprop``. Anything that cannot be read answers ``None``, and ``None`` does not
refuse anything: not being able to tell is not the same as not being drawn.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("event_harvester.uc_browser")

#: ICCCM ``WM_STATE`` values, as ``xprop`` prints them.
_STATES = {"Normal": "normal", "Iconic": "iconic", "Withdrawn": "withdrawn"}


class NativeWindowNotDrawn(RuntimeError):
    """The native browser has a window and none of it is on screen."""


def debug_port(uc_kwargs: dict) -> int | None:
    """The debugging port of the browser this route will use, if it is running."""
    endpoint = uc_kwargs.get("cdp_endpoint")
    if endpoint is None and "service" in uc_kwargs:
        from uc_browser.service.client import ServiceClient

        info = ServiceClient(uc_kwargs["service"]).windows().get(uc_kwargs["window"]) or {}
        endpoint = info.get("cdp") if info.get("running") else None
    if not endpoint:
        return None
    return urlparse(endpoint).port


def _is_browser_on_port(cmdline: bytes, port: int) -> bool:
    """Whether ``cmdline`` is the browser process debugging on ``port``.

    Chrome rewrites its command line as one space-joined string, so the
    arguments are split on NULs and on spaces alike. Its helper processes can
    repeat the port and always carry a ``--type``.
    """
    args = cmdline.replace(b"\0", b" ").split()
    return (
        f"--remote-debugging-port={port}".encode() in args
        and not any(a.startswith(b"--type=") for a in args)
    )


def browser_pid(port: int) -> int | None:
    """The browser process listening for debugging on ``port``."""
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if _is_browser_on_port(cmdline.read_bytes(), port):
                return int(cmdline.parent.name)
        except OSError:
            continue
    return None


def _xprop(*args: str) -> str:
    return subprocess.run(
        ["xprop", *args], capture_output=True, text=True, timeout=5, check=False,
    ).stdout


def window_states(pid: int) -> list[str]:
    """The state of each top-level X window belonging to ``pid``."""
    states = []
    for window in re.findall(r"0x[0-9a-f]+", _xprop("-root", "_NET_CLIENT_LIST")):
        props = _xprop("-id", window, "_NET_WM_PID", "WM_STATE")
        owner = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", props)
        state = re.search(r"window state: (\w+)", props)
        if owner and int(owner.group(1)) == pid and state:
            states.append(_STATES.get(state.group(1), state.group(1).lower()))
    return states


def native_window_state(uc_kwargs: dict) -> str | None:
    """``"drawn"``, ``"minimized"``, or ``None`` when it cannot be told.

    Minimized means every window the browser has is iconified. With several
    windows and one of them on screen there is no telling which a new page
    lands in, so that is not refused.
    """
    if not os.environ.get("DISPLAY"):
        return None
    try:
        port = debug_port(uc_kwargs)
        pid = browser_pid(port) if port else None
        states = window_states(pid) if pid else []
    except Exception as e:  # noqa: BLE001 — this check must not cost the source
        logger.debug("native window state could not be read: %s", e)
        return None
    if not states:
        return None
    return "minimized" if all(s == "iconic" for s in states) else "drawn"


def require_drawn(uc_kwargs: dict) -> None:
    """Raise :class:`NativeWindowNotDrawn` if the native browser is minimized."""
    if native_window_state(uc_kwargs) == "minimized":
        raise NativeWindowNotDrawn(
            "the native browser's window is minimized, so nothing in it is drawn: "
            "pages there do not finish a bot check and may not finish loading. "
            "Restore the window on the desktop; it is not restored from here."
        )
