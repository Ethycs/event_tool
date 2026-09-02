"""Orchestrator for the web fetch pipeline.

Shape after the Phase 3 split:

  * Mode handlers live in ``web_modes/{calendar,feed,search,raw,chat}.py``.
  * Shared helpers (cooldowns, fingerprint filter, link/text extraction,
    JSON-LD parsing, Cloudflare wait, parallel detail fetch) live in
    ``web_utils.py``.
  * This file owns ``fetch_web_sources`` — the parallel-browser dispatch
    that walks the configured sources, picks a worker mode, opens a page,
    and routes to the right ``_do_*`` handler.

Back-compat: the helpers and mode handlers that other modules (harvest,
cli/commands/web, sources/web_session) and the helper test suite import
directly are re-exported here. New code should import from the canonical
locations (``web_utils`` / ``web_modes.*``).
"""

from __future__ import annotations

import concurrent.futures
import logging
from datetime import timedelta

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources import web_checkpoint
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
    DEFAULT_SCROLL_SECONDS,
    _decimate_text,
    _detect_mode,
    _extract_event_links,
    _extract_structured_data,
    _extract_text,
    _fetch_event_detail,
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
)

logger = logging.getLogger("event_harvester.web_fetch")
tracer = get_tracer("event_harvester.web_fetch")


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
) -> list[dict]:
    """Fetch events from all configured web sources.

    Standard (non-Cloudflare) sources are split across ``parallel``
    UCBrowser instances running concurrently; Cloudflare-protected
    sources (``native_chrome: true``) stay in a single serial group to
    keep request rates low. Setting ``parallel=1`` restores the
    one-browser-for-everything behavior.

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

    # Group: standard sources (Chromium + UC extension) vs native Chrome (Cloudflare)
    standard_srcs = [s for s in active if not s.get("native_chrome")]
    native_srcs = [s for s in active if s.get("native_chrome")]

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

    def _process_group(group: list[dict], **uc_kwargs) -> list[dict]:
        from event_harvester.sources.uc_browser import UCBrowser

        otel_tracer = get_tracer("event_harvester.web_fetch")
        messages = []
        cf_retries = []
        with UCBrowser(**uc_kwargs) as uc:
            for src in group:
                mode = src.get("mode")
                name = src.get("name", src["url"])
                nav_tracer, capture, _run_id = _make_tracer_and_capture(name)
                with otel_tracer.start_as_current_span(f"web.{name}") as src_span:
                    src_span.set_attribute("source.name", name)
                    src_span.set_attribute("source.url", src.get("url", ""))
                    src_span.set_attribute("source.mode", src.get("mode", "auto") or "auto")
                    try:
                        # Chat mode doesn't need page pre-open
                        if mode == "chat":
                            msgs = _do_chat(uc, src, tracer=nav_tracer, capture=capture)
                            messages.extend(msgs)
                            if msgs:
                                _save_cooldown(src["url"])
                            logger.info("%s: %d messages.", name, len(msgs))
                            continue

                        # ICS mode: iCalendar feed over HTTP -- no browser
                        # needed (and would fail to navigate webcal:// URLs).
                        if mode == "ics":
                            msgs = _do_ics(uc, src, tracer=nav_tracer, capture=capture)
                            messages.extend(msgs)
                            if msgs:
                                _save_cooldown(src["url"])
                            logger.info("%s: %d messages.", name, len(msgs))
                            continue

                        # RSS mode: same browser-free contract as ICS. One
                        # message per show beats one 30k-char blob per venue.
                        if mode == "rss":
                            msgs = _do_rss(uc, src, tracer=nav_tracer, capture=capture)
                            messages.extend(msgs)
                            if msgs:
                                _save_cooldown(src["url"])
                            logger.info("%s: %d messages.", name, len(msgs))
                            continue

                        # Luma and Partiful publish their event lists as
                        # structured JSON — Luma via a keyless discovery API,
                        # Partiful in its server-rendered Next.js props. Both
                        # are browser-free for the same reason ICS and RSS
                        # are, and both replace a scrape that produced one
                        # event from a page listing dozens.
                        if mode == "luma":
                            msgs = _do_luma(uc, src, tracer=nav_tracer, capture=capture)
                            messages.extend(msgs)
                            if msgs:
                                _save_cooldown(src["url"])
                            logger.info("%s: %d messages.", name, len(msgs))
                            continue

                        if mode == "partiful":
                            msgs = _do_partiful(
                                uc, src, tracer=nav_tracer, capture=capture,
                            )
                            messages.extend(msgs)
                            if msgs:
                                _save_cooldown(src["url"])
                            logger.info("%s: %d messages.", name, len(msgs))
                            continue

                        # Instagram mode drives its own pages (one per
                        # account, each recorded from first paint), so the
                        # shared page-open + Cloudflare wait below would only
                        # be a page it immediately closes.
                        if mode == "instagram":
                            msgs = _do_instagram(
                                uc, src, max_events,
                                tracer=nav_tracer, capture=capture,
                            )
                            messages.extend(msgs)
                            if msgs:
                                _save_cooldown(src["url"])
                            logger.info("%s: %d messages.", name, len(msgs))
                            continue

                        # All other modes: open page, detect mode if needed.
                        # A recorded MHTML checkpoint replaces the live open (and
                        # its Cloudflare wait) when EH_WEB_CHECKPOINT=replay; a
                        # miss falls through to the live fetch below.
                        replayed = web_checkpoint.replay(
                            uc, src["url"], source_name=name,
                            tracer=nav_tracer, capture=capture,
                        )
                        if replayed is not None:
                            page, html = replayed
                        else:
                            page = uc.open(
                                src["url"], source_name=name, tracer=nav_tracer, capture=capture,
                            )
                            html = _wait_cloudflare(page, headless=False)

                        # Cloudflare blocked — queue for native Chrome retry
                        if "just a moment" in html.lower():
                            if not uc_kwargs.get("native_chrome"):
                                logger.info("%s: Cloudflare — queuing for native Chrome retry.", name)
                                cf_retries.append(src)
                                page.close()
                                continue

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
                            msgs = _do_calendar(uc, src, timeout_ms, max_events, page=page, tracer=nav_tracer, capture=capture)
                        elif mode == "feed":
                            msgs = _do_feed(uc, src, max_events, page=page, tracer=nav_tracer, capture=capture)
                        elif mode == "search":
                            msgs = _do_search(uc, src, timeout_ms, max_events, page=page, tracer=nav_tracer, capture=capture)
                        elif mode == "steps":
                            msgs = _do_steps(
                                uc, src, timeout_ms, max_events,
                                page=page, tracer=nav_tracer, capture=capture,
                            )
                        else:
                            msgs = _do_raw(uc, src, page=page, tracer=nav_tracer, capture=capture)

                        # Save mode as signature on success
                        if msgs:
                            try:
                                uc.save_signature(page, f"mode:{mode}")
                            except Exception:
                                pass
                        messages.extend(msgs)
                        if msgs:
                            _save_cooldown(src["url"])
                        logger.info("%s: %d messages.", name, len(msgs))
                    except Exception as e:
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
        return messages, cf_retries

    # Split standard sources across ``parallel`` UCBrowsers; native_chrome
    # stays in one bucket. Each chunk runs in its own worker thread with
    # its own Playwright instance.
    standard_chunks = _chunk_sources(standard_srcs, parallel)
    worker_count = len(standard_chunks) + (1 if native_srcs else 0)
    if worker_count <= 0:
        return []
    if len(standard_chunks) > 1:
        logger.info(
            "Web fetch: %d standard source(s) across %d parallel browser(s); "
            "%d native_chrome source(s) serial.",
            len(standard_srcs), len(standard_chunks), len(native_srcs),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = []
        for chunk in standard_chunks:
            futures.append(pool.submit(_process_group, chunk))
        if native_srcs:
            futures.append(pool.submit(_process_group, native_srcs, native_chrome=True))

        all_messages = []
        all_cf_retries = []
        for f in concurrent.futures.as_completed(futures):
            msgs, retries = f.result()
            all_messages.extend(msgs)
            all_cf_retries.extend(retries)

    # Phase 4: Cloudflare auto-retry with native Chrome
    if all_cf_retries:
        logger.info("Retrying %d source(s) with native Chrome (Cloudflare)...", len(all_cf_retries))
        retry_msgs, _ = _process_group(all_cf_retries, native_chrome=True)
        all_messages.extend(retry_msgs)

    # Universal keyword pass. Native search and Luma's categories narrow what
    # gets *fetched*; this marks what came back, and is the only thing that
    # works on a source with no query surface at all (partiful, ics, rss...).
    # It tags rather than filters — see `keywords` module docstring.
    _tag_keywords(active, all_messages)

    logger.info("Web sources: %d messages from %d sources.", len(all_messages), len(active))
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
