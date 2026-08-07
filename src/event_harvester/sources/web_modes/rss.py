"""RSS mode: fetch an RSS/Atom feed and emit one message per upcoming item.

Sibling of `ics.py` and browser-free for the same reason — the outer dispatch
in `web_fetch.py` still allocates a browser it will not use.

**Why this exists.** Bottom of the Hill publishes a calendar page of ~40 shows
as one 338 KB HTML table. Scraped as `raw` it becomes a single 30,164-character
message, and the LLM returned exactly **one** event from it — *"Calendar and
tickets page; includes merch and newsletter sign-up links"*. Measured
2026-08-07. That is the general failure of `raw` on a dense listing: one blob
in, one event out, however many shows it contains.

The same venue's RSS carries one `<item>` per show with the date, the lineup,
door times and prices already separated. One message per show means the
per-event extraction the pipeline is built for, instead of asking a model to
find forty needles in one haystack.

Two things this mode has to get right that ICS does not:

* **The feed is an archive.** 964 items going back years. Emitting all of them
  would swamp the caps and pay to re-extract history every night, so items are
  filtered to a forward window.
* **The event date is not `pubDate`.** `pubDate` is when the *listing* was
  edited — a show in November can have a pubDate of today. The real date is in
  the item link (`.../20261103.html`), with the title as a fallback.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.web_fetch")

#: How far ahead to keep items. Beyond this is real but not actionable, and
#: every extra item costs an LLM call.
DEFAULT_FORWARD_DAYS = 120

#: How far back. Non-zero so a show earlier today is not dropped by a
#: timezone edge.
DEFAULT_BACK_DAYS = 1

#: `.../20261103.html` — the most reliable date on the item.
_LINK_DATE = re.compile(r"/(\d{4})(\d{2})(\d{2})\d*\.html?\b", re.I)

#: `2026  11/03  :  Sex Mex ...` — the fallback.
_TITLE_DATE = re.compile(r"\b(\d{4})\s+(\d{1,2})\s*/\s*(\d{1,2})\b")

_TAGS = re.compile(r"<[^>]+>")


def _do_rss(uc, src: dict, page=None, tracer=None, capture=None) -> list[dict]:
    """RSS mode (OTel-wrapped). Body in :func:`_do_rss_inner`."""
    name = src.get("name", "rss")
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.rss") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", "rss")
        try:
            messages = _do_rss_inner(src)
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def event_date_from(link: str, title: str) -> date | None:
    """The show's date, from the link if possible, else the title.

    Deliberately **not** `pubDate`: that is when the listing was last edited,
    so a November show carries an August pubDate and every date-based filter
    downstream would be wrong.
    """
    match = _LINK_DATE.search(link or "")
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            pass
    match = _TITLE_DATE.search(title or "")
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            pass
    return None


def clean_description(raw: str, limit: int = 2000) -> str:
    """Unescape entities, drop tags, collapse whitespace."""
    text = html.unescape(raw or "")
    text = text.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:limit]


def _parse_items(payload: bytes) -> list[dict[str, str]]:
    """Pull items out of an RSS or Atom document.

    `xml.etree` is the only parser available here (no `defusedxml`, no
    `feedparser`), so a document declaring a DTD is refused outright rather
    than parsed — that is where entity-expansion attacks live, and no
    legitimate feed needs one.
    """
    import xml.etree.ElementTree as ET

    head = payload[:2048].lstrip().lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ValueError("feed declares a DTD; refusing to parse")

    root = ET.fromstring(payload)
    out: list[dict[str, str]] = []

    def text_of(node, *names) -> str:
        for name in names:
            found = node.find(name)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
            # Atom puts the URL on an attribute rather than in the body.
            found = node.find(f"{{http://www.w3.org/2005/Atom}}{name}")
            if found is not None:
                if (found.text or "").strip():
                    return found.text.strip()
                href = found.get("href")
                if href:
                    return href
        return ""

    nodes = root.iter("item")
    items = list(nodes) or list(root.iter("{http://www.w3.org/2005/Atom}entry"))
    for node in items:
        out.append({
            "title": text_of(node, "title"),
            "description": text_of(node, "description", "summary", "content"),
            "link": text_of(node, "link"),
            "guid": text_of(node, "guid", "id"),
        })
    return out


def _do_rss_inner(src: dict, today: date | None = None) -> list[dict]:
    url = src["url"]
    name = src.get("name", "rss")
    timeout = src.get("timeout_s", 20)
    forward = int(src.get("forward_days", DEFAULT_FORWARD_DAYS))
    back = int(src.get("back_days", DEFAULT_BACK_DAYS))
    today = today or datetime.now().astimezone().date()

    try:
        response = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "event-harvester/1.0 (+RSS)"},
        )
        response.raise_for_status()
    except Exception as e:
        logger.error("%s: rss fetch failed for %s: %s", name, url, e)
        return []

    try:
        items = _parse_items(response.content)
    except Exception as e:
        logger.error("%s: rss parse failed for %s: %s", name, url, e)
        return []

    domain = urlparse(url).netloc
    horizon = today + timedelta(days=forward)
    floor = today - timedelta(days=back)

    messages: list[dict] = []
    undated = skipped = 0

    for item in items:
        title = item["title"]
        if not title:
            continue
        when = event_date_from(item["link"], title)
        if when is None:
            # Keep it: an undated item is more likely a real listing than
            # noise, and the LLM can still read a date out of the text.
            undated += 1
        elif not (floor <= when <= horizon):
            skipped += 1
            continue

        parts = [title]
        if when:
            parts.append(f"Date: {when.isoformat()}")
        if item["link"]:
            parts.append(f"URL: {item['link']}")
        body = clean_description(item["description"])
        if body:
            parts.append(body)

        ident = item["guid"] or item["link"] or title
        messages.append({
            "platform": "web",
            "id": f"rss:{name}:{ident}"[:200],
            "timestamp": (
                datetime.combine(when, datetime.min.time())
                .replace(tzinfo=timezone.utc).isoformat()
                if when else datetime.now(timezone.utc).isoformat()
            ),
            "author": domain,
            "channel": name,
            "content": "\n".join(parts),
        })

    logger.info(
        "%s: rss parsed %d item(s) -> %d in window (%d out of window, %d undated) from %s",
        name, len(items), len(messages), skipped, undated, url,
    )
    return messages
