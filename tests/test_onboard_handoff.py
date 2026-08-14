"""The offramp fires only when the cheap pass is genuinely stuck, and hands
over a question rather than a URL.

Two failure modes this guards against, and both are cheaper to prevent than to
notice later:

1. **Firing too often.** An agent invoked on every source is an agent nobody
   can afford, and its output stops being read. A validated iCalendar feed
   settles the question by other means — no agent, however unreadable the
   homepage was.
2. **Handing over nothing.** Passing the original URL throws away the redirect
   chain, the candidate sweep and the feeds already found, so the agent
   rediscovers them slowly and non-deterministically, and arrives with no
   statement of what is actually unknown.
"""

from __future__ import annotations

import json

from event_harvester.sources.onboard import OnboardReport, PageEvidence
from event_harvester.sources.onboard_handoff import (
    REASONS,
    build_brief,
    classify,
    needs_agent,
)


def _report(**kw) -> OnboardReport:
    base = {"given_url": "https://venue.test", "final_url": "https://venue.test"}
    return OnboardReport(**{**base, **kw})


def _blank(url: str = "https://venue.test/a", **kw) -> PageEvidence:
    return PageEvidence(url=url, ok=True, chars=900, **kw)


class TestItFiresOnlyWhenStuck:
    def test_a_confident_result_needs_no_agent(self):
        good = PageEvidence(url="u", ok=True, dated_links=40, chars=9000)
        assert not needs_agent(_report(candidates=(good,), best=good, mode="calendar"))

    def test_no_signal_anywhere_needs_an_agent(self):
        r = _report(candidates=(_blank(),), best=_blank(), needs_browser=True)
        assert needs_agent(r)

    def test_a_validated_feed_settles_it_without_an_agent(self):
        """The question was answered by other means. An unreadable homepage is
        not a reason to spend a model call when the feed is already in hand."""
        r = _report(candidates=(_blank(),), best=_blank(), needs_browser=True,
                    ics_feed="https://venue.test/events.ics")
        assert not needs_agent(r)

    def test_an_already_configured_source_needs_nothing(self):
        r = _report(already_configured="the-stud", needs_browser=True)
        assert not needs_agent(r)

    def test_nothing_fetched_at_all_needs_an_agent(self):
        assert needs_agent(_report(candidates=(), best=None))


class TestItNamesTheKindOfStuck:
    def test_zero_signal_is_not_the_same_as_blocked(self):
        """Different questions. Conflating them produces a brief that asks for
        the wrong thing."""
        assert classify(_report(candidates=(_blank(),), best=_blank(),
                                needs_browser=True)) == "no_signal"
        assert classify(_report(candidates=(_blank(blocked=True),),
                                best=_blank(), needs_browser=True)) == "blocked"

    def test_unreachable_is_its_own_case(self):
        dead = PageEvidence(url="u", ok=False, error="Timeout")
        assert classify(_report(candidates=(dead,), best=None)) == "unreachable"

    def test_every_reason_has_an_explanation(self):
        for report in (
            _report(candidates=(_blank(),), best=_blank(), needs_browser=True),
            _report(candidates=(_blank(blocked=True),), best=_blank(), needs_browser=True),
            _report(candidates=(), best=None),
        ):
            assert REASONS[classify(report)]


class TestTheBriefCarriesWhatWasAlreadyPaidFor:
    def test_it_returns_none_when_not_stuck(self):
        good = PageEvidence(url="u", ok=True, dated_links=12)
        assert build_brief(_report(candidates=(good,), best=good)) is None

    def test_the_candidates_travel_with_it(self):
        """So the agent chooses from a set rather than searching for one."""
        a, b = _blank("https://venue.test/calendar"), _blank("https://venue.test/shows")
        brief = build_brief(_report(candidates=(a, b), best=a, needs_browser=True))
        assert {c["url"] for c in brief.candidates} == {a.url, b.url}

    def test_the_redirect_chain_is_not_rediscovered(self):
        brief = build_brief(_report(
            candidates=(_blank(),), best=_blank(), needs_browser=True,
            redirect_chain=("http://a.test", "https://www.a.test"),
        ))
        assert "redirect_chain" in brief.settled

    def test_feeds_already_found_are_handed_over(self):
        ev = _blank(rss_feeds=("https://venue.test/rss",))
        brief = build_brief(_report(candidates=(ev,), best=ev, needs_browser=True))
        assert brief.settled["rss_feeds_found"] == ["https://venue.test/rss"]

    def test_app_data_becomes_a_hint_not_just_a_flag(self):
        """A page shipping __NEXT_DATA__ usually has a keyless JSON API behind
        it, which beats reading the rendered DOM. That hint is free and the
        agent should not have to find it."""
        ev = _blank(has_next_data=True)
        brief = build_brief(_report(candidates=(ev,), best=ev, needs_browser=True))
        assert "API" in brief.settled["server_rendered_app_data"]


class TestTheQuestionIsAnswerable:
    def test_it_asks_for_one_url_and_a_count(self):
        brief = build_brief(_report(candidates=(_blank(),), best=_blank(),
                                    needs_browser=True))
        assert "MULTIPLE dated" in brief.question
        assert any("how many" in a for a in brief.acceptance)

    def test_none_is_an_allowed_answer(self):
        """Otherwise the agent invents one, which is the failure mode that
        matters — a source yielding garbage costs tokens every night."""
        brief = build_brief(_report(candidates=(_blank(),), best=_blank(),
                                    needs_browser=True))
        assert "'none'" in brief.question

    def test_a_blocked_page_gets_a_different_question(self):
        brief = build_brief(_report(candidates=(_blank(blocked=True),),
                                    best=_blank(), needs_browser=True))
        assert "challenge" in brief.question

    def test_the_agent_is_told_not_to_configure_anything(self):
        """Only a real --dry-run that produces events proves a source. Looking
        at a page cannot substitute, and an agent asked to decide will."""
        brief = build_brief(_report(candidates=(_blank(),), best=_blank(),
                                    needs_browser=True))
        assert "Do not configure" in brief.budget_hint


class TestItTravelsOutOfProcess:
    def test_the_brief_is_json_serialisable(self):
        """The runner may be an MCP tool, a subprocess, or a container with no
        access to this object graph."""
        brief = build_brief(_report(candidates=(_blank(),), best=_blank(),
                                    needs_browser=True))
        json.dumps(brief.to_dict())

    def test_it_renders_as_a_prompt_for_runners_that_take_text(self):
        brief = build_brief(_report(candidates=(_blank("https://v.test/calendar"),),
                                    best=_blank(), needs_browser=True))
        text = brief.to_prompt()
        assert "https://v.test/calendar" in text
        assert "Answer with:" in text
        assert "do not re-derive" in text
