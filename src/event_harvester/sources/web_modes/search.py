"""Search mode: detect search bar, fill query, scroll, extract event links."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_utils import (
    DEFAULT_SCROLL_SECONDS,
    _extract_event_links,
    _fetch_event_details_parallel,
    _filter_known_links,
    _save_link_fingerprint,
    _scroll_for_seconds,
)

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


def _normalize_queries(src: dict) -> list[str]:
    """Return the search queries for a source, as a list.

    Accepts three spellings so existing configs keep working:
      * ``"query": "AI meetup"``            -> ``["AI meetup"]``
      * ``"query": ["ai", "queer party"]``  -> both
      * ``"queries": [...]``                -> both (preferred for multiples)

    Returns ``[]`` when none is set, which means "don't search, just scroll
    whatever the URL already shows".
    """
    # A source may name shared keyword lists instead of inlining its queries.
    # Those are the operator's own words (`data/keywords.json`) and this is the
    # one mode that can actually type them into a site, so they come first.
    if src.get("keywords"):
        from event_harvester.keywords import terms_for

        shared = terms_for(src["keywords"])
        if shared:
            inline = src.get("queries", src.get("query")) or []
            if isinstance(inline, str):
                inline = [inline]
            return _dedupe_queries([*shared, *inline])

    raw = src.get("queries", src.get("query"))
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    return _dedupe_queries(raw)


def _dedupe_queries(raw) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively, preserve order."""
    out: list[str] = []
    seen: set[str] = set()
    for q in raw or []:
        q = " ".join(str(q).split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def _do_search_inner(uc, src: dict, timeout_ms: int, max_events: int, page=None, tracer=None, capture=None) -> list[dict]:
    """Search mode: detect search bar, query, scroll, extract event links."""
    from urllib.parse import urlparse

    url = src["url"]
    queries = _normalize_queries(src)
    scroll_secs = src.get("scroll_seconds", DEFAULT_SCROLL_SECONDS)
    name = src.get("name", "web")

    logger.info("Search: %s (%d quer%s)", name, len(queries) or 1,
                "y" if len(queries) == 1 else "ies")

    if page is None:
        page = uc.open(url, source_name=name, tracer=tracer, capture=capture)
    uc.detect_all(page)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    if uc.has_login_wall(page):
        logger.warning("%s: login wall — skipping", name)
        page.close()
        return []

    def _run_one_search(query: str) -> bool:
        """Type `query` into the page's search box and submit. Returns success."""
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
            uc.auto_detect(page)
            if diff:
                logger.info("%s: search → %d changed, %d added",
                            name, diff.get("changed", 0), diff.get("added", 0))
        return searched

    # One pass per query, accumulating links. A single query is one keyhole
    # into a large site: `meetup` shipped with `"AI meetup"` and saw nothing
    # else, because the schema could only hold one string.
    collected: list[dict] = []
    seen_urls: set[str] = set()

    def _collect_links() -> None:
        for lnk in _extract_event_links(page) or []:
            key = lnk.get("url") or lnk.get("href")
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            collected.append(lnk)

    if queries:
        for i, query in enumerate(queries):
            if i > 0:
                # Return to the base URL between queries: on a results page
                # the search box may be a different element, and some sites
                # append rather than replace. Reloading keeps each query
                # independent, at the cost of one navigation.
                try:
                    page.goto(url, timeout=timeout_ms)
                    page.wait_for_timeout(1500)
                    uc.dismiss_cookies(page)
                    uc.close_modal(page)
                except Exception as e:
                    logger.warning("%s: could not reload for query %r: %s", name, query, e)
                    break
            if not _run_one_search(query):
                logger.warning("%s: search box not found for query %r", name, query)
            _scroll_for_seconds(page, scroll_secs, step_ms=1000)
            _collect_links()
            logger.info("%s: query %r → %d unique link(s) so far", name, query, len(collected))
    else:
        _scroll_for_seconds(page, scroll_secs, step_ms=1000)
        _collect_links()

    # Extract event links, filter already-known ones
    event_links = _filter_known_links(collected)[:max_events]
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
