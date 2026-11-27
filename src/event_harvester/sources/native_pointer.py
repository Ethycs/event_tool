"""A pointer click in the native browser's window, made through the X server.

Some bot checks end in a box to click. **Measured 2026-11-27 on ``erobay``'s
check**, in Chrome with nothing attached:

* a click made by a script inside the page, on the box itself, from within the
  check's own frame, was rejected three times in three and restarted the check;
* a click from a hardware pointer (the KVM) passed in 2.0 s;
* an X test event (``XTestFakeButtonEvent``) from a program on this host
  passed in 2.4 s.

So the click cannot be made by anything in the page, and can be made from
here. :func:`click` moves the pointer to a point given as an offset from the
top-left of the browser's window and presses once.

It is aimed by position, so it is refused, with the reason, unless all of
these hold at the moment of the press:

* the browser has exactly one window on screen (with two there is no telling
  which holds the page);
* that window's title still shows the page the caller saw
  (``expect_title``), so a page that has moved on, or another tab in front,
  is not clicked;
* the point is inside the window;
* the window under the pointer at that point is that window, so the press
  cannot land on whatever lies over it.

Nothing is mapped, raised or focused to make it possible: the desktop is the
owner's, and a covered window is reported, not uncovered.

The X work runs in a child process. Xlib ends the process it is in on a lost
connection, and this module is used inside a service that must outlive that.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import subprocess
import sys
import time

#: Seconds the child process may take before it is given up on.
_CHILD_TIMEOUT_S = 20


class ClickRefused(RuntimeError):
    """The click was not made. The message is the reason."""


def click(pid: int, offset: tuple[int, int], expect_title: str) -> tuple[int, int]:
    """Click once at ``offset`` from the top-left of ``pid``'s window.

    Returns the point on the screen that was pressed. Raises
    :class:`ClickRefused` when the press was not made.
    """
    done = subprocess.run(  # noqa: S603 — our own module, fixed arguments
        [sys.executable, "-m", __name__, str(pid), str(offset[0]), str(offset[1]), expect_title],
        capture_output=True, text=True, timeout=_CHILD_TIMEOUT_S, check=False,
    )
    try:
        result = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ClickRefused(
            f"the click helper gave no result (exit {done.returncode}): "
            f"{done.stderr.strip()[-200:]}"
        ) from None
    if not result.get("clicked"):
        raise ClickRefused(str(result.get("reason") or "no reason given"))
    x, y = result["point"]
    return int(x), int(y)


# ── child process ────────────────────────────────────────────────────────────


class _X:
    """The few Xlib and XTEST calls the click needs, on one display."""

    def __init__(self):
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
        ptr, win, long_, int_, uint = (
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long, ctypes.c_int, ctypes.c_uint,
        )
        x11.XOpenDisplay.restype = ptr
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XDefaultRootWindow.restype = win
        x11.XDefaultRootWindow.argtypes = [ptr]
        x11.XInternAtom.restype = win
        x11.XInternAtom.argtypes = [ptr, ctypes.c_char_p, int_]
        x11.XGetWindowProperty.argtypes = [ptr, win, win, long_, long_, int_, win] + [ptr] * 5
        x11.XGetGeometry.argtypes = [ptr, win] + [ptr] * 7
        x11.XTranslateCoordinates.argtypes = [ptr, win, win, int_, int_] + [ptr] * 3
        x11.XQueryTree.argtypes = [ptr, win] + [ptr] * 4
        x11.XQueryPointer.argtypes = [ptr, win] + [ptr] * 7
        x11.XFree.argtypes = [ptr]
        x11.XSync.argtypes = [ptr, int_]
        x11.XCloseDisplay.argtypes = [ptr]
        xtst.XTestFakeMotionEvent.argtypes = [ptr, int_, int_, int_, win]
        xtst.XTestFakeButtonEvent.argtypes = [ptr, uint, int_, win]
        # An X error (a window that went away mid-question) must be an answer,
        # not the end of the process. The handler is kept so it is not collected.
        self._on_error = ctypes.CFUNCTYPE(int_, ptr, ptr)(lambda _display, _event: 0)
        x11.XSetErrorHandler(self._on_error)
        self.x11, self.xtst = x11, xtst
        self.display = x11.XOpenDisplay(None)
        if not self.display:
            raise ClickRefused("no X display to click on")
        self.root = x11.XDefaultRootWindow(self.display)

    def atom(self, name: str) -> int:
        return self.x11.XInternAtom(self.display, name.encode(), 0)

    def _property(self, window: int, name: str, pointer_type, length: int = 4096):
        kind, fmt = ctypes.c_ulong(), ctypes.c_int()
        count, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = pointer_type()
        self.x11.XGetWindowProperty(
            self.display, window, self.atom(name), 0, length, 0, 0, ctypes.byref(kind),
            ctypes.byref(fmt), ctypes.byref(count), ctypes.byref(after), ctypes.byref(data),
        )
        return data, count.value

    def longs(self, window: int, name: str) -> list[int]:
        data, count = self._property(window, name, ctypes.POINTER(ctypes.c_ulong))
        values = [data[i] for i in range(count)] if data else []
        if data:
            self.x11.XFree(data)
        return values

    def text(self, window: int, name: str) -> str:
        data, _count = self._property(window, name, ctypes.c_char_p)
        value = (data.value or b"").decode("utf-8", "replace")
        if data:
            self.x11.XFree(data)
        return value

    def rect(self, window: int) -> tuple[int, int, int, int]:
        """``(x, y, width, height)`` of ``window`` on the screen."""
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        x, y, sx, sy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        w, h, border, depth = (ctypes.c_uint() for _ in range(4))
        self.x11.XGetGeometry(
            self.display, window, ctypes.byref(root), ctypes.byref(x), ctypes.byref(y),
            ctypes.byref(w), ctypes.byref(h), ctypes.byref(border), ctypes.byref(depth),
        )
        self.x11.XTranslateCoordinates(
            self.display, window, self.root, 0, 0, ctypes.byref(sx), ctypes.byref(sy),
            ctypes.byref(child),
        )
        return sx.value, sy.value, w.value, h.value

    def ancestors(self, window: int) -> list[int]:
        """``window`` and each parent up to, not including, the root."""
        chain = [window]
        while True:
            root, parent = ctypes.c_ulong(), ctypes.c_ulong()
            children, count = ctypes.c_void_p(), ctypes.c_uint()
            ok = self.x11.XQueryTree(
                self.display, chain[-1], ctypes.byref(root), ctypes.byref(parent),
                ctypes.byref(children), ctypes.byref(count),
            )
            if children:
                self.x11.XFree(children)
            if not ok or not parent.value or parent.value == self.root:
                return chain
            chain.append(parent.value)

    def under_pointer(self) -> int:
        """The root's child the pointer is over: a top-level window or its frame."""
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        ints = [ctypes.c_int() for _ in range(4)]
        mask = ctypes.c_uint()
        self.x11.XQueryPointer(
            self.display, self.root, ctypes.byref(root), ctypes.byref(child),
            *(ctypes.byref(i) for i in ints), ctypes.byref(mask),
        )
        return child.value

    def move(self, x: int, y: int) -> None:
        self.xtst.XTestFakeMotionEvent(self.display, -1, x, y, 0)
        self.x11.XSync(self.display, 0)

    def button(self, down: bool) -> None:
        self.xtst.XTestFakeButtonEvent(self.display, 1, int(down), 0)
        self.x11.XSync(self.display, 0)

    def close(self) -> None:
        self.x11.XCloseDisplay(self.display)


def _visible_windows(x: _X, pid: int) -> list[int]:
    hidden = x.atom("_NET_WM_STATE_HIDDEN")
    return [
        w for w in x.longs(x.root, "_NET_CLIENT_LIST")
        if pid in x.longs(w, "_NET_WM_PID") and hidden not in x.longs(w, "_NET_WM_STATE")
    ]


def _owner(x: _X, top: int) -> str:
    """Who the top-level window (or frame) ``top`` belongs to, for a refusal."""
    for window in x.longs(x.root, "_NET_CLIENT_LIST"):
        if top in x.ancestors(window):
            pid = (x.longs(window, "_NET_WM_PID") or [0])[0]
            return f"{x.text(window, '_NET_WM_NAME')[:50]!r} (pid {pid})"
    return f"window {top:#x}"


def _click(pid: int, offset: tuple[int, int], expect_title: str) -> tuple[int, int]:
    x = _X()
    try:
        windows = _visible_windows(x, pid)
        if len(windows) != 1:
            raise ClickRefused(
                f"the browser has {len(windows)} windows on screen, so which one holds "
                "the page cannot be told"
            )
        window = windows[0]

        def require_title() -> None:
            title = x.text(window, "_NET_WM_NAME")
            if expect_title not in title:
                raise ClickRefused(
                    f"the browser's window shows {title[:60]!r}, not {expect_title[:40]!r}"
                )

        require_title()
        left, top, width, height = x.rect(window)
        if not (0 <= offset[0] < width and 0 <= offset[1] < height):
            raise ClickRefused(
                f"the point {offset[0]},{offset[1]} is outside the {width}x{height} window"
            )
        px, py = left + offset[0], top + offset[1]
        # A short glide to the point, then the press: the sequence that was measured.
        for step in range(1, 9):
            x.move(px - 80 + 10 * step, py - 40 + 5 * step)
            time.sleep(0.03)
        time.sleep(0.15)
        over = x.under_pointer()
        if over not in x.ancestors(window):
            raise ClickRefused(
                f"another window lies over the point {px},{py}: {_owner(x, over)}; "
                "it is not moved from here"
            )
        require_title()
        x.button(True)
        time.sleep(0.08)
        x.button(False)
        return px, py
    finally:
        x.close()


def _main(argv: list[str]) -> int:
    try:
        pid, ox, oy, expect_title = int(argv[0]), int(argv[1]), int(argv[2]), argv[3]
        px, py = _click(pid, (ox, oy), expect_title)
        result = {"clicked": True, "point": [px, py]}
    except ClickRefused as e:
        result = {"clicked": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001 — the parent reads a reason, not a traceback
        result = {"clicked": False, "reason": f"{type(e).__name__}: {e}"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
