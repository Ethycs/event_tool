"""Interactive site walking, for authoring a route once.

The economics of the two halves of web scraping are opposite, so they get
opposite mechanisms:

* **Replay** happens every harvest, for every source. It must be deterministic,
  cheap and model-free — see :mod:`event_harvester.sources.web_steps`.
* **Authoring** happens once per site, when a human or an agent works out how
  the site actually reveals its events. It can afford a browser, screenshots
  and a vision model, because it is paid once and amortised over every run
  afterwards.

This module is the authoring half. It keeps a browser open across calls so an
agent can take one step, look at what happened, and decide the next — the loop
a person does by hand the first time they meet a site. Every successful step is
recorded, so the session's byproduct *is* the route.

Sessions hold a real browser process, so they are capped and reaped. A leaked
Chromium is not a hypothetical in this repo.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from event_harvester.observability.tracing import set_span_attrs, traced
from event_harvester.sources import web_steps
from event_harvester.utils import data_path

logger = logging.getLogger("event_harvester.web_explore")
_TRACER = "event_harvester.sources.web_explore"

# A browser per session is expensive and easy to forget about. Three is enough
# to compare two sites side by side without turning the box into a browser farm.
MAX_SESSIONS = 3
IDLE_TIMEOUT_S = 900

WEB_SOURCES_PATH = data_path("web_sources.json")


@dataclass
class ExploreSession:
    """One open browser, one page, and the route being written against it."""

    session_id: str
    url: str
    uc: Any
    page: Any
    steps: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()

    def close(self) -> None:
        for closer in (
            lambda: self.page.close(),
            lambda: self.uc.__exit__(None, None, None),
        ):
            try:
                closer()
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                logger.debug("explore: teardown step failed: %s", e)


_SESSIONS: dict[str, ExploreSession] = {}
_LOCK = threading.Lock()


def _reap_idle() -> list[str]:
    """Close sessions idle past :data:`IDLE_TIMEOUT_S`. Returns closed ids."""
    now = time.time()
    closed: list[str] = []
    for sid, session in list(_SESSIONS.items()):
        if now - session.last_used > IDLE_TIMEOUT_S:
            session.close()
            _SESSIONS.pop(sid, None)
            closed.append(sid)
    if closed:
        logger.info("explore: reaped %d idle session(s): %s", len(closed), ", ".join(closed))
    return closed


def _get(session_id: str) -> ExploreSession:
    session = _SESSIONS.get(session_id)
    if session is None:
        known = ", ".join(_SESSIONS) or "none"
        raise KeyError(f"no explore session {session_id!r} (open: {known})")
    session.touch()
    return session


def list_sessions() -> list[dict[str, Any]]:
    with _LOCK:
        _reap_idle()
        return [
            {
                "session_id": s.session_id,
                "url": s.url,
                "steps": len(s.steps),
                "age_s": int(time.time() - s.created_at),
                "idle_s": int(time.time() - s.last_used),
            }
            for s in _SESSIONS.values()
        ]


@traced("web.explore.open", tracer_name=_TRACER)
def open_session(url: str, *, native_chrome: bool = False, wait_ms: int = 2500) -> dict[str, Any]:
    """Open a browser on ``url`` and start recording a route.

    The opening ``goto`` is recorded as step 0, so a session that is saved
    without any further action still replays to the right page — which is
    exactly the erobay case, and the reason that case looked "crystallised".
    """
    from event_harvester.sources.uc_browser import UCBrowser

    with _LOCK:
        _reap_idle()
        if len(_SESSIONS) >= MAX_SESSIONS:
            raise RuntimeError(
                f"{len(_SESSIONS)} explore session(s) already open (max {MAX_SESSIONS}). "
                f"Close one first: {', '.join(_SESSIONS)}"
            )
        session_id = secrets.token_hex(3)

        uc = UCBrowser(native_chrome=native_chrome)
        uc.__enter__()
        try:
            page = uc.open(url, source_name=f"explore-{session_id}")
            if wait_ms:
                page.wait_for_timeout(wait_ms)
        except Exception:
            try:
                uc.__exit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.debug("explore: teardown after failed open: %s", e)
            raise

        session = ExploreSession(
            session_id=session_id, url=url, uc=uc, page=page,
            steps=[{"verb": "goto", "url": url}],
        )
        _SESSIONS[session_id] = session

    set_span_attrs(**{"explore.session": session_id, "explore.url": url})
    return {"session_id": session_id, "url": url, **describe(session_id)}


@traced("web.explore.describe", tracer_name=_TRACER)
def describe(session_id: str, *, use_vlm: bool | str = "auto") -> dict[str, Any]:
    """What is on this page, and what can be acted on right now.

    Returns the page diagnosis plus which of the L1 target roles currently
    resolve. That second half is the part an agent needs to choose a next step
    without guessing at selectors — it is the difference between "try clicking
    something" and "there is a search box and a next control".
    """
    from event_harvester.config import load_config

    session = _get(session_id)
    from event_harvester.observability.hints import diagnose

    cfg_llm = None
    if use_vlm is not False:
        try:
            cfg_llm = load_config().llm
        except Exception as e:  # noqa: BLE001
            logger.debug("explore: could not load llm config: %s", e)

    diag = diagnose(session.page, cfg_llm, use_vlm=use_vlm)

    available: dict[str, Any] = {}
    for role in web_steps.TARGET_ROLES:
        try:
            element = web_steps.resolve_target(session.uc, session.page, role)
        except Exception as e:  # noqa: BLE001
            logger.debug("explore: resolving %s failed: %s", role, e)
            element = None
        available[role] = None if element is None else {
            "selector": element.selector,
            "confidence": round(float(element.confidence), 3),
        }

    try:
        cards = len(web_steps.W.extract_links(session.page))
    except Exception:  # noqa: BLE001
        cards = 0

    return {
        "diagnosis": diag.to_dict(),
        "summary": diag.summary(),
        "targets": available,
        "event_links_visible": cards,
        "steps_so_far": list(session.steps),
    }


@traced("web.explore.act", tracer_name=_TRACER)
def act(session_id: str, step: dict[str, Any], *, record: bool = True,
        describe_after: bool = True) -> dict[str, Any]:
    """Run one step against the live page, and record it if it worked.

    Only successful steps are recorded. A route made of things that happened is
    the point; a route made of things that were attempted is a liability, since
    replay has no way to know which was which.
    """
    session = _get(session_id)

    errors = web_steps.validate_steps([step])
    if errors:
        return {"ok": False, "error": "; ".join(errors), "recorded": False}

    scratch = web_steps.ReplayResult(ok=True)
    t0 = time.monotonic()
    try:
        ok, detail = web_steps._run_one(session.uc, session.page, step, scratch)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {e}"
    elapsed = int((time.monotonic() - t0) * 1000)

    entry = {"step": dict(step), "ok": ok, "detail": detail, "elapsed_ms": elapsed}
    session.history.append(entry)
    recorded = False
    if ok and record:
        session.steps.append(dict(step))
        recorded = True

    result: dict[str, Any] = {
        "ok": ok, "detail": detail, "elapsed_ms": elapsed, "recorded": recorded,
        "links_found": len(scratch.links), "texts_found": len(scratch.texts),
    }
    if scratch.links:
        result["links_sample"] = scratch.links[:5]
    if describe_after:
        result.update(describe(session_id, use_vlm=False if ok else "auto"))
    return result


@traced("web.explore.undo", tracer_name=_TRACER)
def undo(session_id: str) -> dict[str, Any]:
    """Drop the last recorded step. The page is *not* rewound.

    Explicit about the asymmetry rather than pretending to a time machine: a
    step that changed the page cannot be un-changed by forgetting it. Re-open
    the session if the page state matters.
    """
    session = _get(session_id)
    if len(session.steps) <= 1:
        return {"ok": False, "error": "nothing to undo beyond the opening goto"}
    dropped = session.steps.pop()
    return {"ok": True, "dropped": dropped, "steps": list(session.steps),
            "note": "page state was not rewound"}


@traced("web.explore.crystallize", tracer_name=_TRACER)
def crystallize(session_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    """Turn the recorded walk into a `web_sources.json` entry. Does not save.

    The opening ``goto`` becomes the source's ``url``, and everything after it
    becomes ``steps`` — so a one-step walk crystallises into exactly the shape
    the schema already had, and a ten-step walk into the shape it never did.
    """
    session = _get(session_id)
    steps = list(session.steps)
    first = steps[0] if steps else {"verb": "goto", "url": session.url}
    rest = steps[1:]

    source: dict[str, Any] = {
        "url": first.get("url", session.url),
        "name": name,
        "mode": "steps" if rest else "raw",
    }
    if rest:
        source["steps"] = rest
    source.update(overrides)

    errors = web_steps.validate_steps(source.get("steps", []))
    return {"ok": not errors, "source": source, "errors": errors}


@traced("web.explore.save", tracer_name=_TRACER)
def save_source(session_id: str, name: str, *, replace: bool = False,
                **overrides: Any) -> dict[str, Any]:
    """Crystallise and append to `data/web_sources.json`.

    Refuses to overwrite an existing source unless ``replace=True``. The feed
    probe learned this lesson the expensive way: an `--apply` that replaced a
    working calendar source with a one-night feed was caught only by running it
    live. Adding a source should never be able to silently remove one.
    """
    from event_harvester.utils import load_json, save_json

    built = crystallize(session_id, name, **overrides)
    if not built["ok"]:
        return built

    sources = load_json(WEB_SOURCES_PATH, [])
    if not isinstance(sources, list):
        return {"ok": False, "error": f"{WEB_SOURCES_PATH} is not a list"}

    existing = next((i for i, s in enumerate(sources) if s.get("name") == name), None)
    if existing is not None and not replace:
        return {
            "ok": False,
            "error": f"source {name!r} already exists; pass replace=true to overwrite",
            "existing": sources[existing],
            "would_write": built["source"],
        }

    if existing is not None:
        previous = sources[existing]
        sources[existing] = built["source"]
    else:
        previous = None
        sources.append(built["source"])

    save_json(WEB_SOURCES_PATH, sources)
    logger.info("explore: %s source %r (%d total)",
                "replaced" if previous else "added", name, len(sources))
    return {"ok": True, "source": built["source"], "replaced": previous,
            "total_sources": len(sources), "path": str(WEB_SOURCES_PATH)}


@traced("web.explore.close", tracer_name=_TRACER)
def close_session(session_id: str) -> dict[str, Any]:
    with _LOCK:
        session = _SESSIONS.pop(session_id, None)
    if session is None:
        return {"ok": False, "error": f"no explore session {session_id!r}"}
    session.close()
    return {"ok": True, "session_id": session_id, "steps": list(session.steps)}


def close_all() -> int:
    """Close every open session. Returns how many were closed."""
    with _LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.close()
    return len(sessions)
