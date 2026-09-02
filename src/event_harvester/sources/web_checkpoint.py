"""Browser-level MHTML checkpoints — record a page once, replay it forever.

Iterating on a web source's *parser* means loading its page over and over, and
for a Cloudflare-gated, native-Chrome source (erobay) each load is a 30-60s
challenge plus real request traffic. Worse, the page's content moves under you:
a calendar that showed 2026-09-01 events yesterday shows 2026-09-02 today, so a
bug you saw once can't be reproduced. A checkpoint freezes the fetched page to
disk as MHTML (Chromium's single-file web archive, captured over CDP) so every
later iteration re-parses the exact same bytes with **no network load**.

Off by default — set ``EH_WEB_CHECKPOINT``:

* ``record`` — fetch live as normal, then snapshot each opened page to
  ``data/.web_checkpoints/<stem>.html`` (the raw DOM the parsers read), plus a
  best-effort ``<stem>.mhtml`` archive.
* ``replay`` — serve the snapshot from disk instead of the network; no browser,
  no Cloudflare wait, deterministic content. Falls through to a live fetch for
  any URL not yet captured.
* unset / ``off`` — no checkpointing; production behaviour is unchanged.

Keyed by URL (not source name) so a source that opens several pages — e.g. the
Calcium month grid plus its next-month grid — snapshots each independently.
"""

from __future__ import annotations

import email
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_harvester.web_checkpoint")

_DIR = Path("data") / ".web_checkpoints"
_ENV = "EH_WEB_CHECKPOINT"
# Programmatic override (tests, scripts) takes precedence over the env var.
_override: Optional[str] = None


def set_mode(mode: Optional[str]) -> None:
    """Force the checkpoint mode for this process (``None`` restores env)."""
    global _override
    _override = mode


def mode() -> str:
    m = (_override if _override is not None else os.environ.get(_ENV, "off"))
    return (m or "off").strip().lower()


def recording() -> bool:
    return mode() == "record"


def replaying() -> bool:
    return mode() == "replay"


def _stem(url: str) -> str:
    """A readable, collision-resistant filename stem for a URL."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", url).strip("-")[:60]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{slug}_{digest}"


def _find(url: str) -> Optional[Path]:
    """The saved snapshot for ``url``.

    Prefers ``.html`` — the raw serialized DOM the parsers actually consume.
    ``.mhtml`` is a best-effort archive kept alongside, but it is the *rendered*
    DOM: the browser drops ``<noscript>`` server links and inline scripts once JS
    runs, and the Calcium parser needs exactly those. So replay reads ``.html``
    and only falls back to ``.mhtml`` if that's all there is.
    """
    for ext in (".html", ".mhtml"):
        p = _DIR / f"{_stem(url)}{ext}"
        if p.exists():
            return p
    return None


def exists(url: str) -> bool:
    return _find(url) is not None


def record(page, url: str) -> Optional[Path]:
    """Snapshot ``page`` to disk. No-op unless recording. Never raises.

    Primary artifact is ``<stem>.html`` — ``page.content()``, the raw serialized
    DOM the parsers consume (keeps ``<noscript>``/scripts, unlike the rendered
    MHTML). A single-file ``<stem>.mhtml`` archive (CDP ``Page.captureSnapshot``)
    is written alongside when CDP is available, best-effort, for a faithful
    resource-carrying record; replay prefers the ``.html`` (see :func:`_find`).
    """
    if not recording():
        return None
    _DIR.mkdir(parents=True, exist_ok=True)
    try:
        out = _DIR / f"{_stem(url)}.html"
        out.write_text(page.content(), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 -- a failed snapshot must not break a harvest
        logger.warning("checkpoint: could not record %s: %s", url, e)
        return None
    # Best-effort MHTML archive alongside the raw HTML.
    try:
        cdp = page.context.new_cdp_session(page)
        data = cdp.send("Page.captureSnapshot", {"format": "mhtml"})["data"]
        (_DIR / f"{_stem(url)}.mhtml").write_text(data, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug("checkpoint: MHTML archive skipped for %s (%s)", url, e)
    logger.info("checkpoint: recorded %s -> %s", url, out.name)
    return out


def _snapshot_html(p: Path) -> str:
    """The main document HTML from a snapshot file.

    A ``.html`` fallback is the document as-is. An ``.mhtml`` is a MIME archive
    (Chromium's captureSnapshot output): pull its first ``text/html`` part and
    decode the transfer encoding — Chrome does **not** reliably render an MHTML
    loaded over ``file://`` into a queryable DOM, so we inject the decoded HTML
    with ``set_content`` instead. Falls back to the raw text if no HTML part is
    found, so a malformed archive still yields something to parse.
    """
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix == ".html":
        return raw
    try:
        msg = email.message_from_string(raw)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, "replace")
    except Exception as e:  # noqa: BLE001
        logger.debug("checkpoint: MHTML decode failed for %s (%s)", p.name, e)
    return raw


class _SnapshotPage:
    """A stand-in page whose ``content()`` is the snapshot's **raw** HTML.

    Deliberately not a browser page: routing replay through a live page and
    reading ``page.content()`` re-serializes the DOM, which drops the
    ``<noscript>`` links and the ``function PopupWindow`` marker the Calcium
    parser keys on. The handlers we replay (calendar/raw) only read the HTML, so
    a raw stand-in is both faithful and instant — no browser, no network, no
    Cloudflare. Unknown page methods are harmless no-ops so an incidental
    ``close()``/``wait_for_load_state()`` on the replay path doesn't blow up.
    """

    def __init__(self, html: str):
        self._html = html

    def content(self) -> str:
        return self._html

    def close(self) -> None:
        pass

    def wait_for_load_state(self, *a, **k) -> None:
        pass

    def __getattr__(self, _name):
        return lambda *a, **k: None


def replay(uc, url: str, *, source_name: str = "web", tracer=None, capture=None):
    """Replay ``url``'s snapshot instead of fetching it live.

    Returns ``(page, html)`` when replaying and a snapshot exists, else ``None``
    so the caller does its normal live open. The page is a raw-HTML stand-in
    (see :class:`_SnapshotPage`), so the Calcium/calendar parsers see exactly the
    bytes that were captured — no network, no Cloudflare wait, no DOM re-render.
    ``uc`` is accepted for call-site symmetry with the live path; replay needs no
    browser.
    """
    if not replaying():
        return None
    p = _find(url)
    if p is None:
        logger.info("checkpoint: no snapshot for %s — falling through to live", url)
        return None
    html = _snapshot_html(p)
    logger.info("checkpoint: replayed %s from %s (%d bytes)", url, p.name, len(html))
    return _SnapshotPage(html), html
