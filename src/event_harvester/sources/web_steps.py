"""A route a source can carry, and the engine that replays it.

`EH-STD-WEBSRC-v0` can hold a *destination* — a URL — because every one of its
ten keys is a scalar. That is why erobay is "crystallised" and Luma, Meetup and
Eventbrite are not: erobay's exploration output happened to fit in a URL field,
and theirs is a *route*. There was nowhere to put one.

This module is that field, plus the engine that walks it.

Two properties are non-negotiable:

* **Replay is deterministic and LLM-free.** An agent authors a route once
  (see :mod:`event_harvester.sources.web_explore`); every harvest afterwards
  replays it with no model in the loop. Paying for inference per source per run
  is how a scraper becomes too expensive to run daily.
* **Steps address *roles*, not selectors.** ``{"verb": "fill", "target":
  "search"}`` resolves through ``web_primitives.find_search_box`` at replay
  time, so a site that moves its search box keeps working. A raw selector is
  available as ``"selector:<css>"`` for when nothing else fits, and it is the
  brittle option on purpose — routes that use it are the ones that rot first.

The vocabulary is deliberately small. Every verb here maps to exactly one L1
primitive, and adding a verb means adding a primitive first.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from event_harvester import web_primitives as W
from event_harvester.observability.metrics import count, record
from event_harvester.observability.tracing import (
    current_span_event,
    fail_current_span,
    get_tracer,
    set_span_attrs,
    traced,
)

logger = logging.getLogger("event_harvester.web_steps")
_TRACER = "event_harvester.sources.web_steps"


# verb -> required keys. Optional keys are validated by _OPTIONAL below.
STEP_VERBS: dict[str, tuple[str, ...]] = {
    "goto": ("url",),
    "dismiss": (),
    "fill": ("target", "text"),
    "click": ("target",),
    "scroll": (),
    "wait": (),
    "extract": (),
}

_OPTIONAL: dict[str, tuple[str, ...]] = {
    "goto": ("wait_ms",),
    "dismiss": (),
    "fill": ("submit", "wait_ms", "optional"),
    "click": ("repeat", "wait_ms", "optional"),
    "scroll": ("seconds", "target", "step_ms"),
    "wait": ("ms",),
    "extract": ("what", "optional"),
}

# Role names a `target` may take, each resolving through an L1 find_* verb.
# `selector:<css>` is handled separately and bypasses discovery entirely.
TARGET_ROLES: tuple[str, ...] = ("search", "pagination", "modal", "feed", "chat")

EXTRACT_KINDS: tuple[str, ...] = ("links", "text", "feed_items")

MAX_STEPS = 40
MAX_REPEAT = 20


def validate_steps(steps: Any) -> list[str]:
    """Return a list of human-readable problems with ``steps``. Empty means valid.

    Pure and browser-free, so a route can be checked when it is written rather
    than when it is next run — which for a nightly harvest is the difference
    between finding out now and finding out tomorrow.
    """
    errors: list[str] = []
    if not isinstance(steps, list):
        return ["steps must be a list"]
    if len(steps) > MAX_STEPS:
        errors.append(f"too many steps ({len(steps)} > {MAX_STEPS})")

    for i, step in enumerate(steps):
        where = f"step {i}"
        if not isinstance(step, dict):
            errors.append(f"{where}: must be an object, got {type(step).__name__}")
            continue
        verb = step.get("verb")
        if verb not in STEP_VERBS:
            known = ", ".join(sorted(STEP_VERBS))
            errors.append(f"{where}: unknown verb {verb!r} (known: {known})")
            continue

        for key in STEP_VERBS[verb]:
            if key not in step or step[key] in (None, ""):
                errors.append(f"{where} ({verb}): missing required key {key!r}")

        allowed = {"verb", *STEP_VERBS[verb], *_OPTIONAL[verb]}
        for key in step:
            if key not in allowed:
                errors.append(f"{where} ({verb}): unexpected key {key!r}")

        target = step.get("target")
        if target is not None and verb in ("fill", "click", "scroll"):
            if not isinstance(target, str):
                errors.append(f"{where} ({verb}): target must be a string")
            elif not target.startswith("selector:") and target not in TARGET_ROLES:
                errors.append(
                    f"{where} ({verb}): target {target!r} is neither a known role "
                    f"({', '.join(TARGET_ROLES)}) nor a 'selector:<css>' escape hatch"
                )

        what = step.get("what")
        if what is not None and what not in EXTRACT_KINDS:
            errors.append(f"{where} (extract): what must be one of {', '.join(EXTRACT_KINDS)}")

        repeat = step.get("repeat")
        if repeat is not None:
            if not isinstance(repeat, int) or repeat < 1:
                errors.append(f"{where} ({verb}): repeat must be a positive integer")
            elif repeat > MAX_REPEAT:
                errors.append(f"{where} ({verb}): repeat {repeat} exceeds MAX_REPEAT={MAX_REPEAT}")

        seconds = step.get("seconds")
        if seconds is not None and (not isinstance(seconds, (int, float)) or seconds < 0):
            errors.append(f"{where} (scroll): seconds must be a non-negative number")

    return errors


@dataclass
class StepResult:
    index: int
    verb: str
    ok: bool
    detail: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "verb": self.verb, "ok": self.ok,
            "detail": self.detail, "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class ReplayResult:
    """What a route produced, and where it stopped producing it."""

    ok: bool
    results: list[StepResult] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    diagnosis: dict[str, Any] | None = None

    @property
    def failed_step(self) -> StepResult | None:
        return next((r for r in self.results if not r.ok), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": [r.to_dict() for r in self.results],
            "links": self.links,
            "texts": self.texts,
            "diagnosis": self.diagnosis,
            "failed_step": self.failed_step.to_dict() if self.failed_step else None,
        }


def resolve_target(uc, page, target: str | None) -> W.Element | None:
    """Turn a step's ``target`` into an :class:`Element`, or ``None``.

    ``selector:<css>`` short-circuits discovery and trusts the caller. Every
    other role goes through the matching L1 ``find_*`` verb, which is what
    lets a route survive a site moving its own furniture around.
    """
    if not target:
        return None
    if target.startswith("selector:"):
        css = target.split(":", 1)[1]
        return W.Element(selector=css, bbox=None, confidence=1.0, label="explicit")
    if target == "search":
        return W.find_search_box(uc, page)
    if target == "pagination":
        return W.find_pagination(page)
    if target == "modal":
        return W.find_modal(uc, page)
    if target == "feed":
        return W.find_feed_container(uc, page)
    if target == "chat":
        return W.find_chat_input(uc, page)
    return None


def _run_one(uc, page, step: dict[str, Any], out: ReplayResult) -> tuple[bool, str]:
    """Execute a single step. Returns ``(ok, detail)``."""
    verb = step["verb"]

    if verb == "goto":
        page.goto(step["url"])
        wait_ms = int(step.get("wait_ms", 1500))
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        return True, step["url"]

    if verb == "dismiss":
        closed = W.dismiss_modal(uc, page)
        # Nothing to dismiss is a normal outcome, not a failure: routes keep a
        # dismiss step because the banner shows up *sometimes*.
        return True, "closed" if closed else "nothing to dismiss"

    if verb == "wait":
        ms = int(step.get("ms", 2000))
        page.wait_for_timeout(ms)
        return True, f"{ms}ms"

    if verb == "scroll":
        element = resolve_target(uc, page, step.get("target"))
        ticks = W.scroll(page, element, int(step.get("seconds", 8)),
                         step_ms=int(step.get("step_ms", 800)))
        return True, f"{ticks} tick(s)"

    if verb == "fill":
        element = resolve_target(uc, page, step["target"])
        if element is None:
            return False, f"target {step['target']!r} not found on the page"
        ok = W.fill_input(page, element, str(step["text"]), submit=bool(step.get("submit", True)))
        if ok:
            page.wait_for_timeout(int(step.get("wait_ms", 2500)))
        return ok, f"{step['target']} <- {str(step['text'])[:40]!r}"

    if verb == "click":
        repeat = int(step.get("repeat", 1))
        clicked = 0
        for _ in range(min(repeat, MAX_REPEAT)):
            element = resolve_target(uc, page, step["target"])
            if element is None:
                break
            if not W.click(page, element):
                break
            clicked += 1
            page.wait_for_timeout(int(step.get("wait_ms", 2000)))
        if clicked == 0:
            return False, f"target {step['target']!r} not found or not clickable"
        return True, f"clicked {clicked}/{repeat}"

    if verb == "extract":
        what = step.get("what", "links")
        if what == "links":
            found = W.extract_links(page)
            seen = {lnk.get("url") for lnk in out.links}
            fresh = [lnk for lnk in found if lnk.get("url") and lnk.get("url") not in seen]
            out.links.extend(fresh)
            return True, f"{len(fresh)} new link(s), {len(out.links)} total"
        if what == "text":
            text = W.extract_text(page)
            if text:
                out.texts.append(text)
            return True, f"{len(text)} char(s)"
        if what == "feed_items":
            try:
                items = uc.get_feed_items(page) or []
            except Exception as e:  # noqa: BLE001
                return False, f"get_feed_items failed: {e}"
            out.texts.extend(str(i) for i in items if i)
            return True, f"{len(items)} item(s)"
        return False, f"unknown extract kind {what!r}"

    return False, f"unhandled verb {verb!r}"


@traced("web.route.replay", tracer_name=_TRACER)
def replay(uc, page, steps: list[dict[str, Any]], *, cfg_llm=None,
           diagnose_on_failure: bool = True, source: str = "") -> ReplayResult:
    """Walk ``steps`` against an open page. No LLM in the loop unless it breaks.

    A step marked ``"optional": true`` may fail without ending the run — that is
    for the steps a site only sometimes needs, like dismissing a banner that
    only some regions get. Any other failure stops the walk, because continuing
    a route past a step that did not happen produces results that look real and
    are not.

    When a step fails and ``cfg_llm`` is available, the page is diagnosed on the
    spot (:mod:`event_harvester.observability.hints`). That is the difference
    between "meetup returned 0 events" and "meetup returned 0 events because a
    consent dialog was covering the results".

    ``source`` is only used as a metric/span dimension. It is a low-cardinality
    label by construction — source names come from `web_sources.json`, not from
    URLs — which is what keeps `web.route.yield` groupable.
    """
    out = ReplayResult(ok=True)
    set_span_attrs(**{"route.source": source, "route.length": len(steps)})

    errors = validate_steps(steps)
    if errors:
        out.ok = False
        out.results.append(StepResult(-1, "validate", False, "; ".join(errors[:5])))
        fail_current_span(f"invalid route: {errors[0]}", error_type="invalid_route")
        count("web.route.replays", ok=False, source=source, reason="invalid_route",
              description="Route replays attempted")
        return out

    tracer = get_tracer(_TRACER)
    for i, step in enumerate(steps):
        verb = step["verb"]
        target = step.get("target")
        # One span per step. Without this a route is a single flat span and the
        # only observable fact is that it took nine seconds -- with it, a route
        # is a waterfall and the slow or broken step names itself.
        with tracer.start_as_current_span(f"web.step.{verb}") as span:
            span.set_attribute("step.index", i)
            span.set_attribute("step.verb", verb)
            if target:
                span.set_attribute("step.target", target)
                span.set_attribute("step.target_kind",
                                   "selector" if target.startswith("selector:") else "role")
            if step.get("optional"):
                span.set_attribute("step.optional", True)

            t0 = time.monotonic()
            try:
                ok, detail = _run_one(uc, page, step, out)
            except Exception as e:  # noqa: BLE001 — one bad step must not lose the run
                ok, detail = False, f"{type(e).__name__}: {e}"
            elapsed = int((time.monotonic() - t0) * 1000)

            span.set_attribute("step.ok", ok)
            span.set_attribute("step.detail", detail[:200])
            span.set_attribute("step.links_total", len(out.links))
            if not ok:
                # A handled failure still has to mark the span, or an
                # unsuccessful route renders entirely green.
                fail_current_span(detail[:200], error_type=f"step_{verb}")

        out.results.append(StepResult(i, verb, ok, detail, elapsed))
        record("web.step.duration", elapsed, unit="ms", verb=verb, ok=ok,
               description="Wall-clock duration of one route step")
        count("web.steps.executed", verb=verb, ok=ok, source=source,
              description="Route steps executed")
        logger.info("step %d %s: %s (%s, %dms)", i, verb, "ok" if ok else "FAILED", detail, elapsed)

        if not ok:
            if step.get("optional"):
                current_span_event("step.optional_failure_skipped",
                                   {"step.index": i, "step.verb": verb})
                continue
            out.ok = False
            if diagnose_on_failure:
                try:
                    from event_harvester.observability.hints import diagnose as _diagnose
                    diag = _diagnose(page, cfg_llm, use_vlm="auto" if cfg_llm else False)
                    out.diagnosis = diag.to_dict()
                    current_span_event("route.diagnosed", {
                        "hint.kind": diag.kind,
                        "hint.vlm_used": diag.vlm_used,
                        "hint.summary": diag.summary()[:200],
                    })
                except Exception as e:  # noqa: BLE001
                    logger.debug("replay: diagnosis failed: %s", e)
            break

    failed = out.failed_step
    set_span_attrs(**{
        "route.ok": out.ok,
        "route.steps_run": len(out.results),
        "route.links": len(out.links),
        "route.texts": len(out.texts),
        "route.failed_index": failed.index if failed else -1,
        "route.failed_verb": failed.verb if failed else None,
        "route.diagnosis": (out.diagnosis or {}).get("kind"),
    })
    if not out.ok:
        fail_current_span(
            f"route stopped at step {failed.index} ({failed.verb})" if failed else "route failed",
            error_type="route_incomplete",
        )
    count("web.route.replays", ok=out.ok, source=source,
          reason=(failed.verb if failed else None),
          description="Route replays attempted")
    # The yield histogram is what makes replay-rot detectable across runs: one
    # zero is a quiet week, a step change in the distribution is a broken route.
    record("web.route.yield", len(out.links), unit="{link}", source=source, ok=out.ok,
           description="Event links produced by one route replay")
    return out
