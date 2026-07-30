"""L1 primitive verbs for web traversal.

A small, fixed vocabulary that every ``_do_*`` mode (and later, a DSPy-
composed LLM planner) reaches into the page through. Wraps UC's
heuristic detectors and Playwright actions in typed signatures so no
selector strings or ``page.evaluate`` snippets leak across the
``_do_*`` boundary.

Two contracts:

1. Callers consume :class:`Element` objects, never raw selectors. Action
   verbs accept an ``Element`` (or ``None`` where it makes sense) and
   return a small bool/int result.
2. ``find_*`` verbs handle their own confidence threshold + (eventually)
   VLM-arbiter fallback internally; callers just check for ``None``.

The VLM fallback is intentionally stubbed for v1 — see
``CONFIDENCE_THRESHOLD`` and the TODO in :func:`_arbitrate`. Hook in
``event_harvester.llm.vision_completion`` once the rest of the
vocabulary is settled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from event_harvester.observability.tracing import set_span_attrs, traced

logger = logging.getLogger(__name__)
_TRACER = "event_harvester.web_primitives"


# Confidence below this triggers the VLM arbiter (when wired); above it,
# the UC classifier's pick is accepted as-is. Tunable per call site.
CONFIDENCE_THRESHOLD = 0.5


@dataclass
class Element:
    """An interactive region on the page.

    Returned by ``find_*``, consumed by action and extract verbs.

    Attributes:
        selector: CSS-or-XPath string Playwright can act on.
        bbox: Optional bounding rect ``(x, y, w, h)`` for the VLM
            arbiter; ``None`` when UC didn't surface coordinates.
        confidence: UC classifier score in ``[0, 1]``; ``1.0`` for
            deterministic (non-classifier) sources.
        label: The primitive name that surfaced this element, e.g.
            ``"search"``, ``"event_card"``, ``"pagination"``. Useful for
            logging and for VLM prompts.
        meta: Extra source-specific fields — title/date/url for
            event cards, raw UC pattern dict for everything else.
    """

    selector: str
    bbox: tuple[int, int, int, int] | None
    confidence: float
    label: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "bbox": list(self.bbox) if self.bbox else None,
            "confidence": self.confidence,
            "label": self.label,
            "meta": dict(self.meta),
        }


# ── Internal helpers ──────────────────────────────────────────────


def _bbox_from_pattern(pat: dict) -> tuple[int, int, int, int] | None:
    """Pull a bbox out of a UC pattern dict if present.

    UC returns coordinates inconsistently across pattern types; tolerate
    the common shapes and skip silently when missing.
    """
    rect = pat.get("rect") or pat.get("bbox") or pat.get("bounds")
    if not rect:
        return None
    if isinstance(rect, dict):
        x = int(rect.get("x", rect.get("left", 0)))
        y = int(rect.get("y", rect.get("top", 0)))
        w = int(rect.get("w", rect.get("width", 0)))
        h = int(rect.get("h", rect.get("height", 0)))
        return (x, y, w, h)
    if isinstance(rect, (list, tuple)) and len(rect) >= 4:
        return tuple(int(v) for v in rect[:4])  # type: ignore[return-value]
    return None


def _arbitrate(page, candidates: list[Element], goal: str) -> Element | None:
    """Return the best candidate. VLM-arbiter hook for v2.

    For v1 this just returns the highest-confidence candidate, or
    ``None`` if the list is empty. When we wire the VLM arbiter, this
    is the single place to do it: take the screenshot, prompt the
    vision LLM with ``goal`` + ``candidates``, return its pick.

    Every ``find_*`` verb funnels through here, so annotating the span once
    here gives all of them the same shape: how many candidates the detectors
    surfaced, what was picked, and how confident it was. That trail is the
    raw material for crystallising a route -- a step whose confidence sits
    near :data:`CONFIDENCE_THRESHOLD` is the one that will break first when
    the page changes.
    """
    set_span_attrs(**{
        "web.arbitrate.goal": goal,
        "web.arbitrate.candidates": len(candidates),
    })
    if not candidates:
        set_span_attrs(**{"web.found": False})
        return None
    best = max(candidates, key=lambda c: c.confidence)
    set_span_attrs(**{
        "web.found": True,
        "web.element.selector": best.selector,
        "web.element.label": best.label,
        "web.element.confidence": round(float(best.confidence), 3),
        "web.element.low_confidence": best.confidence < CONFIDENCE_THRESHOLD,
    })
    # TODO(phase-2): if best.confidence < CONFIDENCE_THRESHOLD, call
    # event_harvester.llm.vision_completion with a screenshot + the
    # candidate list and pick from there.
    return best


def _pattern_to_element(pat: dict, label: str) -> Element | None:
    """Convert one UC pattern dict into an :class:`Element`.

    Returns ``None`` if the pattern has no selector — callers can't act
    on a selectorless candidate even if UC scored it.
    """
    selector = pat.get("selector")
    if not selector:
        return None
    return Element(
        selector=selector,
        bbox=_bbox_from_pattern(pat),
        confidence=float(pat.get("score", pat.get("confidence", 1.0))),
        label=label,
        meta={k: v for k, v in pat.items() if k not in {"selector", "rect", "bbox", "bounds"}},
    )


def _patterns_to_elements(patterns: list[dict] | None, label: str) -> list[Element]:
    if not patterns:
        return []
    return [el for el in (_pattern_to_element(p, label) for p in patterns) if el is not None]


# ── Discovery (read-only) ─────────────────────────────────────────


@traced("web.verb.find_search_box", tracer_name=_TRACER)
def find_search_box(uc, page) -> Element | None:
    """Locate the page's primary search input.

    Tries UC's "search" pattern first; falls back to UC's generic
    input discovery. Both paths go through ``_arbitrate`` so when the
    VLM hook lands a single fallback covers either signal.
    """
    candidates: list[Element] = []
    try:
        patterns = uc.get_patterns(page) or {}
        candidates.extend(_patterns_to_elements(patterns.get("search"), "search"))
    except Exception as e:  # noqa: BLE001 — UC is duck-typed
        logger.debug("find_search_box: get_patterns failed: %s", e)
    if not candidates:
        try:
            inputs = uc.find_inputs(page) or []
            candidates.extend(_patterns_to_elements(inputs, "search"))
        except Exception as e:  # noqa: BLE001
            logger.debug("find_search_box: find_inputs failed: %s", e)
    return _arbitrate(page, candidates, goal="primary search input")


@traced("web.verb.find_event_cards", tracer_name=_TRACER)
def find_event_cards(page) -> list[Element]:
    """Return event-card-shaped links on the current page.

    Wraps the existing ``_extract_event_links`` walker for v1. Each
    returned :class:`Element` carries the link dict (title/date/url) in
    ``meta`` so :func:`extract_event` can read it without a second pass.
    """
    from event_harvester.sources.web_fetch import _extract_event_links

    try:
        links = _extract_event_links(page) or []
    except Exception as e:  # noqa: BLE001
        logger.debug("find_event_cards: _extract_event_links failed: %s", e)
        return []
    out: list[Element] = []
    for link in links:
        url = link.get("url") or link.get("href")
        if not url:
            continue
        out.append(Element(
            selector=link.get("selector") or f"a[href='{url}']",
            bbox=None,
            confidence=1.0,
            label="event_card",
            meta=dict(link),
        ))
    return out


@traced("web.verb.find_pagination", tracer_name=_TRACER)
def find_pagination(page) -> Element | None:
    """Locate a "Next page" control.

    No dedicated UC pattern yet; uses an inline JS heuristic that scores
    anchors/buttons by their text + aria-label against a "next" lexicon.
    Returns the highest-scored candidate as an :class:`Element` or
    ``None`` if nothing matched.
    """
    try:
        hit = page.evaluate(r"""() => {
            const re = /\b(next|more|load\s*more|see\s*more|>>?)\b/i;
            let best = null;
            for (const el of document.querySelectorAll('a, button')) {
                const text = ((el.textContent || '') + ' '
                    + (el.getAttribute('aria-label') || '') + ' '
                    + (el.getAttribute('title') || '')).trim();
                if (!re.test(text)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                let sel = '';
                if (el.id) sel = '#' + el.id;
                else if (el.getAttribute('aria-label'))
                    sel = el.tagName.toLowerCase()
                        + '[aria-label="'
                        + el.getAttribute('aria-label').replace(/"/g, '\\"') + '"]';
                else continue;
                const score = re.test(text.toLowerCase()) ? 0.8 : 0.5;
                if (!best || score > best.score) {
                    best = {selector: sel, score,
                            rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}};
                }
            }
            return best;
        }""")
    except Exception as e:  # noqa: BLE001
        logger.debug("find_pagination: page.evaluate failed: %s", e)
        return None
    if not hit:
        return None
    return _pattern_to_element(dict(hit, **{"selector": hit["selector"]}), "pagination")


@traced("web.verb.find_login_wall", tracer_name=_TRACER)
def find_login_wall(uc, page) -> Element | None:
    """Return a synthetic :class:`Element` flagging a login wall.

    UC's ``has_login_wall`` returns a bool today, so the returned
    element has no selector to act on — it's a signal for callers to
    abort or trigger a re-auth flow.
    """
    try:
        if not uc.has_login_wall(page):
            return None
    except Exception as e:  # noqa: BLE001
        logger.debug("find_login_wall: has_login_wall failed: %s", e)
        return None
    return Element(
        selector="",
        bbox=None,
        confidence=1.0,
        label="login_wall",
        meta={"signal_only": True},
    )


@traced("web.verb.find_modal", tracer_name=_TRACER)
def find_modal(uc, page) -> Element | None:
    """Locate a blocking modal (cookie banner, popup, generic dialog)."""
    candidates: list[Element] = []
    try:
        patterns = uc.get_patterns(page) or {}
        for kind in ("modal", "cookie", "dialog"):
            candidates.extend(_patterns_to_elements(patterns.get(kind), kind))
    except Exception as e:  # noqa: BLE001
        logger.debug("find_modal: get_patterns failed: %s", e)
    return _arbitrate(page, candidates, goal="blocking modal")


@traced("web.verb.find_feed_container", tracer_name=_TRACER)
def find_feed_container(uc, page) -> Element | None:
    """Locate the scrollable feed container UC detected, if any."""
    try:
        patterns = uc.get_patterns(page) or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("find_feed_container: get_patterns failed: %s", e)
        return None
    return _arbitrate(page, _patterns_to_elements(patterns.get("feed"), "feed"),
                      goal="primary feed container")


@traced("web.verb.find_chat_input", tracer_name=_TRACER)
def find_chat_input(uc, page) -> Element | None:
    """Locate a chat input (Grok/ChatGPT style)."""
    candidates: list[Element] = []
    try:
        ml_hit = uc.ml_find_chat(page)
        if ml_hit:
            el = _pattern_to_element(ml_hit, "chat")
            if el is not None:
                candidates.append(el)
    except Exception as e:  # noqa: BLE001
        logger.debug("find_chat_input: ml_find_chat failed: %s", e)
    if not candidates:
        try:
            patterns = uc.get_patterns(page) or {}
            candidates.extend(_patterns_to_elements(patterns.get("chat"), "chat"))
        except Exception as e:  # noqa: BLE001
            logger.debug("find_chat_input: get_patterns failed: %s", e)
    return _arbitrate(page, candidates, goal="chat composer input")


# ── Action ────────────────────────────────────────────────────────


@traced("web.verb.click", tracer_name=_TRACER)
def click(page, element: Element) -> bool:
    """Click ``element``. Returns ``True`` on success, ``False`` on any error."""
    if not element.selector:
        logger.debug("click: %s element has no selector", element.label)
        return False
    try:
        page.click(element.selector)
    except Exception as e:  # noqa: BLE001
        logger.debug("click(%s) failed: %s", element.selector, e)
        return False
    return True


@traced("web.verb.fill_input", tracer_name=_TRACER)
def fill_input(page, element: Element, text: str, *, submit: bool = False) -> bool:
    """Fill ``element`` with ``text``. Optionally press Enter to submit."""
    if not element.selector:
        return False
    try:
        page.fill(element.selector, text)
        if submit:
            page.press(element.selector, "Enter")
    except Exception as e:  # noqa: BLE001
        logger.debug("fill_input(%s) failed: %s", element.selector, e)
        return False
    return True


@traced("web.verb.scroll", tracer_name=_TRACER)
def scroll(page, element: Element | None, seconds: int, step_ms: int = 800) -> int:
    """Scroll one viewport every ``step_ms`` for ``seconds``.

    When ``element`` is given, scrolls that container; otherwise scrolls
    the window. Returns the number of scroll ticks executed.
    """
    if seconds <= 0:
        return 0
    import time as _time
    end = _time.time() + seconds
    ticks = 0
    while _time.time() < end:
        try:
            if element is not None and element.selector:
                page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (el) el.scrollTop += el.clientHeight * 2;
                        else window.scrollBy(0, window.innerHeight * 2);
                    }""",
                    element.selector,
                )
            else:
                page.evaluate("window.scrollBy(0, window.innerHeight)")
        except Exception as e:  # noqa: BLE001
            logger.debug("scroll: page.evaluate failed: %s", e)
            return ticks
        page.wait_for_timeout(step_ms)
        ticks += 1
    return ticks


@traced("web.verb.dismiss_modal", tracer_name=_TRACER)
def dismiss_modal(uc, page) -> bool:
    """Dismiss any visible cookie banner or modal. Returns ``True`` if anything closed."""
    closed = False
    try:
        if uc.dismiss_cookies(page):
            closed = True
    except Exception as e:  # noqa: BLE001
        logger.debug("dismiss_modal: dismiss_cookies failed: %s", e)
    try:
        if uc.close_modal(page):
            closed = True
    except Exception as e:  # noqa: BLE001
        logger.debug("dismiss_modal: close_modal failed: %s", e)
    return closed


@traced("web.verb.wait_for_navigation", tracer_name=_TRACER)
def wait_for_navigation(page, timeout_ms: int = 5000) -> bool:
    """Wait for the page to reach a quiescent state. Returns ``True`` on success."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception as e:  # noqa: BLE001
        logger.debug("wait_for_navigation timed out or failed: %s", e)
        return False
    return True


# ── Extraction ────────────────────────────────────────────────────


@traced("web.verb.extract_text", tracer_name=_TRACER)
def extract_text(page, element: Element | None = None) -> str:
    """Read text content from ``element`` (or the whole page body)."""
    try:
        if element is not None and element.selector:
            return page.evaluate(
                "(sel) => { const el = document.querySelector(sel); return el ? el.innerText : ''; }",
                element.selector,
            ) or ""
        return page.evaluate("document.body.innerText") or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_text failed: %s", e)
        return ""


@traced("web.verb.extract_event", tracer_name=_TRACER)
def extract_event(element: Element) -> dict[str, Any]:
    """Pull structured event fields from a card :class:`Element`.

    Reads from ``element.meta`` which carries the link dict produced by
    ``_extract_event_links`` / the calcium parser. When fields are
    missing, returns them as ``None`` rather than raising.
    """
    meta = element.meta or {}
    return {
        "title": meta.get("title") or meta.get("text"),
        "date": meta.get("date"),
        "time": meta.get("time"),
        "location": meta.get("location") or meta.get("venue"),
        "link": meta.get("url") or meta.get("href"),
    }


@traced("web.verb.extract_links", tracer_name=_TRACER)
def extract_links(page) -> list[dict[str, Any]]:
    """Return raw event link dicts from the page (pre-Element)."""
    from event_harvester.sources.web_fetch import _extract_event_links

    try:
        return _extract_event_links(page) or []
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_links failed: %s", e)
        return []


@traced("web.verb.screenshot", tracer_name=_TRACER)
def screenshot(page) -> bytes:
    """Capture the current viewport as PNG bytes. Empty bytes on failure."""
    try:
        return page.screenshot()
    except Exception as e:  # noqa: BLE001
        logger.debug("screenshot failed: %s", e)
        return b""
