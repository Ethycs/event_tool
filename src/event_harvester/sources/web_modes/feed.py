"""Feed mode: scroll page while intercepting API responses.

The dwell loop interleaves scroll, GIF-frame snapshots, and XHR capture,
so feed mode keeps its own scrolling rather than using the shared
``_scroll_for_seconds`` helper.

It used to own the Instagram-GraphQL post walker too. That moved to
``sources/instagram.py`` on 2026-08-06 when `instagram` mode started
needing it: the walker was the general half (parse a feed payload) and
the scroll loop was the specific half, and only one of them was about
feeds. This mode is now the answer for a site that *has* no API worth
addressing directly — scroll, capture whatever fires, parse it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_utils import (
    _extract_event_links,
    _fetch_event_details_parallel,
    _filter_known_links,
    _save_link_fingerprint,
)

logger = logging.getLogger("event_harvester.web_fetch")


def _do_feed(uc, src: dict, max_events: int = 30, page=None, tracer=None, capture=None) -> list[dict]:
    """Feed mode (OTel-wrapped). Body lives in ``_do_feed_inner``."""
    name = src.get("name", "web")
    mode = "feed"
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.{mode}") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", mode)
        try:
            messages = _do_feed_inner(
                uc, src, max_events=max_events,
                page=page, tracer=tracer, capture=capture,
            )
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _do_feed_inner(uc, src: dict, max_events: int = 30, page=None, tracer=None, capture=None) -> list[dict]:
    """Feed mode: scroll page while intercepting API responses.

    Note: page param is accepted for interface consistency but feed mode
    always opens its own page (needs response interception from first load).
    If a pre-opened page is passed, it's closed and re-opened with interception.
    """
    if page is not None:
        page.close()  # need to re-open with interception
    import time as _time
    from urllib.parse import urlparse

    url = src["url"]
    api_pattern = src.get("api_pattern", "graphql|api")
    scroll_secs = src.get("scroll_seconds", 30)
    name = src.get("name", "web")

    logger.info("Feed: scrolling %s for %ds...", name, scroll_secs)

    page, captured = uc.open_with_intercept(
        url, api_pattern, source_name=name, tracer=tracer, capture=capture,
    )
    page.wait_for_timeout(3000)

    # UC obstacle clearing + feed detection
    uc.detect_all(page)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    # Use UC-detected feed container for targeted scrolling
    feed_selector = None
    patterns = uc.get_patterns(page)
    if patterns.get("feed"):
        feed_selector = patterns["feed"][0].get("selector")
        logger.info("UC: scrolling detected feed container: %s", feed_selector)

    # Scroll (targeted if feed detected, full-page otherwise). When
    # screenshots are enabled, snap a PNG roughly every 5s during the
    # dwell so the GIF later shows the feed actually loading.
    end_time = _time.time() + scroll_secs
    scroll_count = 0
    feed_pngs: list = []
    last_snap = 0.0
    snap_interval_s = 5.0
    while _time.time() < end_time:
        if feed_selector:
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (el) el.scrollTop += el.clientHeight * 2;
                    else window.scrollBy(0, window.innerHeight * 2);
                }""",
                feed_selector,
            )
        else:
            page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(800)
        scroll_count += 1

        # Periodic GIF frame — only when capture is on. next_step()
        # claims a step id but does NOT write a JSONL row (just advances
        # the counter), which is what we want for raw frame captures.
        if capture is not None and tracer is not None:
            now = _time.time()
            if now - last_snap >= snap_interval_s:
                last_snap = now
                try:
                    p = capture.snap(page, step=tracer.next_step())
                    if p is not None:
                        feed_pngs.append(p)
                except Exception as e:  # noqa: BLE001 — capture is duck-typed
                    logger.debug("feed-snap failed for %s: %s", name, e)

    # Get page text — prefer feed container
    if feed_selector:
        page_text = page.evaluate(
            "(sel) => { const el = document.querySelector(sel); return el ? el.innerText : document.body.innerText; }",
            feed_selector,
        )
    else:
        page_text = page.evaluate("document.body.innerText") or ""
    logger.info("%s: scrolled %d times, captured %d API responses, %d chars page text.",
                name, scroll_count, len(captured), len(page_text))

    messages = []

    # Try event link extraction from scrolled page (maximizes events)
    # Skip for api_only sources (e.g. Instagram where links aren't event pages)
    event_links = []
    if not src.get("api_only"):
        event_links = _extract_event_links(page)
        event_links = _filter_known_links(event_links)
        if event_links:
            fetch_links = [l for l in event_links[:max_events] if not l.get("inline")]
            logger.info("%s: %d new event links, fetching in parallel...", name, len(fetch_links))
            results = _fetch_event_details_parallel(
                uc._context, uc._stealth_plugin, fetch_links, uc.timeout_ms,
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

    # Page text as a message (if no event links found)
    if not event_links and page_text and len(page_text) > 200:
        messages.append({
            # "web", not the source name — see _extract_posts_from_api.
            # Every other mode emits "web"; this was the one that didn't,
            # which exempted feed sources from the per-source `web` cap.
            "platform": "web",
            "id": f"feed:page:{hash(url)}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "author": name,
            "channel": url[:120],
            "content": page_text[:5000],
        })

    # Snapshot the final URL before closing the page so we can attach
    # it to the feed-gif trace row below.
    final_url = url
    try:
        final_url = page.url or url
    except Exception:
        pass
    page.close()

    # Assemble scroll-sequence GIF from the dwell-loop frames. Skips
    # cleanly when capture is off or no frames were captured. Pillow
    # missing -> gif_from_pngs returns None; we just don't record a row.
    if capture is not None and tracer is not None and feed_pngs:
        try:
            gif_path = capture.gif_from_pngs(
                feed_pngs, output=capture.out_dir / "feed.gif",
            )
            if gif_path is not None:
                try:
                    rel = str(gif_path.relative_to(tracer.out_dir))
                except ValueError:
                    rel = str(gif_path)
                tracer.record(
                    kind="feed-gif", url=final_url, image=rel,
                    label="scroll-sequence",
                )
        except Exception as e:  # noqa: BLE001 — capture is duck-typed
            logger.debug("feed-gif assembly failed for %s: %s", name, e)

    # Parse captured API responses. ``seen`` spans every response in the run:
    # a feed re-serves the same media in a prefetch and again in the timeline
    # query, so per-response dedup alone posts the same night twice.
    from event_harvester.sources.instagram import loads_multi, normalize_posts

    seen: set[str] = set()
    for resp_url, resp_body in captured:
        if tracer is not None:
            try:
                tracer.record(
                    kind="xhr", url=resp_url, parent=None, label="feed-scroll",
                )
            except Exception:
                pass
        for payload in loads_multi(resp_body):
            messages.extend(
                normalize_posts(
                    payload, source_name=name,
                    channel=f"{name}-feed", seen=seen,
                ),
            )

    return messages


# ── Instagram / generic GraphQL post extractor ───────────────────────

def _extract_posts_from_api(data: dict, url: str, source_name: str) -> list[dict]:
    """Extract individual posts from a feed API response.

    Thin wrapper over :func:`event_harvester.sources.instagram.normalize_posts`
    since 2026-08-06. The walker used to live here and keyed on *container*
    shape — ``edges`` and ``items``, recursing only through dict values, never
    through lists. A payload nesting its posts one array deeper (which is what
    Instagram's deferred timeline does) returned zero posts and no error. The
    shared walker keys on the *object* instead: a dict with a caption and an
    id is a post, wherever it sits.

    ``platform`` is always ``"web"`` per EH-STD-MSG-v0 — the *source* name
    goes in ``channel``. This used to emit ``"platform": "instagram"``, which
    made `CapConfig.get("instagram")` fall through to its default instead of
    applying the `web` cap, so feed sources were silently uncapped.
    """
    from event_harvester.sources.instagram import normalize_posts

    return normalize_posts(
        data, source_name=source_name, channel=f"{source_name}-feed",
    )
