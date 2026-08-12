"""Serve the events site, and keep it fresh on a schedule.

Zero new dependencies: `mcp`/`fastmcp` already pull in Starlette and uvicorn,
so the server is built on what is installed rather than on a framework added
for the purpose.

The page itself is the same self-contained artifact
`outputs/html_report.py` produces. That is deliberate, and it is the reason
this module is small: the site is a **file**, and serving it is a detail. The
same bytes can be emailed, opened from disk on a plane, or put behind a CDN —
so a served page and a forwarded page can never drift apart, because they are
the same page.

What this adds over `--report`:

* a URL instead of an attachment,
* a background refresh so the URL is current rather than a snapshot,
* `/events.json` for anything that would rather have the data than the page.

**No authentication.** The events are personal, and this binds to loopback by
default for that reason. `--host 0.0.0.0` puts your calendar on the network for
anyone who can reach the port; put a reverse proxy with auth in front before
doing that.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from event_harvester.observability.metrics import count, record
from event_harvester.observability.tracing import get_tracer, set_span_attrs, traced
from event_harvester.utils import REPO_ROOT, data_path

logger = logging.getLogger("event_harvester.site")
_TRACER = "event_harvester.servers.site"

SITE_DIR = data_path(".site")
SITE_HTML = SITE_DIR / "index.html"
SITE_CALENDAR = SITE_DIR / "calendar.html"
SITE_JSON = SITE_DIR / "events.json"

DEFAULT_INTERVAL_HOURS = 24


class SiteState:
    """What the server knows about the page it is serving.

    Held in one object rather than module globals so the refresh task and the
    request handlers cannot disagree about whether a build is in flight.
    """

    def __init__(self) -> None:
        self.last_refresh: float | None = None
        self.last_error: str | None = None
        self.event_count: int = 0
        self.refreshing: bool = False
        self.refresh_count: int = 0
        # Serialises refreshes. A harvest opens browsers and calls an LLM;
        # two at once would double the cost and race on the same output file.
        self.lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        age = None if self.last_refresh is None else int(time.time() - self.last_refresh)
        return {
            "ok": self.last_error is None,
            "events": self.event_count,
            "last_refresh_epoch": self.last_refresh,
            "age_seconds": age,
            "refreshing": self.refreshing,
            "refreshes": self.refresh_count,
            "error": self.last_error,
            "html": str(SITE_HTML),
        }


@traced("site.refresh", tracer_name=_TRACER)
async def refresh(state: SiteState, cfg, *, harvest: bool = True) -> dict[str, Any]:
    """Rebuild the page. With `harvest=False`, rebuild from the cached events.

    Returns the state snapshot. Never raises: a failed refresh must leave the
    previously-good page being served rather than take the site down.
    """
    if state.lock.locked():
        logger.info("site: refresh already in progress, skipping")
        count("site.refresh.skipped", reason="in_flight",
              description="Refreshes not run")
        return state.snapshot()

    async with state.lock:
        state.refreshing = True
        started = time.monotonic()
        try:
            events, source_counts, total_messages = await _collect(cfg, harvest=harvest)
            _write(events, source_counts, total_messages)
            state.event_count = len(events)
            state.last_refresh = time.time()
            state.last_error = None
            state.refresh_count += 1
            elapsed = int((time.monotonic() - started) * 1000)
            logger.info("site: refreshed — %d event(s) in %dms", len(events), elapsed)
            record("site.refresh.duration", elapsed, unit="ms",
                   description="Time to rebuild the events site")
            count("site.refresh", ok=True, harvested=harvest,
                  description="Site refreshes")
        except Exception as e:  # noqa: BLE001 — the site must survive a bad harvest
            state.last_error = f"{type(e).__name__}: {e}"
            logger.exception("site: refresh failed")
            count("site.refresh", ok=False, harvested=harvest,
                  description="Site refreshes")
        finally:
            state.refreshing = False

    set_span_attrs(**{"site.events": state.event_count, "site.ok": state.last_error is None})
    return state.snapshot()


async def _collect(cfg, *, harvest: bool) -> tuple[list[dict], dict[str, int], int]:
    """Get events, either by asking for a harvest or from the shared cache.

    **The site does not run the pipeline.** It submits a job and waits, and
    the MCP backend's single runner executes it. That is what stops the two
    services harvesting simultaneously — not the queue itself, but the fact
    that only one process owns execution.

    Either way the events are read from the same cache file, so the served
    page and the MCP tools cannot disagree about what the last harvest found.
    """
    if harvest:
        await _submit_and_wait(cfg)
    return _read_cached_events()


async def _submit_and_wait(cfg) -> None:
    """Queue a harvest and wait for it. Raises so `refresh` records the error."""
    from event_harvester import jobs
    from event_harvester.job_executor import build_request
    from event_harvester.job_runner import wait_for

    request = build_request(days=cfg.days_back)
    job = jobs.submit(request, submitted_by="timer")
    logger.info("site: queued harvest %s (coalesced=%d)", job.id, job.coalesced)

    finished = await wait_for(job.id)
    if finished is None:
        raise RuntimeError(f"job {job.id} vanished from the queue")
    if finished.state == jobs.STATE_QUEUED:
        # Nothing drained it. Almost always: the MCP backend is not running.
        raise RuntimeError(
            f"job {job.id} was never picked up — is EventHarvesterMCP running? "
            "The site does not harvest on its own by design."
        )
    if finished.state != jobs.STATE_DONE:
        raise RuntimeError(
            f"job {job.id} {finished.state}: "
            f"{(finished.result or {}).get('error') or 'no detail'}"
        )


def _read_cached_events() -> tuple[list[dict], dict[str, int], int]:
    """Read the shared event cache — the same file the MCP tools read.

    One store, so the page and the tools cannot drift. (There were three of
    these once; `data/.server_events.json` had been dead since March.)
    """
    from event_harvester.servers.mcp_server import LATEST_EVENTS_FILE
    from event_harvester.utils import load_json

    data = load_json(LATEST_EVENTS_FILE, {}) or {}
    return (
        list(data.get("events", [])),
        dict(data.get("source_counts", {})),
        int(data.get("stats", {}).get("total_messages", 0)),
    )


def _write(events: list[dict], source_counts: dict[str, int], total_messages: int) -> None:
    import json

    from event_harvester.outputs.calendar_html import generate_calendar_html
    from event_harvester.outputs.html_report import _prepare, generate_html_report
    from event_harvester.servers.mcp_server import _events_for_report

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    prepared = _events_for_report(events) if events else []
    generate_html_report(
        validated_events=prepared,
        source_counts=source_counts,
        total_messages=total_messages,
        output_path=str(SITE_HTML),
        title="Events",
        # The site is the one caller that should pay for pictures: it rebuilds
        # on a schedule on a machine with a network, and thumbnails are cached
        # on disk by URL so a rebuild re-costs only the newly-seen links.
        thumbnails=os.environ.get("SITE_THUMBNAILS", "1").lower()
        not in ("0", "false", "no"),
        # Only the served copy gets cross-page links; a forwarded file has no
        # origin to resolve /calendar or /admin against. See `outputs/nav.py`.
        nav=True,
    )
    # Both pages are written in the same pass, from the same list, so /calendar
    # can never show a different harvest than /. A calendar built lazily on
    # first request would have been one more thing that can be stale.
    generate_calendar_html(
        validated_events=prepared,
        output_path=str(SITE_CALENDAR),
        title="Calendar",
        nav=True,
    )
    # The JSON is the same projection the page embeds, so a client reading it
    # cannot see a different set of events than a browser does.
    from datetime import date

    SITE_JSON.write_text(
        json.dumps(_prepare(prepared, date.today()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalise_prefix(prefix: str) -> str:
    """`harvester`, `/harvester/`, `//harvester` → `/harvester`. Empty stays empty.

    Every spelling an operator might reasonably type or paste out of an nginx
    config maps to the one form `Mount` accepts — it requires a leading slash
    and rejects a trailing one.
    """
    p = (prefix or "").strip().strip("/")
    return f"/{p}" if p else ""


def build_app(cfg, *, interval_hours: float = DEFAULT_INTERVAL_HOURS,
              harvest_on_start: bool = True, schedule: bool = True,
              root_path: str = ""):
    """Build the Starlette app that serves the site.

    ``root_path`` additionally mounts every route under a prefix, for the case
    where a reverse proxy forwards `/harvester/calendar` **unchanged** rather
    than stripping the prefix first. It is not needed for a stripping proxy,
    and it is not how the pages find each other — they use relative URLs and
    work under any mount depth without being told about it (`outputs/nav.py`).
    The unprefixed routes stay registered either way, so `:8800/calendar`
    keeps working while the proxy path does too.
    """
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route

    state = SiteState()
    tracer = get_tracer(_TRACER)

    async def index(request):
        with tracer.start_as_current_span("site.GET./") as span:
            if not SITE_HTML.exists():
                span.set_attribute("site.built", False)
                return PlainTextResponse(
                    "The site has not been built yet. It builds on startup and every "
                    f"{interval_hours}h; check /healthz.",
                    status_code=503,
                )
            span.set_attribute("site.events", state.event_count)
            # no-store: the whole point is that the URL is current. A cached
            # copy of yesterday's events is worse than a slow load.
            return FileResponse(SITE_HTML, media_type="text/html; charset=utf-8",
                                headers={"Cache-Control": "no-store"})

    async def calendar_page(request):
        with tracer.start_as_current_span("site.GET./calendar"):
            if not SITE_CALENDAR.exists():
                return PlainTextResponse(
                    "The calendar has not been built yet. It is written with the "
                    "events page on every refresh; check /healthz.",
                    status_code=503,
                )
            return FileResponse(SITE_CALENDAR, media_type="text/html; charset=utf-8",
                                headers={"Cache-Control": "no-store"})

    async def events_json(request):
        if not SITE_JSON.exists():
            return JSONResponse({"ok": False, "error": "not built yet"}, status_code=503)
        return FileResponse(SITE_JSON, media_type="application/json",
                            headers={"Cache-Control": "no-store"})

    async def healthz(request):
        payload = state.snapshot()
        payload["interval_hours"] = interval_hours
        # Identity, so "is this service running current code?" is a field
        # rather than an investigation. See buildinfo.py.
        try:
            from event_harvester.buildinfo import build_info

            payload["build"] = build_info()
        except Exception as e:  # noqa: BLE001 - health must never 500
            payload["build"] = {"error": f"{type(e).__name__}: {e}"}
        # The site no longer runs harvests, so "is anything happening" is a
        # question about the queue, not about this process.
        try:
            from event_harvester import jobs

            payload["queue"] = jobs.queue_summary()
        except Exception as e:  # noqa: BLE001 - health must never 500
            payload["queue"] = {"error": f"{type(e).__name__}: {e}"}
        return JSONResponse(payload, status_code=200 if payload["ok"] else 503)

    async def refresh_now(request):
        """Manual rebuild. POST-only so a link preview cannot trigger a harvest."""
        do_harvest = request.query_params.get("harvest", "1") not in ("0", "false", "no")
        return JSONResponse(await refresh(state, cfg, harvest=do_harvest))

    async def _loop():
        """Rebuild every `interval_hours`, forever."""
        seconds = max(60.0, float(interval_hours) * 3600.0)
        while True:
            await asyncio.sleep(seconds)
            logger.info("site: scheduled refresh")
            await refresh(state, cfg, harvest=True)

    @asynccontextmanager
    async def lifespan(_app):
        # Build from cache first so the URL answers immediately, then harvest.
        # A cold start that waits for a full harvest serves 503 for minutes.
        await refresh(state, cfg, harvest=False)
        tasks: list[asyncio.Task] = []
        if harvest_on_start:
            tasks.append(asyncio.create_task(refresh(state, cfg, harvest=True)))
        if schedule:
            tasks.append(asyncio.create_task(_loop()))
            logger.info("site: scheduled refresh every %sh", interval_hours)
        try:
            yield
        finally:
            # Without this, Ctrl-C leaves the refresh loop pending and uvicorn
            # waits on a task that never returns.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # /admin lives on this origin by deliberate choice — `01 - Design/02 - The
    # Control UI.md` §2, "one origin, split routes". It writes config, so its
    # handlers validate against closed vocabularies and write atomically; see
    # that module's docstring for what this origin still lacks.
    from event_harvester.servers.admin import build_routes as admin_routes

    routes = [
        Route("/", index),
        Route("/calendar", calendar_page),
        Route("/events.json", events_json),
        Route("/healthz", healthz),
        Route("/refresh", refresh_now, methods=["POST"]),
        *admin_routes(),
    ]
    prefix = normalise_prefix(root_path)
    if prefix:
        # Mounted *first* so the prefixed path wins, and the bare routes kept
        # after it so direct access on the port is unaffected. `Mount` also
        # redirects `/harvester` to `/harvester/`, which is the one case
        # relative URLs alone cannot resolve.
        routes = [Mount(prefix, routes=list(routes)), *routes]
        logger.info("site: also serving under %s", prefix)

    app = Starlette(debug=False, routes=routes, lifespan=lifespan)
    app.state.site = state
    return app


def make_app():
    """App factory for `uvicorn --factory`, used by `--reload`.

    uvicorn's reloader restarts the process on every file change, so it needs
    an import string rather than an app object. Config comes through the
    environment because the reloaded child does not inherit our arguments.

    **Harvesting and scheduling default to off here.** With `--reload` a saved
    file restarts the server, and a harvest per keystroke-save would open
    browsers and spend tokens every few seconds. Reload mode serves the cached
    events; set `EH_SITE_HARVEST_ON_START=1` if you really mean it.
    """
    from event_harvester.config import load_config

    def _flag(name: str) -> bool:
        return os.environ.get(name, "").lower() in ("1", "true", "yes")

    return build_app(
        load_config(),
        interval_hours=float(os.environ.get("EH_SITE_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS)),
        harvest_on_start=_flag("EH_SITE_HARVEST_ON_START"),
        schedule=_flag("EH_SITE_SCHEDULE"),
        root_path=os.environ.get("EH_SITE_ROOT_PATH", ""),
    )


def run_site_reload(*, host: str = "127.0.0.1", port: int = 8800,
                    interval_hours: float = DEFAULT_INTERVAL_HOURS,
                    harvest_on_start: bool = False, schedule: bool = False,
                    root_path: str = "") -> int:
    """Serve with uvicorn's auto-reloader, in a subprocess.

    A subprocess rather than in-process because uvicorn implements reload by
    supervising *child* processes, which cannot be driven from inside the CLI's
    already-running event loop. Handing the whole thing to `python -m uvicorn`
    keeps the real reloader and sidesteps the loop entirely.
    """
    import subprocess
    import sys

    env = dict(os.environ)
    env["EH_SITE_INTERVAL_HOURS"] = str(interval_hours)
    env["EH_SITE_HARVEST_ON_START"] = "1" if harvest_on_start else "0"
    env["EH_SITE_SCHEDULE"] = "1" if schedule else "0"
    env["EH_SITE_ROOT_PATH"] = root_path

    cmd = [
        sys.executable, "-m", "uvicorn",
        "event_harvester.servers.site:make_app", "--factory",
        "--reload",
        "--reload-dir", str(REPO_ROOT / "src"),
        "--host", host, "--port", str(port),
        "--log-level", "warning",
    ]
    logger.info("site: reload mode — http://%s:%d", host, port)
    try:
        return subprocess.call(cmd, env=env, cwd=str(REPO_ROOT))
    except KeyboardInterrupt:
        return 130


async def run_site(cfg, *, host: str = "127.0.0.1", port: int = 8800,
                   interval_hours: float = DEFAULT_INTERVAL_HOURS,
                   harvest_on_start: bool = True, schedule: bool = True,
                   root_path: str = "") -> int:
    """Serve the site until interrupted. Returns an exit code.

    A coroutine, not a sync function, and `uvicorn.Server.serve()` rather than
    `uvicorn.run()`. The latter calls `asyncio.run()` internally, and the CLI
    dispatcher already runs every command inside a loop — so `uvicorn.run()`
    raised *"asyncio.run() cannot be called from a running event loop"* and the
    process exited immediately. Under NSSM that surfaced as `SERVICE_PAUSED`
    with nothing listening; from a terminal it would have been an instant
    traceback. It was invisible to the tests because they exercised the app
    object through `TestClient` and never the entry point.
    """
    import uvicorn

    app = build_app(cfg, interval_hours=interval_hours,
                    harvest_on_start=harvest_on_start, schedule=schedule,
                    root_path=root_path)
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "site: binding to %s — these events are personal and this server has "
            "no authentication. Put a reverse proxy with auth in front.", host,
        )
    logger.info("site: http://%s:%d", host, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    await server.serve()
    return 0


def site_path() -> Path:
    return SITE_HTML
