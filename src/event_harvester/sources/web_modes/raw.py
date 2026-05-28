"""Raw mode: just load page and grab text. Last-resort fallback."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from event_harvester.sources.web_utils import (
    DEFAULT_SCROLL_SECONDS,
    _extract_text,
    _scroll_for_seconds,
    _wait_cloudflare,
)
from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.web_fetch")


def _do_raw(uc, src: dict, page=None, tracer=None, capture=None) -> list[dict]:
    """Raw mode (OTel-wrapped). Body lives in ``_do_raw_inner``."""
    name = src.get("name", "web")
    mode = "raw"
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.{mode}") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", mode)
        try:
            messages = _do_raw_inner(uc, src, page=page, tracer=tracer, capture=capture)
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _do_raw_inner(uc, src: dict, page=None, tracer=None, capture=None) -> list[dict]:
    """Raw mode: just load page and grab text."""
    from urllib.parse import urlparse

    url = src["url"]
    name = src.get("name", "web")
    if page is None:
        page = uc.open(url, source_name=name, tracer=tracer, capture=capture)
        html = _wait_cloudflare(page, headless=False)
    else:
        html = page.content()
    uc.dismiss_cookies(page)
    # Scroll so lazy-loaded content lands in the HTML we hand to the extractor.
    _scroll_for_seconds(page, src.get("scroll_seconds", DEFAULT_SCROLL_SECONDS))
    if page is not None:
        try:
            html = page.content()
        except Exception:
            pass
    text = _extract_text(html)
    page.close()

    if len(text) < 50:
        return []

    domain = urlparse(url).netloc
    return [{
        "platform": "web",
        "id": f"web:{hash(url)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": domain,
        "channel": url[:120],
        "content": text[:8000],
    }]
