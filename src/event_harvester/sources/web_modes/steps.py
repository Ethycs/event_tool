"""Steps mode: replay a recorded route, deterministically.

The mode every other mode is a hardcoded special case of. `calendar`, `feed`
and `search` each encode one fixed walk in Python; this one reads the walk from
the source config, so a site that does not match any of the three no longer
needs a new module — it needs a route.

Authoring lives in :mod:`event_harvester.sources.web_explore`. Nothing here
calls an LLM on the happy path; a vision call happens only when a step fails
and there is a diagnosis worth attaching to the failure.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_steps import replay
from event_harvester.sources.web_utils import (
    _fetch_event_details_parallel,
    _filter_known_links,
    _save_link_fingerprint,
)

logger = logging.getLogger("event_harvester.web_fetch")


def _do_steps(uc, src: dict, timeout_ms: int, max_events: int,
              page=None, tracer=None, capture=None) -> list[dict]:
    """Steps mode (OTel-wrapped). Body lives in ``_do_steps_inner``."""
    name = src.get("name", "web")
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.steps") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", "steps")
        try:
            messages = _do_steps_inner(
                uc, src, timeout_ms, max_events, page=page, tracer=tracer, capture=capture,
            )
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _do_steps_inner(uc, src: dict, timeout_ms: int, max_events: int,
                    page=None, tracer=None, capture=None) -> list[dict]:
    url = src["url"]
    name = src.get("name", "web")
    steps = src.get("steps") or []

    if not steps:
        logger.warning("%s: mode=steps but no steps configured", name)
        return []

    if page is None:
        page = uc.open(url, source_name=name, tracer=tracer, capture=capture)

    cfg_llm = None
    try:
        from event_harvester.config import load_config
        cfg_llm = load_config().llm
    except Exception as e:  # noqa: BLE001 — diagnosis is optional, the run is not
        logger.debug("%s: could not load llm config for diagnosis: %s", name, e)

    result = replay(uc, page, steps, cfg_llm=cfg_llm)

    if not result.ok:
        failed = result.failed_step
        where = f"step {failed.index} ({failed.verb}): {failed.detail}" if failed else "unknown"
        # The diagnosis is the whole reason this is not just an error line. A
        # route that stops yielding is indistinguishable from a quiet week
        # unless something says *why* it stopped.
        why = ""
        if result.diagnosis:
            why = f" — {result.diagnosis.get('hints', [{}])[0].get('evidence', '')}"
            kind = result.diagnosis.get("kind")
            if kind:
                why = f" — page looks like: {kind}{why}"
        logger.warning("%s: route failed at %s%s", name, where, why)

    # A partial walk still produced links; keep them rather than throwing away
    # work because the last step of the route broke.
    event_links = _filter_known_links(result.links)[:max_events]
    messages: list[dict] = []
    domain = urlparse(url).netloc

    if event_links:
        logger.info("%s: %d event link(s) from route, fetching...", name, len(event_links))
        fetched = _fetch_event_details_parallel(
            uc._context, uc._stealth_plugin, event_links, timeout_ms,
        )
        for lnk, text in fetched:
            if not text:
                continue
            _save_link_fingerprint(lnk)
            messages.append({
                "platform": "web",
                "id": f"web:{hash(lnk['url'])}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "author": urlparse(lnk["url"]).netloc,
                "channel": lnk["url"][:120],
                "content": text[:5000],
            })

    # Text collected by `extract: text` steps is content in its own right —
    # a route that reads an agenda page directly never produces links.
    for text in result.texts:
        if len(text) < 50:
            continue
        messages.append({
            "platform": "web",
            "id": f"web:{hash(text[:200])}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "author": domain,
            "channel": f"{name}-steps",
            "content": text[:8000],
        })

    try:
        page.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("%s: page close failed: %s", name, e)

    expected = src.get("expect_min_events")
    if expected is not None and len(messages) < int(expected):
        # Replay-rot tripwire: a stale selector yields zero, which reads exactly
        # like a quiet week. Only the source's own history can tell them apart.
        logger.warning(
            "%s: route yielded %d message(s), below expect_min_events=%s — "
            "the route may have rotted", name, len(messages), expected,
        )

    return messages
