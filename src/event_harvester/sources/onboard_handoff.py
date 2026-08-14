"""When the cheap reasoning cannot decide, hand an agent a brief — not the URL.

`onboard.explore` answers most of blueprint step 3 for the price of a few HTTP
requests. It cannot answer all of it, and the boundary is sharp rather than
fuzzy: **a page whose events are rendered by JavaScript returns a nav bar and a
cookie banner to `requests`.** Every signal reads zero, and zero is not evidence
of an empty page — only of a blind observer. `explore` already refuses to rank
on that (``OnboardReport.needs_browser``); this module is what happens next.

## Why a brief rather than a URL

Handing an agent the original URL throws away everything already paid for — the
redirect chain, the candidates discovered, the feeds found, the numbers that
came back zero. It would rediscover them, more slowly, non-deterministically,
and at the cost of a model call per step. Worse, it would arrive with no
statement of what is actually unknown, and an agent with no specific question
answers a general one.

So the offramp carries:

* **what is already settled** — do not re-derive it;
* **the exact question**, phrased so the answer is checkable;
* **the candidate set**, so the agent chooses rather than searches;
* **the acceptance test**, because "looks right" is not a finding.

## What the agent is *not* asked to do

Configure anything. Decide a mode. Judge whether the source is worth having.
Those need the pipeline's own evidence (blueprint step 7 — a real `--dry-run`
that produces real events) and no amount of looking at a page substitutes.

The agent answers exactly one question: **which of these URLs shows a list of
dated events to a browser?** That is narrow enough to verify, and narrow is
what makes an agent affordable here — it runs once per source added, not once
per message per night.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from event_harvester.sources.onboard import OnboardReport

#: Why the cheap pass gave up. Each maps to a different question for the agent,
#: and conflating them would produce a brief that asks for the wrong thing.
REASONS = {
    "no_signal": (
        "Every candidate returned zero dated links, zero JSON-LD events and "
        "zero date strings. Consistent with client-side rendering."
    ),
    "blocked": (
        "A bot challenge was served instead of the page. Nothing downstream "
        "saw real content."
    ),
    "unreachable": "The URL could not be fetched at all.",
    "ambiguous": (
        "Two or more candidates scored equally on every signal, so the "
        "ranking has no basis to prefer one."
    ),
}


@dataclass
class AgentBrief:
    """A self-contained task for whatever runs the browser.

    Serialisable on purpose: the runner may be an MCP tool in this process, a
    subprocess, or a container with no access to this object graph.
    """

    reason: str
    question: str
    given_url: str
    candidates: list[dict] = field(default_factory=list)
    settled: dict = field(default_factory=dict)
    acceptance: list[str] = field(default_factory=list)
    budget_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "reason": self.reason,
            "reason_detail": REASONS.get(self.reason, ""),
            "question": self.question,
            "given_url": self.given_url,
            "candidates": self.candidates,
            "settled": self.settled,
            "acceptance": self.acceptance,
            "budget_hint": self.budget_hint,
        }

    def to_prompt(self) -> str:
        """The brief as text, for a runner that takes a prompt rather than JSON."""
        lines = [
            f"Task: {self.question}",
            "",
            f"Starting URL: {self.given_url}",
            f"Why this needs a browser: {REASONS.get(self.reason, self.reason)}",
            "",
            "Already established without a browser — do not re-derive:",
        ]
        for k, v in self.settled.items():
            lines.append(f"  - {k}: {v}")
        lines += ["", "Candidates, best-guess order (all scored zero, hence the task):"]
        for c in self.candidates:
            lines.append(
                f"  - {c['url']}  [{c.get('origin', '?')}] "
                f"{c.get('chars', 0)} chars, {c.get('dated_links', 0)} dated links"
            )
        lines += ["", "Answer with:"]
        lines += [f"  - {a}" for a in self.acceptance]
        if self.budget_hint:
            lines += ["", self.budget_hint]
        return "\n".join(lines)


def needs_agent(report: OnboardReport) -> bool:
    """Is this a case the cheap pass genuinely could not settle?

    Deliberately *not* "did anything go wrong". A source that resolved to a
    validated iCalendar feed with 40 future events needs no agent even though
    its homepage was unreadable — the question was answered by other means.
    """
    if report.already_configured:
        return False
    if report.ics_feed:
        return False
    return bool(report.needs_browser) or report.best is None


def classify(report: OnboardReport) -> str:
    """Which kind of stuck this is. See :data:`REASONS`."""
    if report.best is None or not any(c.ok for c in report.candidates):
        return "unreachable"
    if any(c.blocked for c in report.candidates):
        return "blocked"
    if report.needs_browser:
        return "no_signal"
    return "ambiguous"


def build_brief(report: OnboardReport) -> AgentBrief | None:
    """Turn a stuck report into a task, or ``None`` when nothing is stuck."""
    if not needs_agent(report):
        return None

    reason = classify(report)
    settled: dict = {
        "final_url_after_redirects": report.final_url,
        "candidates_fetched": len(report.candidates),
    }
    if len(report.redirect_chain) > 1:
        settled["redirect_chain"] = " -> ".join(report.redirect_chain)
    if report.warnings:
        settled["warnings_already_raised"] = report.warnings
    feeds = sorted({f for c in report.candidates for f in c.rss_feeds})
    if feeds:
        settled["rss_feeds_found"] = feeds
    if any(c.has_next_data for c in report.candidates):
        # The single most useful hint for the agent, and it is free: a page
        # that ships __NEXT_DATA__ usually has a keyless JSON API behind it,
        # which beats reading the rendered DOM.
        settled["server_rendered_app_data"] = (
            "__NEXT_DATA__/__NUXT__ present — look for an XHR/API endpoint "
            "before scraping the DOM; it is usually cursor-paginated JSON."
        )

    question = (
        "Which of the candidate URLs below shows a list of MULTIPLE dated "
        "events to a real browser? Report the one URL with the most dated "
        "events, and how many you counted. If none of them do, say so — "
        "'none' is a valid and useful answer."
    )
    if reason == "blocked":
        question = (
            "A bot challenge blocked the cheap fetch. Open the candidates in a "
            "real browser session and report which shows a list of multiple "
            "dated events, and whether the challenge resolves on its own."
        )
    elif reason == "unreachable":
        question = (
            "Nothing could be fetched. Determine whether this host serves a "
            "site at all, whether it redirects somewhere useful, and if so "
            "which URL lists multiple dated events."
        )

    return AgentBrief(
        reason=reason,
        question=question,
        given_url=report.given_url,
        candidates=[c.to_dict() for c in report.candidates],
        settled=settled,
        acceptance=[
            "the single URL that lists the most dated events (or 'none')",
            "how many dated events you counted there",
            "whether those events are in the page HTML or arrive from an API "
            "call (name the endpoint if so)",
            "one sentence on why the other candidates lost",
        ],
        budget_hint=(
            "This runs once per source added, not per harvest. Prefer reading "
            "one page well over sampling many. Do not configure anything: the "
            "answer is evidence for a human, and the source is only proven by "
            "a real --dry-run that produces events."
        ),
    )


def brief_to_json(brief: AgentBrief, indent: int = 2) -> str:
    return json.dumps(brief.to_dict(), indent=indent)
