"""Live page diagnosis: why is this page not giving us what we asked for?

`nav_quality.py` answers that question *after* a run, from a trace on disk.
This module answers it *now*, against an open page, while there is still
something to do about it.

Two layers, deliberately ordered:

1. :func:`collect_signals` + :func:`rule_hints` — one ``page.evaluate`` and
   pure functions over the result. Free, instant, no LLM, no network. This is
   where most answers come from, because most blocked pages announce
   themselves: a password field, a consent dialog, "Just a moment...".
2. :func:`vlm_hint` — a screenshot and one vision call. Costs money and about
   a second. Reached only when the rules are not confident, or when the caller
   explicitly asks.

That ordering is the whole design. A VLM call per page would be affordable
during authoring and ruinous during a harvest, and the rules resolve the common
cases anyway. The same discipline the feed probe uses for candidate URLs
applies here: do the cheap deterministic thing first and only pay when it
comes back uncertain.

Every hint carries a ``suggested_step`` where one exists — a dict in the
:mod:`event_harvester.sources.web_steps` vocabulary, ready to append to a
route. A diagnosis you cannot act on is just a log line.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from event_harvester.observability.metrics import count, record
from event_harvester.observability.nav_quality import parse_classification
from event_harvester.observability.tracing import (
    current_span_event,
    fail_current_span,
    set_span_attrs,
    traced,
)
from event_harvester.utils import data_path

logger = logging.getLogger(__name__)
_TRACER = "event_harvester.observability.hints"


HINT_KINDS: tuple[str, ...] = (
    "login_wall",
    "cloudflare_challenge",
    "cookie_banner",
    "error",
    "empty",
    "needs_scroll",
    "needs_search",
    "needs_pagination",
    "content",
    "unknown",
)

# Rule confidence at or below which `diagnose(use_vlm="auto")` escalates to a
# screenshot. Tuned so the unambiguous cases (a password field, a Cloudflare
# interstitial) never pay for a vision call, and "the page looks empty and I
# cannot tell why" always does — that is precisely the case a human would
# solve by looking at it.
VLM_ESCALATION_THRESHOLD = 0.6

# Below this many characters of visible text, a page is not showing content.
# Chosen from the shipped sources: the thinnest real calendar page in
# `web_sources.json` renders ~1.5k characters; interstitials sit under 400.
MIN_CONTENT_CHARS = 400

# A page whose scrollable height is this many viewports tall, but which is
# showing few links, is probably lazy-loading rather than empty.
LAZY_LOAD_VIEWPORT_RATIO = 2.0

# At or above this many links, a page is showing a list of things — which is
# what every source here is after. Below it, structural suggestions (scroll,
# search, paginate) are worth making.
HEALTHY_LINKS = 10

HINTS_DIR = data_path(".web_hints")

_LOGIN_RE = re.compile(
    r"\b(sign in|log ?in|create account|continue with (google|apple|facebook))\b", re.I,
)
_CHALLENGE_RE = re.compile(
    r"(just a moment|checking your browser|verify you are human"
    r"|cf-challenge|captcha|are you a robot)", re.I,
)
_CONSENT_RE = re.compile(
    r"\b(accept (all )?cookies|we use cookies"
    r"|cookie (policy|preferences|settings)|consent|gdpr)\b", re.I,
)
_EMPTY_RE = re.compile(
    r"\b(no (results|events|matches)( found)?|nothing (here|found)"
    r"|0 results|try (a )?different)\b", re.I,
)
# Status codes must arrive with error wording attached. A bare `\b404\b` looked
# reasonable and classified lu.ma/discover — a healthy page with 51 event links —
# as `error` at 0.85 confidence, because the page says things like "500 attendees"
# and "1,404 going". Worse than the wrong answer was the confidence: 0.85 sits
# above VLM_ESCALATION_THRESHOLD, so `auto` would never have taken the screenshot
# that disproves it. High-confidence rules must be the unambiguous ones.
_ERROR_RE = re.compile(
    r"("
    r"\b(?:error|http)\s*(?:404|4\d\d|5\d\d)\b"
    r"|\b(?:404|4\d\d|5\d\d)\s*[-—:]?\s*"
    r"(?:error|not found|forbidden|internal server|bad gateway|service unavailable)\b"
    r"|\bpage not found\b"
    r"|\bsomething went wrong\b"
    r"|\binternal server error\b"
    r"|\bservice unavailable\b"
    r"|\bthis (?:site|page) can.?t be reached\b"
    r"|\bserver (?:error|not found)\b"
    r")", re.I,
)

# One page.evaluate, one round-trip. Every field here is cheap to compute and
# is consumed by a rule below; nothing is collected "just in case".
_SIGNALS_JS = r"""() => {
    const txt = (document.body && document.body.innerText) || '';
    const q = (sel) => document.querySelectorAll(sel).length;
    const nextRe = /\b(next|more|load\s*more|see\s*more|show\s*more)\b/i;
    let nextControls = 0;
    for (const el of document.querySelectorAll('a, button')) {
        const label = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim();
        if (!nextRe.test(label)) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) nextControls++;
    }
    let visibleInputs = 0;
    for (const el of document.querySelectorAll('input, textarea')) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) visibleInputs++;
    }
    return {
        url: location.href,
        title: document.title || '',
        text_len: txt.length,
        text_sample: txt.slice(0, 4000),
        links: q('a[href]'),
        inputs: visibleInputs,
        password_inputs: q('input[type="password"]'),
        search_inputs: q('input[type="search"], input[name*="search" i], '
            + 'input[placeholder*="search" i]'),
        forms: q('form'),
        iframes: q('iframe'),
        dialogs: q('dialog[open], [role="dialog"], [aria-modal="true"]'),
        next_controls: nextControls,
        scroll_height: (document.documentElement && document.documentElement.scrollHeight) || 0,
        viewport_height: window.innerHeight || 0,
    };
}"""


@dataclass
class Hint:
    """One actionable observation about the current page.

    ``suggested_step`` is a ``web_steps`` step dict when the hint implies an
    obvious next move, and ``None`` when it does not — a login wall is real
    information even though no click gets past it.
    """

    kind: str
    confidence: float
    evidence: str
    suggested_step: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Diagnosis:
    """What is on the page, why we think so, and what to try next."""

    url: str
    title: str
    hints: list[Hint] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    screenshot: str | None = None
    vlm_used: bool = False
    vlm_explanation: str = ""

    @property
    def top(self) -> Hint | None:
        return self.hints[0] if self.hints else None

    @property
    def kind(self) -> str:
        return self.hints[0].kind if self.hints else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "kind": self.kind,
            "hints": [h.to_dict() for h in self.hints],
            "signals": {k: v for k, v in self.signals.items() if k != "text_sample"},
            "screenshot": self.screenshot,
            "vlm_used": self.vlm_used,
            "vlm_explanation": self.vlm_explanation,
        }

    def summary(self) -> str:
        """One line a human or an agent can read without unpacking the dict."""
        top = self.top
        if top is None:
            return f"{self.url}: no signals"
        action = ""
        if top.suggested_step:
            action = f" → try {top.suggested_step.get('verb')}"
        return f"{top.kind} ({top.confidence:.0%}): {top.evidence}{action}"


@traced("hints.collect_signals", tracer_name=_TRACER)
def collect_signals(page) -> dict[str, Any]:
    """Read cheap structural signals off the page in one round-trip.

    Returns ``{}`` when the page cannot be evaluated — a closed or crashed
    page is a diagnosis failure, not a crash of the caller.
    """
    try:
        signals = page.evaluate(_SIGNALS_JS) or {}
    except Exception as e:  # noqa: BLE001 — page is duck-typed and may be dead
        logger.debug("collect_signals failed: %s", e)
        # Swallowed exceptions are invisible to a trace unless something says
        # so. This one matters: no signals means every downstream verdict is
        # "unknown", and the reason would otherwise be lost.
        fail_current_span(f"page.evaluate failed: {e}", error_type="page_unreadable")
        count("hints.signal_failures", description="Pages that could not be read at all")
        return {}
    signals = dict(signals)
    set_span_attrs(**{
        "page.text_len": int(signals.get("text_len") or 0),
        "page.links": int(signals.get("links") or 0),
        "url.full": str(signals.get("url") or "")[:200],
    })
    return signals


def rule_hints(signals: dict[str, Any]) -> list[Hint]:
    """Turn raw signals into ranked hints. Pure; no page, no network.

    Ordered most-blocking first: there is no point suggesting a scroll when a
    consent dialog is covering the content. Every branch cites the signal it
    fired on, because a hint whose evidence you cannot check is not much better
    than a guess.
    """
    if not signals:
        return [Hint("unknown", 0.0, "no signals could be read from the page")]

    text = str(signals.get("text_sample") or "")
    text_len = int(signals.get("text_len") or 0)
    links = int(signals.get("links") or 0)
    hints: list[Hint] = []

    # Ordered by how completely each condition blocks everything downstream.
    if _CHALLENGE_RE.search(text):
        hints.append(Hint(
            "cloudflare_challenge", 0.95,
            "bot-check text on the page; a browser challenge is in the way",
            {"verb": "wait", "ms": 8000},
        ))
    if int(signals.get("password_inputs") or 0) > 0:
        hints.append(Hint(
            "login_wall", 0.95,
            "a password input is present — this page is behind a sign-in",
            None,
        ))
    elif _LOGIN_RE.search(text) and text_len < MIN_CONTENT_CHARS * 3:
        hints.append(Hint(
            "login_wall", 0.6,
            "sign-in wording with little other content",
            None,
        ))
    if _CONSENT_RE.search(text) or int(signals.get("dialogs") or 0) > 0:
        hints.append(Hint(
            "cookie_banner", 0.8,
            "consent wording or an open dialog is on top of the page",
            {"verb": "dismiss"},
        ))
    if _ERROR_RE.search(text):
        hints.append(Hint(
            "error", 0.85,
            "the page is reporting an error rather than content",
            None,
        ))
    if _EMPTY_RE.search(text):
        hints.append(Hint(
            "empty", 0.85,
            "the page says it has no results",
            None,
        ))

    blocked = bool(hints)

    # Suggestions, not diagnoses: the page is not blocked, it just has not been
    # driven far enough yet. These are the hints that carry a step.
    suggestions: list[Hint] = []
    scroll_h = float(signals.get("scroll_height") or 0)
    view_h = float(signals.get("viewport_height") or 0) or 1.0
    if links < HEALTHY_LINKS and scroll_h > view_h * LAZY_LOAD_VIEWPORT_RATIO:
        suggestions.append(Hint(
            "needs_scroll", 0.7,
            f"only {links} links but the page is {scroll_h / view_h:.1f} "
            "viewports tall — likely lazy-loading",
            {"verb": "scroll", "seconds": 8},
        ))
    if int(signals.get("search_inputs") or 0) > 0 and links < 30:
        suggestions.append(Hint(
            "needs_search", 0.55,
            "a search box is present and few links are showing — the content may be behind a query",
            {"verb": "fill", "target": "search", "text": "", "submit": True},
        ))
    if int(signals.get("next_controls") or 0) > 0:
        suggestions.append(Hint(
            "needs_pagination", 0.5,
            f"{signals.get('next_controls')} next/more control(s) on the page",
            {"verb": "click", "target": "pagination"},
        ))

    # A page that is plainly showing content is a *confident* verdict even when
    # there is more of it to load. Ranking "there is a Load More button" (0.50)
    # above "this page is fine" would drag every healthy paginated page below
    # VLM_ESCALATION_THRESHOLD and buy a screenshot for each one — measured on
    # lu.ma/discover, which has 51 event links and a next control.
    healthy = text_len >= MIN_CONTENT_CHARS and links >= HEALTHY_LINKS
    if not blocked and healthy:
        hints.append(Hint(
            "content", 0.75,
            f"{text_len} characters across {links} links; nothing looks blocked",
            None,
        ))

    hints.sort(key=lambda h: h.confidence, reverse=True)
    suggestions.sort(key=lambda h: h.confidence, reverse=True)
    hints.extend(suggestions)

    if not hints:
        hints.append(Hint(
            "empty", 0.5,
            f"only {text_len} characters of visible text across {links} links, "
            "and nothing on the page explaining why",
            None,
        ))
    return hints


_VLM_PROMPT = """You are looking at a screenshot of a web page that an automated
event scraper just loaded and did not get what it expected from.

First line: classify the page as EXACTLY ONE of
content, login_wall, cloudflare_challenge, cookie_banner, error, empty, unknown.

Then, in one or two sentences, say what a human would click or do next to reach
a list of events. Be specific about what you can actually see — name the button
or region. If nothing on this page leads to events, say so plainly.
"""


@traced("hints.vlm_hint", tracer_name=_TRACER)
def vlm_hint(page, cfg_llm, *, screenshot_path: Path | None = None) -> tuple[str, str, str | None]:
    """Screenshot the page and ask a vision model what is going on.

    Returns ``(classification, explanation, screenshot_path_str)``. On any
    failure returns ``("unknown", <reason>, path-or-None)`` — a diagnosis that
    cannot be produced must not take down the caller that asked for it.
    """
    from event_harvester.web_primitives import screenshot as _screenshot

    png = _screenshot(page)
    if not png:
        return ("unknown", "screenshot failed", None)

    path = screenshot_path
    if path is None:
        HINTS_DIR.mkdir(parents=True, exist_ok=True)
        path = HINTS_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    except OSError as e:
        logger.debug("vlm_hint: could not save screenshot: %s", e)
        return ("unknown", f"could not save screenshot: {e}", None)

    t0 = time.monotonic()
    try:
        response = vision_completion(_VLM_PROMPT, [path], cfg_llm, max_tokens=300)
    except Exception as e:  # noqa: BLE001 — vision is best-effort
        logger.warning("vlm_hint: vision call failed: %s", e)
        fail_current_span(str(e), error_type="vision_call_failed")
        count("hints.vlm_calls", ok=False,
              description="Vision calls made to diagnose a page (cost-bearing)")
        return ("unknown", str(e), str(path))

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    classification, full = parse_classification(response)
    # This counter is the cost signal for the whole hint system. Every increment
    # is a real vision request; a step change here means the escalation gate has
    # stopped doing its job.
    count("hints.vlm_calls", ok=True, classification=classification,
          description="Vision calls made to diagnose a page (cost-bearing)")
    record("hints.vlm_duration", elapsed_ms, unit="ms",
           description="Latency of one page-diagnosis vision call")
    set_span_attrs(**{
        "hint.vlm.classification": classification,
        "hint.vlm.duration_ms": elapsed_ms,
        "hint.vlm.screenshot": str(path),
    })
    return (classification, full, str(path))


@traced("hints.diagnose", tracer_name=_TRACER)
def diagnose(page, cfg_llm=None, *, use_vlm: bool | str = "auto",
             screenshot_path: Path | None = None) -> Diagnosis:
    """Diagnose the current page. Rules always; the VLM only when it earns it.

    ``use_vlm``:
      * ``"auto"`` (default) — escalate only when the best rule hint is at or
        below :data:`VLM_ESCALATION_THRESHOLD`, and only if ``cfg_llm`` is
        given. This is the mode a harvest should use.
      * ``True`` — always take the screenshot. This is the mode an agent
        authoring a route should use; it is looking at one page, on purpose.
      * ``False`` — never. Free and offline.
    """
    signals = collect_signals(page)
    hints = rule_hints(signals)
    diag = Diagnosis(
        url=str(signals.get("url") or ""),
        title=str(signals.get("title") or ""),
        hints=hints,
        signals=signals,
    )

    rule_kind = hints[0].kind if hints else "unknown"
    top_conf = hints[0].confidence if hints else 0.0
    wants_vlm = use_vlm is True or (use_vlm == "auto" and top_conf <= VLM_ESCALATION_THRESHOLD)

    # The escalation decision is the cost lever, so it is recorded as an event
    # whichever way it goes — "why did this page not get a screenshot" is as
    # much an operational question as "why did it".
    current_span_event("hint.escalation_decision", {
        "hint.rule_kind": rule_kind,
        "hint.rule_confidence": round(float(top_conf), 3),
        "hint.mode": str(use_vlm),
        "hint.escalated": bool(wants_vlm and cfg_llm is not None),
        "hint.threshold": VLM_ESCALATION_THRESHOLD,
    })
    count("hints.escalations", escalated=bool(wants_vlm and cfg_llm is not None),
          mode=str(use_vlm), rule_kind=rule_kind,
          description="Diagnosis calls, split by whether they paid for vision")

    if wants_vlm and cfg_llm is not None:
        classification, explanation, shot = vlm_hint(page, cfg_llm, screenshot_path=screenshot_path)
        diag.vlm_used = True
        diag.vlm_explanation = explanation
        diag.screenshot = shot
        if classification != "unknown":
            # The VLM saw the rendered page; the rules only saw the DOM. When
            # they disagree, the picture wins — that asymmetry is the reason
            # for taking it. Rule hints are kept beneath it, not discarded,
            # because their suggested_steps are still the actionable part.
            existing = next((h for h in diag.hints if h.kind == classification), None)
            diag.hints.insert(0, Hint(
                classification, 0.9,
                f"vision: {explanation.strip().splitlines()[-1][:200] if explanation else ''}",
                existing.suggested_step if existing else None,
            ))
            if classification != rule_kind:
                # Disagreement is the interesting case and the one worth tuning
                # on: it is either a rule that needs fixing or a page the DOM
                # genuinely cannot describe.
                current_span_event("hint.vlm_overrode_rules", {
                    "hint.rule_kind": rule_kind,
                    "hint.vlm_kind": classification,
                })
                count("hints.vlm_overrides", rule_kind=rule_kind, vlm_kind=classification,
                      description="Times the screenshot disagreed with the DOM rules")

    set_span_attrs(**{
        "hint.kind": diag.kind,
        "hint.rule_kind": rule_kind,
        "hint.count": len(diag.hints),
        "hint.vlm_used": diag.vlm_used,
        "hint.confidence": round(float(diag.hints[0].confidence), 3) if diag.hints else 0.0,
        "hint.suggested_verb": (diag.top.suggested_step or {}).get("verb") if diag.top else None,
    })
    count("hints.diagnoses", kind=diag.kind, vlm_used=diag.vlm_used,
          description="Page diagnoses produced")
    return diag


def vision_completion(*args, **kwargs):  # pragma: no cover - thin shim
    """Lazy import target, mirroring ``nav_quality.vision_completion``.

    Kept so tests can patch ``hints.vision_completion`` without paying the
    litellm import cost at module load.
    """
    from event_harvester.llm import vision_completion as _vc
    return _vc(*args, **kwargs)
