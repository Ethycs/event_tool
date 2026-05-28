"""Backward-compatibility shim.

UCBrowser now lives in the `uc_browser` package, installed from
`ext/universal_controller/` as an editable pip dependency (see the
event_harvester `pyproject.toml`).

Every existing `from event_harvester.sources.uc_browser import UCBrowser`
call still works via this shim — no code changes needed in callers.

For new code, prefer importing from the canonical location:

    from uc_browser import UCBrowser, BrowserMode
"""

from uc_browser import BrowserMode, UCBrowser

__all__ = ["UCBrowser", "BrowserMode"]
