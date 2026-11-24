"""Orchestrator for the web fetch pipeline.

Shape after the Phase 3 split:

  * Mode handlers live in ``web_modes/{calendar,feed,search,raw,chat}.py``.
  * Shared helpers (cooldowns, fingerprint filter, link/text extraction,
    JSON-LD parsing, Cloudflare wait, parallel detail fetch) live in
    ``web_utils.py``.
  * This file owns ``fetch_web_sources`` — it routes each configured source by
    the access technique its mode uses (``HTTP_MODES`` below: plain HTTP, no
    browser; everything else: a browser group), then hands it to the right
    ``_do_*`` handler.

Back-compat: the helpers and mode handlers that other modules (harvest,
cli/commands/web, sources/web_session) and the helper test suite import
directly are re-exported here. New code should import from the canonical
locations (``web_utils`` / ``web_modes.*``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import partial

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources import web_access_log, web_checkpoint
from event_harvester.sources.web_modes.calendar import _do_calendar
from event_harvester.sources.web_modes.chat import _do_chat
from event_harvester.sources.web_modes.feed import _do_feed
from event_harvester.sources.web_modes.ics import _do_ics
from event_harvester.sources.web_modes.instagram import _do_instagram
from event_harvester.sources.web_modes.luma import _do_luma
from event_harvester.sources.web_modes.partiful import _do_partiful
from event_harvester.sources.web_modes.raw import _do_raw
from event_harvester.sources.web_modes.rss import _do_rss
from event_harvester.sources.web_modes.search import _do_search
from event_harvester.sources.web_modes.steps import _do_steps

# Back-compat re-exports — modules outside of sources/ continue to import
# these from web_fetch. The canonical home is web_utils.
from event_harvester.sources.web_utils import (  # noqa: F401
    _COOLDOWN_FILE,
    _DATA_DIR,
    _DEFAULT_COOLDOWN,
    _DEFAULT_SOURCES,
    _JUNK_LINK_TITLES,
    _WEB_SOURCES_FILE,
    BOT_CHECK_WAIT_S,
    DEFAULT_SCROLL_SECONDS,
    SourceBlocked,
    SourceDeviated,
    _decimate_text,
    _detect_mode,
    _extract_event_links,
    _extract_structured_data,
    _extract_text,
    _fetch_event_details_parallel,
    _filter_known_links,
    _is_dead_page,
    _is_junk_link_title,
    _is_on_cooldown,
    _load_cooldowns,
    _load_web_sources,
    _save_cooldown,
    _save_link_fingerprint,
    _scroll_for_seconds,
    _wait_cloudflare,
    is_bot_check,
)

logger = logging.getLogger("event_harvester.web_fetch")
tracer = get_tracer("event_harvester.web_fetch")

# ── Routing: the access technique each mode uses ─────────────────────

#: Modes read over plain HTTP — a published feed or a platform's own JSON.
#: No browser is involved: these sources never enter a browser group and
#: their handlers take no browser. Every mode not listed here drives a page.
HTTP_MODES = {
    "ics": _do_ics,            # iCalendar feed
    "rss": _do_rss,            # RSS / Atom feed
    "luma": _do_luma,          # Luma's keyless discovery API
    "partiful": _do_partiful,  # the props Partiful's pages are rendered from
}


# ── What happened to each source ─────────────────────────────────────

#: Seconds one source may run before it is declared hung. The longest honest
#: source measured (about 350 detail pages) took eleven minutes. Override per
#: source with ``deadline_s`` in ``web_sources.json``.
SOURCE_DEADLINE_S = 20 * 60

#: Per-source outcomes of the most recent runs, newest last.
_REPORT_FILE = _DATA_DIR / ".web_source_report.json"
_REPORT_RUNS_KEPT = 30

#: ``ok``       at least one message (and at least ``expect_min_events``)
#: ``low``      fewer messages than the source's ``expect_min_events``
#: ``empty``    ran cleanly and found nothing
#: ``blocked``  a bot check stood where the page should be; needs a person
#: ``deviated`` the page is not the page the source's handler was written for
#: ``failed``   raised
#: ``hung``     ran past its deadline; its thread and browser were abandoned
#: ``skipped``  queued behind a hung source in the same browser, so never ran
STATES = ("ok", "low", "empty", "blocked", "deviated", "failed", "hung", "skipped")


@dataclass
class SourceOutcome:
    """What one source did in one run. ``state`` is one of :data:`STATES`."""

    name: str
    mode: str
    state: str
    messages: int = 0
    seconds: float = 0.0
    detail: str = ""
    #: The access method the source was reached by: ``http``, ``standard``
    #: (Chromium with the UC extension) or ``native`` (installed Chrome).
    method: str = ""


class _Group:
    """One worker thread: its sources, what it has produced, where it is."""

    def __init__(self, label: str, method: str, sources: list[dict], work, on_abandon=None):
        self.label = label
        #: Called once if the group is given up on. For a group attached to
        #: the UC service this revokes its pages there, which is what
        #: unblocks the stuck thread.
        self.on_abandon = on_abandon
        #: The access method every source in this group is reached by. A
        #: check passed by one method says nothing about another, so states
        #: are recorded per method.
        self.method = method
        self.sources = sources
        self.messages: list[dict] = []
        self.cf_retries: list[dict] = []
        #: ``(source, monotonic start)`` while a source runs, else ``None``.
        self.current: tuple[dict, float] | None = None
        self.abandoned = False
        self.error: BaseException | None = None
        self.lock = threading.Lock()
        self.done = threading.Event()
        self._work = work
        # Daemon: a thread stuck inside a browser call must not keep the
        # process alive once it has been given up on.
        self.thread = threading.Thread(target=self._run, name=f"web-{label}", daemon=True)

    def _run(self) -> None:
        try:
            self._work(self)
        except BaseException as e:  # noqa: BLE001 — reported by the caller
            self.error = e
        finally:
            self.done.set()


def _revoke(service: str, label: str) -> None:
    """Have the UC service close the pages of the client(s) labelled ``label``.

    A thread stuck in a browser call cannot be killed from here. The service
    holds the browser, so it can close the page under the call, and the call
    then fails and the thread unwinds.
    """
    from uc_browser.service.client import ServiceClient

    result = ServiceClient(service).revoke(label)
    logger.warning("Revoked %s at the UC service: %s", label, result)


def _log_access(name: str, state: str, detail: str, method: str, *,
                messages: int = 0, observed: bool = True) -> None:
    try:
        web_access_log.record(
            name, state, detail, method=method, messages=messages, observed=observed,
        )
    except Exception as e:  # noqa: BLE001 — the log must not cost the harvest
        logger.warning("Could not write the access log for %s: %s", name, e)


def _note(outcomes: dict[str, SourceOutcome], outcome: SourceOutcome) -> None:
    """Keep ``outcome`` for the run report and put it in the access log.

    Logged as it happens, not at the end of the run, so a run that is cut
    short still leaves what it learned. A skipped source was not accessed, so
    it is logged as a failure of the run without moving the source's state.
    """
    outcomes[outcome.name] = outcome
    _log_access(
        outcome.name, outcome.state, outcome.detail, outcome.method,
        messages=outcome.messages, observed=outcome.state != "skipped",
    )


def _await_groups(groups: list[_Group], outcomes: dict[str, SourceOutcome],
                  poll_s: float = 1.0) -> None:
    """Run ``groups`` to the end, giving up on one whose source has hung.

    A sync Playwright call can block for ever and a thread cannot be killed,
    so the only thing left to decide is who waits. Before 2026-11-24 the whole
    run waited: one hung source held the job to its 90-minute limit and every
    finished source's messages were discarded with it (2026-11-19 and
    2026-11-22, both in ``ra-sf``'s detail pages). Now the hung source is
    recorded, the sources queued behind it in the same browser are recorded as
    skipped, and everything already produced is kept.
    """
    for group in groups:
        group.thread.start()
    pending = list(groups)
    while pending:
        for group in list(pending):
            if group.done.is_set():
                pending.remove(group)
                continue
            with group.lock:
                current = group.current
                if current is None or group.done.is_set():
                    continue
                src, started = current
                ran = time.monotonic() - started
                if ran <= float(src.get("deadline_s", SOURCE_DEADLINE_S)):
                    continue
                group.abandoned = True
            pending.remove(group)
            if group.on_abandon is not None:
                try:
                    group.on_abandon()
                except Exception as e:  # noqa: BLE001 — best effort; the run goes on
                    logger.warning("Could not release the hung group %s: %s", group.label, e)
            name = src.get("name", src["url"])
            logger.error(
                "%s: no result after %.0fs — giving up on it and on its browser.", name, ran,
            )
            _note(outcomes, SourceOutcome(
                name, src.get("mode") or "auto", "hung", seconds=round(ran, 1),
                detail=f"exceeded its {ran:.0f}s deadline", method=group.method,
            ))
            for later in group.sources[group.sources.index(src) + 1:]:
                later_name = later.get("name", later["url"])
                if later_name not in outcomes:
                    _note(outcomes, SourceOutcome(
                        later_name, later.get("mode") or "auto", "skipped",
                        detail=f"queued behind {name}, which hung", method=group.method,
                    ))
        if pending:
            pending[0].done.wait(poll_s)

    for group in groups:
        if group.error is None:
            continue
        # The group's browser did not start, or died outside any one source.
        logger.error("Web group %s failed: %s", group.label, group.error)
        for src in group.sources:
            name = src.get("name", src["url"])
            if name not in outcomes:
                _note(outcomes, SourceOutcome(
                    name, src.get("mode") or "auto", "failed",
                    detail=str(group.error)[:200], method=group.method,
                ))


def _report(outcomes: dict[str, SourceOutcome], total_messages: int) -> None:
    """Say which sources need attention, and keep the run on disk."""
    by_state: dict[str, list[str]] = {}
    for outcome in outcomes.values():
        by_state.setdefault(outcome.state, []).append(outcome.name)

    attention = "; ".join(
        f"{state}: {', '.join(sorted(by_state[state]))}"
        for state in ("blocked", "deviated", "hung", "skipped", "failed", "low")
        if state in by_state
    )
    if attention:
        logger.warning("Web sources needing attention — %s", attention)
    if "empty" in by_state:
        logger.info("Web sources that found nothing — %s", ", ".join(sorted(by_state["empty"])))

    try:
        from event_harvester.utils import load_json, save_json

        runs = (load_json(_REPORT_FILE, default={}) or {}).get("runs", [])
        runs.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "messages": total_messages,
            "sources": {
                name: {k: v for k, v in asdict(o).items() if k != "name"}
                for name, o in sorted(outcomes.items())
            },
        })
        save_json(_REPORT_FILE, {"runs": runs[-_REPORT_RUNS_KEPT:]})
    except Exception as e:  # noqa: BLE001 — the report must not cost the harvest
        logger.warning("Could not write %s: %s", _REPORT_FILE.name, e)


# ── Parallel-browser dispatch ────────────────────────────────────────

def _chunk_sources(sources: list[dict], n: int) -> list[list[dict]]:
    """Split ``sources`` into ``n`` round-robin chunks of similar size.

    Used to dispatch the standard (non-Cloudflare) sources across
    ``n`` parallel UCBrowser instances. Round-robin balances the slow
    feed/calendar sources across workers rather than dumping them all
    on one. Empty chunks are dropped from the result.
    """
    if not sources or n <= 1:
        return [list(sources)] if sources else []
    n = min(n, len(sources))
    out: list[list[dict]] = [[] for _ in range(n)]
    for i, src in enumerate(sources):
        out[i % n].append(src)
    return [c for c in out if c]


def fetch_web_sources(
    sources: list[dict] | None = None,
    max_events: int = 30,
    timeout_ms: int = 30000,
    no_cooldown: bool = False,
    parallel: int = 1,
    debug_trace: bool = False,
    debug_screenshots: bool = False,
    uc_service: str = "",
    uc_native_window: str = "",
) -> list[dict]:
    """Fetch events from all configured web sources.

    Sources are routed by the access technique their mode uses. Modes in
    ``HTTP_MODES`` are read over plain HTTP by one worker and never see a
    browser. Of the rest, standard (non-Cloudflare) sources are split across
    ``parallel`` browsers running concurrently; Cloudflare-protected
    sources (``native_chrome: true``) stay in a single serial group to
    keep request rates low.

    ``debug_trace`` (default off): when True, every page session is
    recorded to ``data/.web_traces/<source>/<run-id>.jsonl`` via the
    NavigationTracer. Useful for debugging which URLs a source actually
    hit during a run.

    ``debug_screenshots`` (default off): when True, capture per-step
    PNG screenshots alongside the JSONL trace and assemble a
    scroll-sequence GIF for feed sources. Implies ``debug_trace``.
    """
    # Screenshots imply trace — see config.load_config(). Re-apply here
    # so direct callers (tests, scripts) get the same implication.
    if debug_screenshots:
        debug_trace = True
    if sources is None:
        sources = _load_web_sources()
    if not sources:
        return []

    # Filter cooldowns
    active = []
    for src in sources:
        cd_hours = src.get("cooldown_hours", -1)
        if not no_cooldown and cd_hours >= 0 and _is_on_cooldown(src["url"], timedelta(hours=cd_hours)):
            logger.info("Skipping %s (on cooldown).", src.get("name", src["url"]))
        else:
            active.append(src)
    if not active:
        return []

    # Route by access technique. Plain-HTTP sources need no browser and are
    # given none. Browser sources split into standard (Chromium + UC
    # extension) and native Chrome (Cloudflare).
    http_srcs = [s for s in active if s.get("mode") in HTTP_MODES]
    browser_srcs = [s for s in active if s.get("mode") not in HTTP_MODES]
    standard_srcs = [s for s in browser_srcs if not s.get("native_chrome")]
    native_srcs = [s for s in browser_srcs if s.get("native_chrome")]

    def _make_tracer_and_capture(source_name: str):
        """Build a (tracer, capture, run_id) triple for one source.

        Returns ``(None, None, None)`` when ``debug_trace`` is off. When
        ``debug_screenshots`` is also on, the capture writes to a
        ``<tracer.out_dir>/<source>/<run_id>/`` subdirectory so the
        JSONL row's ``image`` field can be stored relative to
        ``tracer.out_dir`` as ``<source>/<run_id>/<step>.png``.
        """
        if not debug_trace:
            return None, None, None
        import secrets as _sec
        import time as _t

        from event_harvester.observability.navigation_trace import NavigationTracer
        run_id = _t.strftime("%Y%m%dT%H%M%S") + "_" + _sec.token_hex(2)
        nav_tracer = None
        try:
            nav_tracer = NavigationTracer(source_name=source_name, run_id=run_id)
        except Exception as e:
            logger.debug("NavigationTracer init failed for %s: %s", source_name, e)
            return None, None, None
        capture = None
        if debug_screenshots:
            # Lazy import: keeps screenshot module out of the import
            # graph when --debug-web-screenshots is off.
            try:
                from event_harvester.observability.screenshot_capture import ScreenshotCapture
                capture = ScreenshotCapture(
                    out_dir=nav_tracer.out_dir / source_name / run_id,
                )
            except Exception as e:
                logger.debug("ScreenshotCapture init failed for %s: %s", source_name, e)
                capture = None
        return nav_tracer, capture, run_id

    otel_tracer = get_tracer("event_harvester.web_fetch")
    outcomes: dict[str, SourceOutcome] = {}

    def _run_source(group: _Group, src: dict, fetch) -> None:
        """One source under its span, trace pair and error boundary.

        ``fetch(tracer=, capture=)`` returns the source's messages, or ``None``
        when it handed the source on (queued for the native Chrome retry). One
        bad source never aborts the run: what happened to it is recorded in
        ``outcomes`` and the group moves on.
        """
        name = src.get("name", src["url"])
        mode = src.get("mode") or "auto"
        started = time.monotonic()
        with group.lock:
            group.current = (src, started)
        state, msgs, detail = "failed", [], ""
        nav_tracer, capture, _run_id = _make_tracer_and_capture(name)
        with otel_tracer.start_as_current_span(f"web.{name}") as src_span:
            src_span.set_attribute("source.name", name)
            src_span.set_attribute("source.url", src.get("url", ""))
            src_span.set_attribute("source.mode", mode)
            try:
                msgs = fetch(tracer=nav_tracer, capture=capture)
                if msgs is None:
                    state, msgs = "deferred", []
                elif not msgs:
                    state = "empty"
                elif len(msgs) < int(src.get("expect_min_events") or 0):
                    state = "low"
                else:
                    state = "ok"
            except SourceBlocked as e:
                state, detail = "blocked", str(e)
                logger.warning(
                    "%s: blocked by a bot check — it needs a person at the browser.", name,
                )
            except SourceDeviated as e:
                state, detail = "deviated", str(e)[:300]
                logger.warning("%s: not the page this source was written for — %s", name, e)
                src_span.set_attribute("source.deviation", e.kind)
            except Exception as e:
                state, detail = "failed", str(e)[:200]
                logger.error("Failed %s (%s): %s", name, mode, e)
                src_span.set_attribute("error.message", str(e))
                try:
                    src_span.record_exception(e)
                except Exception:
                    pass
            finally:
                if nav_tracer is not None:
                    try:
                        nav_tracer.close()
                    except Exception:
                        pass
                if capture is not None:
                    try:
                        capture.close()
                    except Exception:
                        pass
            src_span.set_attribute("source.state", state)

        with group.lock:
            group.current = None
            if group.abandoned:
                # Declared hung while it ran. Its result is not counted, so the
                # report and the messages cannot disagree about it.
                return
            group.messages.extend(msgs)
        if state == "deferred":
            # Blocked by this method and handed to another. The run's outcome
            # for the source comes from the retry; this method's failure is
            # still a fact about this method.
            _log_access(
                name, "blocked", "bot check; handed to the native Chrome retry", group.method,
            )
        else:
            _note(outcomes, SourceOutcome(
                name, mode, state, len(msgs), round(time.monotonic() - started, 1), detail,
                method=group.method,
            ))
        if msgs:
            _save_cooldown(src["url"])
        if state in ("ok", "low", "empty"):
            logger.info("%s: %d messages.", name, len(msgs))
        if state == "low" and mode != "steps":  # steps mode logs its own tripwire
            logger.warning(
                "%s: %d message(s), below expect_min_events=%s.",
                name, len(msgs), src.get("expect_min_events"),
            )

    def _http_work(group: _Group) -> None:
        """Feeds and platform APIs, one after another. No browser."""
        for src in group.sources:
            if group.abandoned:
                break
            _run_source(group, src, partial(HTTP_MODES[src["mode"]], src))

    def _browser_source(uc, src: dict, native: bool, cf_retries: list, *, tracer, capture):
        """One browser source. ``None`` means it was queued in ``cf_retries``."""
        mode = src.get("mode")
        name = src.get("name", src["url"])

        # Two modes open their own pages. Chat waits longer than the shared
        # open does. Instagram drives one page per account, each recorded from
        # first paint, so a shared pre-opened page would only be a page it
        # immediately closes.
        if mode == "chat":
            return _do_chat(uc, src, tracer=tracer, capture=capture)
        if mode == "instagram":
            return _do_instagram(uc, src, max_events, tracer=tracer, capture=capture)

        # All other modes: open page, detect mode if needed.
        # A recorded MHTML checkpoint replaces the live open (and
        # its Cloudflare wait) when EH_WEB_CHECKPOINT=replay; a
        # miss falls through to the live fetch below.
        replayed = web_checkpoint.replay(
            uc, src["url"], source_name=name, tracer=tracer, capture=capture,
        )
        if replayed is not None:
            page, html = replayed
        else:
            page = uc.open(src["url"], source_name=name, tracer=tracer, capture=capture)
            html = _wait_cloudflare(
                page, headless=False,
                wait_s=int(src.get("bot_check_wait_s", BOT_CHECK_WAIT_S)),
            )

        # Bot check not cleared. A standard browser hands the source to the
        # native Chrome retry. In native Chrome there is nowhere further to
        # go: the page is a challenge, not content, and is never scraped.
        if is_bot_check(html):
            page.close()
            if native:
                raise SourceBlocked(
                    "the source's page", "cloudflare_challenge",
                    "bot check did not clear in native Chrome",
                )
            logger.info("%s: Cloudflare — queuing for native Chrome retry.", name)
            cf_retries.append(src)
            return None

        # Snapshot the cleared page (no-op unless recording).
        web_checkpoint.record(page, src["url"])

        # Auto-mode: check saved signatures, then detect
        if not mode or mode == "auto":
            uc.detect_all(page)
            uc.dismiss_cookies(page)
            uc.close_modal(page)

            # Check saved signature first
            try:
                sigs = uc.load_signatures(page)
                mode_sig = next(
                    (s for s in sigs if s.get("pattern", "").startswith("mode:")),
                    None,
                )
                if mode_sig:
                    mode = mode_sig["pattern"].split(":")[1]
                    logger.info("%s: saved signature → mode=%s", name, mode)
            except Exception:
                pass

            if not mode or mode == "auto":
                mode = _detect_mode(uc, page, src)
                logger.info("%s: auto-detected mode=%s", name, mode)

        # Dispatch with pre-opened page
        if mode == "calendar":
            msgs = _do_calendar(
                uc, src, timeout_ms, max_events, page=page, tracer=tracer, capture=capture,
            )
        elif mode == "feed":
            msgs = _do_feed(uc, src, max_events, page=page, tracer=tracer, capture=capture)
        elif mode == "search":
            msgs = _do_search(
                uc, src, timeout_ms, max_events, page=page, tracer=tracer, capture=capture,
            )
        elif mode == "steps":
            msgs = _do_steps(
                uc, src, timeout_ms, max_events, page=page, tracer=tracer, capture=capture,
            )
        else:
            msgs = _do_raw(uc, src, page=page, tracer=tracer, capture=capture)

        # Save mode as signature on success
        if msgs:
            try:
                uc.save_signature(page, f"mode:{mode}")
            except Exception:
                pass
        return msgs

    def _browser_work(uc_kwargs: dict, final: bool, group: _Group) -> None:
        """One browser for the group, its sources one after another.

        ``final``: there is no further route to hand a bot-checked source to.
        """
        from event_harvester.sources.uc_browser import WebBrowser

        native = final
        with WebBrowser(**uc_kwargs) as uc:
            for src in group.sources:
                if group.abandoned:
                    break
                try:
                    _run_source(
                        group, src,
                        partial(_browser_source, uc, src, native, group.cf_retries),
                    )
                finally:
                    # Whatever the mode left open — an exception between a
                    # page's open and its close used to strand the tab.
                    leaked = uc.close_pages()
                    if leaked:
                        logger.debug(
                            "%s: closed %d page(s) left open.", src.get("name"), leaked,
                        )

    # How the native route gets its browser. Named here, by the client: a
    # window of the UC service when one is configured, else a Chrome this run
    # launches and closes. They are different methods and are logged as
    # such: a check cleared by one does not carry to the other.
    on_abandon = None
    if uc_service and uc_native_window:
        label = f"event_tool:{os.getpid()}:{int(time.time())}"
        native_method = f"service:{uc_native_window}"
        native_browser = {"service": uc_service, "window": uc_native_window, "label": label}
        on_abandon = partial(_revoke, uc_service, label)
    else:
        native_method, native_browser = "native", {"native_chrome": True}

    # One worker for the plain-HTTP sources; standard browser sources split
    # across ``parallel`` browsers; native_chrome stays in one bucket. Each
    # browser group runs in its own thread with its own Playwright instance.
    standard_chunks = _chunk_sources(standard_srcs, parallel)
    groups: list[_Group] = []
    if http_srcs:
        groups.append(_Group("http", "http", http_srcs, _http_work))
    for i, chunk in enumerate(standard_chunks):
        groups.append(_Group(
            f"standard-{i}", "standard", chunk, partial(_browser_work, {}, False),
        ))
    if native_srcs:
        groups.append(_Group(
            "native", native_method, native_srcs,
            partial(_browser_work, native_browser, True), on_abandon=on_abandon,
        ))
    logger.info(
        "Web fetch: %d source(s) over plain HTTP; %d standard source(s) across "
        "%d browser(s); %d native_chrome source(s) serial via %s.",
        len(http_srcs), len(standard_srcs), len(standard_chunks), len(native_srcs),
        native_method,
    )
    _await_groups(groups, outcomes)

    # Phase 4: Cloudflare auto-retry with native Chrome
    cf_retries = [src for group in groups for src in group.cf_retries]
    if cf_retries:
        logger.info("Retrying %d source(s) with native Chrome (Cloudflare)...", len(cf_retries))
        retry = _Group(
            "native-retry", native_method, cf_retries,
            partial(_browser_work, native_browser, True), on_abandon=on_abandon,
        )
        groups.append(retry)
        _await_groups([retry], outcomes)

    all_messages = [m for group in groups for m in group.messages]

    # Universal keyword pass. Native search and Luma's categories narrow what
    # gets *fetched*; this marks what came back, and is the only thing that
    # works on a source with no query surface at all (partiful, ics, rss...).
    # It tags rather than filters — see `keywords` module docstring.
    _tag_keywords(active, all_messages)

    logger.info("Web sources: %d messages from %d sources.", len(all_messages), len(active))
    _report(outcomes, len(all_messages))
    return all_messages


def _tag_keywords(sources: list[dict], messages: list[dict]) -> None:
    """Tag each source's messages with the keyword terms they match."""
    by_name = {s.get("name"): s for s in sources if s.get("keywords")}
    if not by_name:
        return
    from event_harvester.keywords import tag_messages, terms_for

    total = 0
    for name, src in by_name.items():
        terms = terms_for(src["keywords"])
        if not terms:
            continue
        # `channel` carries the source name for every web mode; sub-source
        # modes suffix it (`partiful-sf`), so match on the prefix.
        mine = [
            m for m in messages
            if str(m.get("channel", "")).split("-")[0] == str(name).split("-")[0]
        ]
        total += tag_messages(mine, terms)
    if total:
        logger.info("Keywords: %d message(s) matched.", total)
