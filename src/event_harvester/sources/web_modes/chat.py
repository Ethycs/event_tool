"""Chat mode: query an AI chatbot and extract events from its response.

Thin wrapper — the heavy lifting (anchor-locked response wait, scan-diff
fallbacks) lives in ``UCBrowser.chat`` (now in the ext/universal_controller
package).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.web_fetch")


def _do_chat(uc, src: dict, tracer=None, capture=None) -> list[dict]:
    """Chat mode (OTel-wrapped). Body lives in ``_do_chat_inner``."""
    name = src.get("name", "chat")
    mode = "chat"
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.{mode}") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", mode)
        try:
            messages = _do_chat_inner(uc, src, tracer=tracer, capture=capture)
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _do_chat_inner(uc, src: dict, tracer=None, capture=None) -> list[dict]:
    """Chat mode: query an AI chatbot and extract events from its response."""
    from urllib.parse import urlparse

    url = src["url"]
    query = src.get("query", "What events are happening this week? List each with date, time, location, and link.")
    timeout_s = src.get("timeout_s", 45)
    name = src.get("name", "chat")

    logger.info("Chat: %s (query: %s...)", name, query[:60])

    page = uc.open(url, wait_ms=5000, source_name=name, tracer=tracer, capture=capture)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    if uc.has_login_wall(page):
        logger.warning("%s: login wall — run --web-login first", name)
        page.close()
        return []

    response = uc.chat(page, query, timeout_s=timeout_s)
    page.close()

    if not response or len(response) < 50:
        logger.warning("%s: no usable response (%d chars)", name, len(response or ""))
        return []

    logger.info("%s: got %d chars response", name, len(response))
    domain = urlparse(url).netloc

    return [{
        "platform": "web",
        "id": f"chat:{name}:{hash(query)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": f"chat:{name}",
        "channel": f"chat-{name}",
        "content": response[:8000],
    }]
