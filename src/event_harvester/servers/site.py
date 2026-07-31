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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from event_harvester.observability.metrics import count, record
from event_harvester.observability.tracing import get_tracer, set_span_attrs, traced
from event_harvester.utils import data_path

logger = logging.getLogger("event_harvester.site")
_TRACER = "event_harvester.servers.site"

SITE_DIR = data_path(".site")
SITE_HTML = SITE_DIR / "index.html"
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
    """Get events, either by running a harvest or from the MCP cache."""
    if harvest:
        from event_harvester.pipeline import Pipeline

        result = await Pipeline(cfg).run()
        return (
            list(result.events or []),
            dict(result.source_counts or {}),
            len(result.all_messages or []),
        )

    # Cache path: the same file the MCP tools read, so `--no-schedule` serves
    # whatever the last harvest produced from any entry point.
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
    )
    # The JSON is the same projection the page embeds, so a client reading it
    # cannot see a different set of events than a browser does.
    from datetime import date

    SITE_JSON.write_text(
        json.dumps(_prepare(prepared, date.today()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_app(cfg, *, interval_hours: float = DEFAULT_INTERVAL_HOURS,
              harvest_on_start: bool = True, schedule: bool = True):
    """Build the Starlette app that serves the site."""
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
    from starlette.routing import Route

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

    async def events_json(request):
        if not SITE_JSON.exists():
            return JSONResponse({"ok": False, "error": "not built yet"}, status_code=503)
        return FileResponse(SITE_JSON, media_type="application/json",
                            headers={"Cache-Control": "no-store"})

    async def healthz(request):
        payload = state.snapshot()
        payload["interval_hours"] = interval_hours
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

    app = Starlette(
        debug=False,
        routes=[
            Route("/", index),
            Route("/events.json", events_json),
            Route("/healthz", healthz),
            Route("/refresh", refresh_now, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.site = state
    return app


def run_site(cfg, *, host: str = "127.0.0.1", port: int = 8800,
             interval_hours: float = DEFAULT_INTERVAL_HOURS,
             harvest_on_start: bool = True, schedule: bool = True) -> int:
    """Serve the site until interrupted. Returns an exit code."""
    import uvicorn

    app = build_app(cfg, interval_hours=interval_hours,
                    harvest_on_start=harvest_on_start, schedule=schedule)
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "site: binding to %s — these events are personal and this server has "
            "no authentication. Put a reverse proxy with auth in front.", host,
        )
    logger.info("site: http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def site_path() -> Path:
    return SITE_HTML
