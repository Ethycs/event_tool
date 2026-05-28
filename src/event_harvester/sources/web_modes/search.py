"""Search mode: detect search bar, fill query, scroll, extract event links."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from event_harvester.sources.web_utils import (
    DEFAULT_SCROLL_SECONDS,
    _extract_event_links,
    _fetch_event_details_parallel,
    _filter_known_links,
    _save_link_fingerprint,
    _scroll_for_seconds,
)
from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.web_fetch")


def _do_search(uc, src: dict, timeout_ms: int, max_events: int, page=None, tracer=None, capture=None) -> list[dict]:
    """Search mode (OTel-wrapped). Body lives in ``_do_search_inner``."""
    name = src.get("name", "web")
    mode = "search"
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.{mode}") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", mode)
        try:
            messages = _do_search_inner(
                uc, src, timeout_ms, max_events,
                page=page, tracer=tracer, capture=capture,
            )
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _do_search_inner(uc, src: dict, timeout_ms: int, max_events: int, page=None, tracer=None, capture=None) -> list[dict]:
    """Search mode: detect search bar, query, scroll, extract event links."""
    from urllib.parse import urlparse

    url = src["url"]
    query = src.get("query")
    scroll_secs = src.get("scroll_seconds", DEFAULT_SCROLL_SECONDS)
    name = src.get("name", "web")

    logger.info("Search: %s", name)

    if page is None:
        page = uc.open(url, source_name=name, tracer=tracer, capture=capture)
    uc.detect_all(page)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    if uc.has_login_wall(page):
        logger.warning("%s: login wall — skipping", name)
        page.close()
        return []

    # Search if query provided
    if query:
        uc.first_scan(page)
        searched = uc.search(page, query)

        # Fallback: generic input discovery if static detect missed the search bar
        if not searched:
            # Try UC generic discovery first, then inline JS fallback
            inputs = uc.find_inputs(page)
            if not inputs:
                inputs = page.evaluate("""() => {
                    const els = document.querySelectorAll(
                        'input[type="search"], input[type="text"], input:not([type]), textarea'
                    );
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const style = getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        let sel = '';
                        if (el.id) sel = '#' + el.id;
                        else if (el.getAttribute('placeholder'))
                            sel = el.tagName.toLowerCase()
                                + '[placeholder="' + el.getAttribute('placeholder').replace(/"/g, '\\\\"') + '"]';
                        else sel = el.tagName.toLowerCase() + '[type="' + (el.type || 'text') + '"]';
                        return [{selector: sel, score: 1}];
                    }
                    return [];
                }""") or []

            if inputs:
                best = inputs[0]
                logger.info("%s: using generic input: %s (score=%.1f)",
                            name, best.get("selector"), best.get("score", 0))
                try:
                    page.fill(best["selector"], query)
                    page.press(best["selector"], "Enter")
                    searched = True
                except Exception as e:
                    logger.debug("Generic search fill failed: %s", e)

        if searched:
            page.wait_for_timeout(3000)
            diff = uc.next_scan(page)
            detected = uc.auto_detect(page)
            if diff:
                logger.info("%s: search → %d changed, %d added",
                            name, diff.get("changed", 0), diff.get("added", 0))

    # Scroll
    _scroll_for_seconds(page, scroll_secs, step_ms=1000)

    # Extract event links, filter already-known ones
    event_links = _filter_known_links(_extract_event_links(page))[:max_events]
    messages = []
    domain = urlparse(url).netloc

    if event_links:
        logger.info("%s: %d event links, fetching in parallel...", name, len(event_links))
        results = _fetch_event_details_parallel(
            uc._context, uc._stealth_plugin, event_links, timeout_ms,
        )
        for lnk, text in results:
            if text:
                _save_link_fingerprint(lnk)
                messages.append({
                    "platform": "web",
                    "id": f"web:{hash(lnk['url'])}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "author": urlparse(lnk["url"]).netloc,
                    "channel": lnk["url"][:120],
                    "content": text[:5000],
                })
    else:
        # Fallback: feed items
        items = uc.get_feed_items(page)
        for i, item_text in enumerate(items):
            if len(item_text) > 30:
                messages.append({
                    "platform": "web",
                    "id": f"web:{hash(item_text[:100])}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "author": domain,
                    "channel": f"{name}-feed",
                    "content": item_text[:5000],
                })

    page.close()
    return messages
