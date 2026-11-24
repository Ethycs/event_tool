"""Native work is refused in a browser whose window is not on screen."""

from __future__ import annotations

import pytest

from event_harvester.sources import native_window, uc_browser
from event_harvester.sources.native_window import NativeWindowNotDrawn
from event_harvester.sources.native_window import native_window_state as real_state

ROUTE = {"cdp_endpoint": "http://127.0.0.1:9222", "label": "event_tool"}

CLIENT_LIST = "_NET_CLIENT_LIST(WINDOW): window id # 0xa00001, 0xb00002, 0xc00003\n"


def _props(pid: int, state: str) -> str:
    return (
        f"_NET_WM_PID(CARDINAL) = {pid}\n"
        f"WM_STATE(WM_STATE):\n\t\twindow state: {state}\n\t\ticon window: 0x0\n"
    )


@pytest.fixture
def desktop(monkeypatch):
    """A desktop whose windows are given as ``{window id: (pid, state)}``."""
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(native_window, "browser_pid", lambda port: 4242)

    def install(windows: dict):
        def xprop(*args):
            if args[0] == "-root":
                return CLIENT_LIST
            pid, state = windows.get(args[1], (1, "Normal"))
            return _props(pid, state)

        monkeypatch.setattr(native_window, "_xprop", xprop)

    return install


def test_a_browser_with_its_only_window_iconified_is_minimized(desktop):
    desktop({"0xa00001": (999, "Normal"), "0xb00002": (4242, "Iconic")})
    assert real_state(ROUTE) == "minimized"


def test_another_programs_minimized_window_does_not_count(desktop):
    desktop({"0xa00001": (999, "Iconic"), "0xb00002": (4242, "Normal")})
    assert real_state(ROUTE) == "drawn"


def test_one_window_on_screen_is_enough(desktop):
    desktop({"0xa00001": (4242, "Iconic"), "0xb00002": (4242, "Normal")})
    assert real_state(ROUTE) == "drawn"


def test_what_cannot_be_read_is_not_a_verdict(desktop, monkeypatch):
    desktop({"0xa00001": (999, "Iconic")})  # the browser has no window at all
    assert real_state(ROUTE) is None

    desktop({"0xb00002": (4242, "Iconic")})
    monkeypatch.delenv("DISPLAY")  # no desktop to ask
    assert real_state(ROUTE) is None

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(native_window, "browser_pid", lambda port: None)  # not running
    assert real_state(ROUTE) is None


def test_a_minimized_browser_is_refused_before_any_page_is_opened(monkeypatch):
    """Measured: twenty minutes and the source, spent on pages nothing drew."""
    opened: list[str] = []
    monkeypatch.setattr(native_window, "native_window_state", lambda uc_kwargs: "minimized")
    monkeypatch.setattr(
        uc_browser, "pass_check_unattached",
        lambda endpoint, url, wait_s: opened.append(url) or (True, "t"),
    )

    with pytest.raises(NativeWindowNotDrawn, match="minimized"):
        uc_browser.prepare_native(ROUTE, "https://venue.example/", 5)
    assert opened == []

    monkeypatch.setattr(native_window, "native_window_state", lambda uc_kwargs: None)
    assert uc_browser.prepare_native(ROUTE, "https://venue.example/", 5) == (True, "t")


def test_the_browser_is_found_by_its_port_however_its_command_line_is_stored():
    """Chrome stores it space-joined; the first version of this split on NULs
    only and found no browser at all, so nothing would ever have been refused."""
    chrome = b"/opt/google/chrome/chrome --remote-debugging-port=34021 --user-data-dir=/p about:blank"
    assert native_window._is_browser_on_port(chrome, 34021)
    assert native_window._is_browser_on_port(chrome.replace(b" ", b"\0"), 34021)
    assert not native_window._is_browser_on_port(chrome, 3402)
    helper = chrome + b" --type=renderer"
    assert not native_window._is_browser_on_port(helper, 34021)
