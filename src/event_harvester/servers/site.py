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
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# buildinfo records the commit and the time at import, which must be process
# start, so it is imported here and not in the health handler.
from event_harvester.buildinfo import build_info
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

# The browser-facing side of `/refresh`. GET returns this page (a crawler
# fetching it triggers nothing); the buttons POST, and the page then polls
# `/healthz` so a multi-minute harvest never rides on one long HTTP request
# that a proxy would time out. Relative URLs (`refresh`, `healthz`, `.`) so it
# works identically on the bare port and under a `--root-path` mount.
_REFRESH_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Refresh — Event Harvester</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, -apple-system, sans-serif; max-width: 42rem;
         margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; }
  button { font: inherit; padding: .55rem 1rem; margin: .25rem .5rem .25rem 0;
           border-radius: .4rem; border: 1px solid #8888; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  #status { margin-top: 1rem; padding: .75rem 1rem; border-radius: .4rem;
            background: #8881; white-space: pre-wrap; font-variant-numeric: tabular-nums; }
  #status.err { color: #c0392b; }
  a { color: inherit; }
  .muted { opacity: .7; font-size: .9em; }
</style>
<h1>Event site refresh</h1>
<p class="muted">A full refresh runs a harvest (browsers + LLM) on the server, so
it takes a few minutes. This page fires it and watches progress &mdash; you can
close the tab; the harvest keeps running.</p>
<button id="full">Refresh now (full harvest)</button>
<button id="cache">Rebuild from cache (fast)</button>
<div id="status">Loading current status&hellip;</div>
<p class="muted"><a href=".">&larr; back to events</a></p>
<script>
(function () {
  const statusEl = document.getElementById('status');
  const full = document.getElementById('full');
  const cache = document.getElementById('cache');
  let polling = false, start = 0;

  function render(s, note) {
    const lines = [];
    if (note) lines.push(note);
    lines.push('events served: ' + (s.events != null ? s.events : '?'));
    lines.push('refreshing: ' + (s.refreshing ? 'yes' : 'no'));
    if (s.age_seconds != null) lines.push('last refresh: ' + s.age_seconds + 's ago');
    if (s.refreshes != null) lines.push('refreshes this run: ' + s.refreshes);
    if (s.error) lines.push('error: ' + s.error);
    statusEl.classList.toggle('err', !!s.error);
    statusEl.textContent = lines.join('\\n');
  }

  const health = () => fetch('healthz', { cache: 'no-store' }).then(r => r.json());

  async function poll() {
    if (polling) return;
    polling = true;
    let sawRefreshing = false;
    try {
      for (;;) {
        const s = await health();
        if (s.refreshing) sawRefreshing = true;
        render(s, s.refreshing ? '\\u23f3 harvesting\\u2026'
                               : (sawRefreshing ? '\\u2705 done' : 'idle'));
        if (sawRefreshing && !s.refreshing) break;
        // finished fast, or never started:
        if (!sawRefreshing && (Date.now() - start) > 15000) break;
        await new Promise(r => setTimeout(r, 3000));
      }
    } finally {
      polling = false;
      full.disabled = cache.disabled = false;
    }
  }

  async function trigger(harvest) {
    full.disabled = cache.disabled = true;
    start = Date.now();
    try {
      const r = await fetch('refresh' + (harvest ? '' : '?harvest=0'), { method: 'POST' });
      const s = await r.json();
      render(s, s.already_refreshing ? 'a refresh is already running \\u2014 watching it'
                                     : 'started\\u2026');
      poll();
    } catch (e) {
      statusEl.classList.add('err');
      statusEl.textContent = 'could not start: ' + e;
      full.disabled = cache.disabled = false;
    }
  }

  full.addEventListener('click', () => trigger(true));
  cache.addEventListener('click', () => trigger(false));
  health().then(s => render(s, null))
          .catch(e => { statusEl.textContent = 'status unavailable: ' + e; });
})();
</script>
"""


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


# Fields whose absence makes an event a data-quality problem worth surfacing on
# the debug endpoint. A calendar event with no date or no link is broken; one
# with no time or location is thin. See `debug_incomplete`.
_KEY_FIELDS = ("date", "time", "location", "link")
_BLANK_VALUES = frozenset({"", "tbd", "none", "null"})


def _is_blank(value: Any) -> bool:
    """Empty for debug purposes: None, or a string that is empty/TBD/None/null."""
    return value is None or str(value).strip().lower() in _BLANK_VALUES


def _incomplete_events(events: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Events missing one or more key fields, plus per-field missing counts.

    Returns ``(rows, counts)``. Each row carries the event's ``index`` (its
    position in the cache, stable until the next harvest), its identifying
    fields, and the ``missing`` list — so 'why is this event blank?' is one
    request, not a cache dump.
    """
    counts = {field: 0 for field in _KEY_FIELDS}
    rows: list[dict] = []
    for index, event in enumerate(events):
        missing = [f for f in _KEY_FIELDS if _is_blank(event.get(f))]
        if not missing:
            continue
        for field in missing:
            counts[field] += 1
        rows.append({
            "index": index,
            "title": event.get("title"),
            "source": event.get("source"),
            "date": event.get("date"),
            "time": event.get("time"),
            "location": event.get("location"),
            "link": event.get("link"),
            "missing": missing,
        })
    return rows, counts


# Messaging platforms we always report on the empty-sources view, so a platform
# that has gone dark (0 events) shows up even when nothing references it.
_KNOWN_PLATFORMS = ("discord", "telegram", "signal", "gmail")
# Aggregator hosts that front many distinct sources — a link on one of these
# identifies the platform, not which configured source it came from, so they are
# not used to attribute an event to a source by host.
_GENERIC_HOSTS = frozenset({"calendar.google.com", "google.com"})


def _host(url: Any) -> str:
    """Bare registrable-ish host of a URL (``www.`` stripped), or ""."""
    from urllib.parse import urlparse

    try:
        return urlparse(str(url or "")).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _empty_sources(
    events: list[dict], web_sources: list[dict]
) -> tuple[list[dict], dict[str, int], int]:
    """Tally events per configured source so dead sources are visible.

    Returns ``(web_rows, platform_counts, unattributed)``:

    - ``web_rows`` — one row per configured web source with the number of events
      attributed to it, zero-count sources first. A web event is attributed by,
      in order: an ``ics:<name>:``/``web:<name>:`` source-id prefix, its link
      host matching a source's link/site/url host, then the source name
      appearing in its link or ``source`` text.
    - ``platform_counts`` — events per messaging platform, including the known
      platforms at 0 so a silent one is obvious.
    - ``unattributed`` — web events no configured source claimed (honest: link
      attribution is best-effort, so a nonzero here means "look closer").
    """
    from collections import Counter

    counts = {s.get("name", ""): 0 for s in web_sources if s.get("name")}
    modes = {s.get("name", ""): s.get("mode") for s in web_sources if s.get("name")}
    host_index: dict[str, str] = {}
    for s in web_sources:
        name = s.get("name")
        if not name:
            continue
        for key in ("link", "site", "url"):
            h = _host(s.get(key))
            if h and h not in _GENERIC_HOSTS:
                host_index.setdefault(h, name)

    unattributed = 0
    platform_counts: Counter = Counter({p: 0 for p in _KNOWN_PLATFORMS})
    for e in events:
        platforms = e.get("_platforms") or []
        for p in platforms:
            if p != "web":
                platform_counts[p] += 1
        if "web" not in platforms:
            continue
        name = None
        for sid in e.get("_source_ids") or []:
            parts = str(sid).split(":")
            if len(parts) >= 2 and parts[0] in ("ics", "web") and parts[1] in counts:
                name = parts[1]
                break
        if name is None:
            name = host_index.get(_host(e.get("link")))
        if name is None:
            hay = f"{e.get('link') or ''} {e.get('source') or ''}".lower()
            for cand in counts:
                if cand.lower() in hay:
                    name = cand
                    break
        if name in counts:
            counts[name] += 1
        else:
            unattributed += 1

    web_rows = [
        {"name": n, "mode": modes.get(n), "events": counts[n]} for n in counts
    ]
    web_rows.sort(key=lambda r: (r["events"], r["name"]))
    return web_rows, dict(platform_counts), unattributed


def _cooldown_state(web_sources: list[dict]) -> dict[str, dict]:
    """Per-source cooldown status, so the empty view distinguishes *skipped*
    from *fetched-but-empty*.

    A run skips a source when it opted into cooldown (`cooldown_hours >= 0`) and
    was fetched within that window; a source with no `cooldown_hours` is never
    skipped. Returns ``{name: {last_fetch_hours, cooling, cooldown_hours}}`` —
    ``cooling`` meaning a run right now would skip it.
    """
    from datetime import datetime, timezone

    from event_harvester.sources.web_utils import _load_cooldowns

    cds = _load_cooldowns()
    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for s in web_sources:
        name = s.get("name")
        if not name:
            continue
        last = cds.get(s.get("url"))
        hrs = None
        if last:
            try:
                hrs = round((now - datetime.fromisoformat(last)).total_seconds() / 3600, 2)
            except Exception:
                hrs = None
        try:
            cd_hours = float(s.get("cooldown_hours", -1))
        except (TypeError, ValueError):
            cd_hours = -1.0
        cooling = cd_hours >= 0 and hrs is not None and hrs < cd_hours
        out[name] = {"last_fetch_hours": hrs, "cooling": cooling, "cooldown_hours": cd_hours}
    return out


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
    from starlette.responses import (
        FileResponse,
        HTMLResponse,
        JSONResponse,
        PlainTextResponse,
    )
    from starlette.routing import Mount, Route

    state = SiteState()
    tracer = get_tracer(_TRACER)
    # Hold a strong ref to each in-flight fire-and-forget refresh, or the event
    # loop may garbage-collect the task mid-harvest (asyncio only keeps weak
    # refs). The done-callback drops it once finished.
    refresh_tasks: set[asyncio.Task] = set()

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
        """`/refresh`: GET renders a control page, POST triggers a rebuild.

        A GET has no side effect (so a link preview or crawler still cannot
        start a harvest — the original reason this was POST-only); it just
        serves buttons that POST. The POST is **fire-and-forget**: it kicks the
        refresh onto a background task and returns immediately, because a full
        harvest takes minutes and a proxy would time the request out. The page
        polls `/healthz` for progress. `?harvest=0` rebuilds from cache.
        """
        if request.method == "GET":
            return HTMLResponse(_REFRESH_PAGE)

        do_harvest = request.query_params.get("harvest", "1") not in ("0", "false", "no")
        if state.refreshing or state.lock.locked():
            return JSONResponse({"started": False, "already_refreshing": True,
                                 **state.snapshot()})
        task = asyncio.create_task(refresh(state, cfg, harvest=do_harvest))
        refresh_tasks.add(task)
        task.add_done_callback(refresh_tasks.discard)
        return JSONResponse({"started": True, "harvest": do_harvest, **state.snapshot()})

    async def debug_incomplete(request):
        """Events with empty key fields (date/time/location/link) — a
        data-quality view for debugging. `?field=link` narrows to one missing
        field. Reads the same cache the page serves."""
        events, _counts, _total = _read_cached_events()
        rows, missing_counts = _incomplete_events(events)
        only = request.query_params.get("field")
        if only in _KEY_FIELDS:
            rows = [r for r in rows if only in r["missing"]]
        return JSONResponse(
            {
                "ok": True,
                "total_events": len(events),
                "incomplete": len(rows),
                "missing_counts": missing_counts,
                "events": rows,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def debug_empty(request):
        """Configured sources that produced no events — the 'which source went
        dark?' view. Every web source and known messaging platform is listed
        with its event count, zero-count first. Each web source also carries its
        cooldown state so a *skipped* source (fetched too recently, so the last
        run passed it over) is distinguished from one that was fetched and
        genuinely produced nothing. Reads the same cache the page serves."""
        from event_harvester.sources.web_utils import _load_web_sources

        events, _counts, _total = _read_cached_events()
        web_sources = _load_web_sources() or []
        web_rows, platform_counts, unattributed = _empty_sources(events, web_sources)
        cooldowns = _cooldown_state(web_sources)
        for r in web_rows:
            r.update(cooldowns.get(r["name"], {}))

        empty_web = [r["name"] for r in web_rows if r["events"] == 0]
        # A skipped source is empty *because a run passed it over* (cooling),
        # not because it produced nothing — call those out separately so they
        # aren't mistaken for broken scrapers.
        skipped = [r["name"] for r in web_rows if r["events"] == 0 and r.get("cooling")]
        empty_fetched = [r["name"] for r in web_rows
                         if r["events"] == 0 and not r.get("cooling")]
        empty_platforms = [p for p, c in platform_counts.items() if c == 0]
        return JSONResponse(
            {
                "ok": True,
                "total_events": len(events),
                "empty_web_sources": empty_web,
                "skipped_sources": skipped,
                "empty_after_fetch": empty_fetched,
                "empty_platforms": empty_platforms,
                "web_sources": web_rows,
                "platform_counts": platform_counts,
                "unattributed_web_events": unattributed,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _loop():
        """Rebuild every `interval_hours`, forever, counted from process start.

        Each refresh is due one interval after the last one was due, not
        after it finished. Counted from the finish, a 45-minute harvest moved
        the daily run 45 minutes later every day (queued 06:09:36 on
        2026-11-25, 06:54:46 on 2026-11-26). A refresh that overruns whole
        intervals skips them: one run at a time, never a backlog.
        """
        seconds = max(60.0, float(interval_hours) * 3600.0)
        clock = asyncio.get_running_loop().time
        due = clock() + seconds
        while True:
            await asyncio.sleep(max(0.0, due - clock()))
            logger.info("site: scheduled refresh")
            await refresh(state, cfg, harvest=True)
            due += seconds * max(1, math.ceil((clock() - due) / seconds))

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
    # `/sources` is read-mostly and its one mutation is an append to an attempt
    # log — it proposes a configuration, it never writes the source config.
    from event_harvester.servers.admin import build_routes as admin_routes
    from event_harvester.servers.sources_page import routes as sources_routes

    routes = [
        Route("/", index),
        Route("/calendar", calendar_page),
        Route("/events.json", events_json),
        Route("/healthz", healthz),
        Route("/refresh", refresh_now, methods=["GET", "POST"]),
        Route("/debug/incomplete", debug_incomplete),
        Route("/debug/empty", debug_empty),
        *sources_routes(),
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
