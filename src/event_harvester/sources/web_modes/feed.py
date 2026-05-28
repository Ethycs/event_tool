"""Feed mode: scroll page while intercepting API responses.

The dwell loop interleaves scroll, GIF-frame snapshots, and XHR capture,
so feed mode keeps its own scrolling rather than using the shared
``_scroll_for_seconds`` helper. Owns the Instagram-GraphQL post walker
since no other mode uses it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from event_harvester.sources.web_utils import (
    _extract_event_links,
    _fetch_event_details_parallel,
    _filter_known_links,
    _save_link_fingerprint,
)
from event_harvester.observability.tracing import get_tracer

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
    import json as json_mod
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
    domain = urlparse(url).netloc

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
            "platform": name,
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

    # Parse captured API responses
    for resp_url, resp_body in captured:
        if tracer is not None:
            try:
                tracer.record(
                    kind="xhr", url=resp_url, parent=None, label="feed-scroll",
                )
            except Exception:
                pass
        try:
            data = json_mod.loads(resp_body)
            posts = _extract_posts_from_api(data, resp_url, name)
            messages.extend(posts)
        except json_mod.JSONDecodeError:
            pass

    return messages


# ── Instagram / generic GraphQL post extractor ───────────────────────

def _extract_posts_from_api(data: dict, url: str, platform: str) -> list[dict]:
    """Extract individual posts from a feed API response.

    Handles Instagram GraphQL timeline responses and other common
    feed structures. Returns one message dict per post.
    """
    posts = []
    data_inner = data.get("data", data)

    # Walk through all values looking for edges/items with media+caption
    def _walk(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return

        # Instagram timeline: edges → node → media → caption
        edges = obj.get("edges", [])
        if isinstance(edges, list):
            for edge in edges:
                node = edge.get("node", {}) if isinstance(edge, dict) else {}
                media = node.get("media") or {}
                if not isinstance(media, dict):
                    # Try explore_story.media
                    es = node.get("explore_story", {})
                    media = es.get("media", {}) if isinstance(es, dict) else {}

                if not isinstance(media, dict) or not media:
                    continue

                user = media.get("user", {})
                username = user.get("username", "?") if isinstance(user, dict) else "?"

                caption = media.get("caption")
                text = ""
                if isinstance(caption, dict):
                    text = caption.get("text", "")
                elif isinstance(caption, str):
                    text = caption

                taken_at = media.get("taken_at", "")
                media_id = str(media.get("id", media.get("pk", "")))

                if text and len(text) > 10:
                    ts = ""
                    if taken_at:
                        try:
                            ts = datetime.fromtimestamp(int(taken_at), tz=timezone.utc).isoformat()
                        except (ValueError, OSError):
                            pass

                    posts.append({
                        "platform": platform,
                        "id": f"ig:{media_id}" if media_id else f"ig:{hash(text[:50])}",
                        "timestamp": ts or datetime.now(timezone.utc).isoformat(),
                        "author": f"@{username}",
                        "channel": "instagram-feed",
                        "content": text[:2000],
                    })

        # Also check items arrays (some APIs use items instead of edges)
        items = obj.get("items", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                caption = item.get("caption")
                text = ""
                if isinstance(caption, dict):
                    text = caption.get("text", "")
                user = item.get("user", {})
                username = user.get("username", "?") if isinstance(user, dict) else "?"
                media_id = str(item.get("id", item.get("pk", "")))

                if text and len(text) > 10:
                    posts.append({
                        "platform": platform,
                        "id": f"ig:{media_id}" if media_id else f"ig:{hash(text[:50])}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "author": f"@{username}",
                        "channel": "instagram-feed",
                        "content": text[:2000],
                    })

        # Recurse into dict values
        for v in obj.values():
            if isinstance(v, dict):
                _walk(v, depth + 1)

    _walk(data_inner)
    return posts
