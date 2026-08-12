"""Tests for Gmail body hydration.

These exist because the pipeline was reading ~8% of every email. `content` was
`subject + snippet`, Gmail caps that snippet at 200 characters, and measured
2026-08-10 over a live 85-message window the median body was 2505 chars. 32 of
those messages reached the reranker with neither a date nor a link and were
dropped for it -- while 4 held an Eventbrite or Luma link sitting just past the
cut.

So the tests are about the two things that decide whether hydration actually
helps: that the **order** of the assembled content survives
`build_prompt`'s 400-char truncation, and that a fetch failure costs the
snippet rather than the message.
"""

from __future__ import annotations

import pytest

from event_harvester.config import GmailConfig
from event_harvester.sources import gmail

PROMPT_WINDOW = gmail._PROMPT_WINDOW  # pinned to build_prompt below


def _msg(mid: str = "m1", subject: str = "Subject", snippet: str = "snip") -> dict:
    return {
        "platform": "gmail",
        "id": mid,
        "content": f"{subject}\n{snippet}",
        "timestamp": "2026-08-10T12:00:00+00:00",
        "author": "someone@example.com",
        "channel": "INBOX",
    }


@pytest.fixture
def cfg():
    return GmailConfig(fetch_bodies=True, body_max_chars=5000)


def _patch_bodies(monkeypatch, bodies: dict[str, str]):
    monkeypatch.setattr(gmail, "fetch_full_bodies", lambda cfg, ids: bodies)


class TestCleanBody:
    """Marketing mail pads its preview line with zero-width characters."""

    def test_invisible_padding_is_removed(self):
        # The exact sequence Gmail preview-padding uses, from a real message.
        got = gmail.clean_body("Blog just dropped ͏​ ͏​ ͏​ ͏​ ͏​ ͏​")
        assert got == "Blog just dropped"

    def test_runs_of_spaces_collapse(self):
        assert gmail.clean_body("a     b") == "a b"

    def test_paragraphs_survive_but_gaps_close(self):
        assert gmail.clean_body("a\n\n\n\n\nb") == "a\n\nb"

    def test_empty_is_empty(self):
        assert gmail.clean_body("") == ""
        assert gmail.clean_body(None) == ""


class TestEventLinks:
    """A link to a ticketing platform is the strongest signal an email has."""

    @pytest.mark.parametrize("url", [
        "https://www.eventbrite.com/e/downtown-first-thursdays-aug-6-2026-tickets-198",
        "https://luma.com/livestream-8-6",
        "https://lu.ma/8ymekijo",
        "https://www.meetup.com/sf-ai/events/12345/",
        "https://partiful.com/e/abc",
        "https://dice.fm/event/xyz",
    ])
    def test_known_platforms_are_found(self, url):
        assert gmail.event_links(f"blah {url} blah") == [url]

    def test_a_url_is_never_truncated(self):
        """Half a URL is not a URL, and the slug carries the date."""
        url = "https://www.eventbrite.com/e/" + "a" * 300 + "-aug-6-2026-tickets-1980043150"
        assert gmail.event_links(url) == [url]

    def test_document_order_is_preserved(self):
        text = "https://lu.ma/second is later than nothing; https://dice.fm/first"
        assert gmail.event_links(text) == ["https://lu.ma/second", "https://dice.fm/first"]

    def test_duplicates_collapse(self):
        text = "https://lu.ma/x and again https://lu.ma/x"
        assert gmail.event_links(text) == ["https://lu.ma/x"]

    def test_the_count_is_bounded(self):
        text = " ".join(f"https://lu.ma/{i}" for i in range(20))
        assert len(gmail.event_links(text, limit=3)) == 3

    def test_trailing_punctuation_is_not_part_of_the_url(self):
        assert gmail.event_links("see https://lu.ma/x.") == ["https://lu.ma/x"]

    def test_ordinary_links_are_ignored(self):
        assert gmail.event_links("https://example.com/news https://github.com/a/b") == []


class TestHydration:
    def test_the_body_replaces_the_snippet(self, cfg, monkeypatch):
        _patch_bodies(monkeypatch, {"m1": "The full body, which is much longer."})
        msgs = [_msg(subject="Invite", snippet="trunc")]
        gmail.hydrate_bodies(cfg, msgs)
        assert "The full body" in msgs[0]["content"]

    def test_the_subject_still_leads(self, cfg, monkeypatch):
        _patch_bodies(monkeypatch, {"m1": "body text"})
        msgs = [_msg(subject="You are invited")]
        gmail.hydrate_bodies(cfg, msgs)
        assert msgs[0]["content"].startswith("You are invited")

    def test_event_links_come_before_the_body(self, cfg, monkeypatch):
        """build_prompt truncates to 400 chars -- a trailing link is unseen."""
        body = ("preamble " * 200) + " https://lu.ma/abc " + ("tail " * 200)
        _patch_bodies(monkeypatch, {"m1": body})
        msgs = [_msg(subject="Invite")]
        gmail.hydrate_bodies(cfg, msgs)
        content = msgs[0]["content"]
        assert "https://lu.ma/abc" in content[:PROMPT_WINDOW], (
            "the link must survive build_prompt's 400-char cut"
        )

    def test_the_prompt_window_lands_on_the_event_not_the_letterhead(
        self, cfg, monkeypatch,
    ):
        """The whole point: decimation centres content on the dates."""
        body = (
            "Unsubscribe | View this email in your browser. " * 40
            + "Doors 7:30 PM on Nov 3 2026 at the Rickshaw Stop with Sex Mex. "
            + "Legal footer, all rights reserved. " * 40
        )
        _patch_bodies(monkeypatch, {"m1": body})
        msgs = [_msg(subject="Show")]
        gmail.hydrate_bodies(cfg, msgs)
        window = msgs[0]["content"][:PROMPT_WINDOW]
        assert "Nov 3" in window and "7:30" in window

    def test_content_respects_the_configured_ceiling(self, monkeypatch):
        _patch_bodies(monkeypatch, {"m1": "x" * 50_000})
        msgs = [_msg()]
        gmail.hydrate_bodies(GmailConfig(body_max_chars=1000), msgs)
        assert len(msgs[0]["content"]) <= 1000

    def test_body_length_is_recorded(self, cfg, monkeypatch):
        _patch_bodies(monkeypatch, {"m1": "abcde"})
        msgs = [_msg()]
        gmail.hydrate_bodies(cfg, msgs)
        assert msgs[0]["body_chars"] == 5


class TestLinkExpansion:
    """The body says "You're invited"; the page says when and where.

    Measured 2026-08-10: 12 of 14 real links returned a full schema.org
    `Event` to a plain GET, so this is exact data replacing inference.
    """

    @staticmethod
    def _patch_fetch(monkeypatch, pages: dict[str, str]):
        from event_harvester.sources import feed_probe

        monkeypatch.setattr(
            feed_probe, "_fetch_page", lambda url, timeout_s: pages.get(url),
        )

    @staticmethod
    def _jsonld(name="Party", start="2026-09-01T18:00", end="2026-09-01T22:00",
                loc="The Bank"):
        import json as _j
        blob = _j.dumps({
            "@type": "Event", "name": name, "startDate": start, "endDate": end,
            "location": {"name": loc, "address": {"addressLocality": "SF"}},
        })
        return f'<html><script type="application/ld+json">{blob}</script></html>'

    def test_facts_are_pulled_from_the_page(self, monkeypatch):
        url = "https://lu.ma/abcdef1234"
        self._patch_fetch(monkeypatch, {url: self._jsonld()})
        got = gmail.expand_event_links([url])
        assert got[url]["date"] == "2026-09-01"
        assert got[url]["time"] == "18:00 - 22:00"
        assert "The Bank" in got[url]["location"]

    def test_a_url_is_fetched_once_however_many_mails_link_it(self, monkeypatch):
        url = "https://lu.ma/abcdef1234"
        calls = []
        from event_harvester.sources import feed_probe

        def counting(u, timeout_s):
            calls.append(u)
            return self._jsonld()

        monkeypatch.setattr(feed_probe, "_fetch_page", counting)
        gmail.expand_event_links([url, url, url])
        assert calls == [url]

    def test_a_dead_page_is_absent_not_an_exception(self, monkeypatch):
        self._patch_fetch(monkeypatch, {})
        assert gmail.expand_event_links(["https://lu.ma/gone"]) == {}

    def test_a_raising_fetch_is_absent_not_an_exception(self, monkeypatch):
        from event_harvester.sources import feed_probe

        def boom(u, timeout_s):
            raise OSError("connection reset")

        monkeypatch.setattr(feed_probe, "_fetch_page", boom)
        assert gmail.expand_event_links(["https://lu.ma/x"]) == {}

    def test_a_page_without_jsonld_yields_nothing(self, monkeypatch):
        url = "https://lu.ma/spa"
        self._patch_fetch(monkeypatch, {url: "<html>client-rendered</html>"})
        assert gmail.expand_event_links([url]) == {}

    def test_facts_land_inside_the_prompt_window(self, cfg, monkeypatch):
        """The whole point of fetching them -- they must survive the 400 cut."""
        url = "https://lu.ma/abcdef1234"
        self._patch_fetch(monkeypatch, {url: self._jsonld(name="Longevity Night")})
        _patch_bodies(monkeypatch, {"m1": f"filler {url} " + ("noise " * 400)})
        msgs = [_msg(subject="You are invited")]
        gmail.hydrate_bodies(cfg, msgs)
        window = msgs[0]["content"][:PROMPT_WINDOW]
        assert "Longevity Night" in window
        assert "2026-09-01" in window
        assert "The Bank" in window

    def test_facts_precede_the_body(self, cfg, monkeypatch):
        url = "https://lu.ma/abcdef1234"
        self._patch_fetch(monkeypatch, {url: self._jsonld()})
        _patch_bodies(monkeypatch, {"m1": f"body text {url}"})
        msgs = [_msg(subject="S")]
        gmail.hydrate_bodies(cfg, msgs)
        c = msgs[0]["content"]
        assert c.index("EVENT:") < c.index("body text")

    def test_expansion_is_skipped_when_disabled(self, monkeypatch):
        from event_harvester.sources import feed_probe

        def boom(*a, **k):
            raise AssertionError("expand_links=False must not fetch")

        monkeypatch.setattr(feed_probe, "_fetch_page", boom)
        _patch_bodies(monkeypatch, {"m1": "body https://lu.ma/abcdef1234"})
        gmail.hydrate_bodies(GmailConfig(expand_links=False), [_msg()])

    def test_the_fetch_ceiling_is_honoured(self, monkeypatch):
        calls = []
        from event_harvester.sources import feed_probe

        monkeypatch.setattr(
            feed_probe, "_fetch_page",
            lambda u, timeout_s: calls.append(u) or self._jsonld(),
        )
        bodies = {
            f"m{i}": f"see https://lu.ma/event{i:04d}" for i in range(10)
        }
        monkeypatch.setattr(gmail, "fetch_full_bodies", lambda cfg, ids: bodies)
        msgs = [_msg(f"m{i}") for i in range(10)]
        gmail.hydrate_bodies(GmailConfig(expand_max=3), msgs)
        assert len(calls) == 3


class TestFormatEventFacts:
    def test_all_three_facts_render(self):
        got = gmail.format_event_facts(
            {"title": "T", "date": "2026-09-01", "time": "18:00", "location": "SF"},
        )
        assert got == "EVENT: T | WHEN: 2026-09-01 18:00 | WHERE: SF"

    def test_missing_pieces_are_omitted_not_blank(self):
        got = gmail.format_event_facts({"title": "T", "date": "", "location": ""})
        assert got == "EVENT: T"

    def test_it_stays_short_enough_to_be_worth_the_window(self):
        got = gmail.format_event_facts({
            "title": "A Reasonably Long Event Title Goes Here",
            "date": "2026-09-01", "time": "18:00 - 22:00",
            "location": "Venue Name, 123 Street, San Francisco",
        })
        assert len(got) < PROMPT_WINDOW // 2


class TestHydrationFailsSafe:
    """A body that will not fetch must cost the body, never the message."""

    def test_an_empty_body_keeps_the_snippet(self, cfg, monkeypatch):
        _patch_bodies(monkeypatch, {"m1": ""})
        msgs = [_msg(subject="Subj", snippet="the snippet")]
        gmail.hydrate_bodies(cfg, msgs)
        assert msgs[0]["content"] == "Subj\nthe snippet"

    def test_a_missing_id_keeps_the_snippet(self, cfg, monkeypatch):
        _patch_bodies(monkeypatch, {"other": "body"})
        msgs = [_msg(subject="Subj", snippet="the snippet")]
        gmail.hydrate_bodies(cfg, msgs)
        assert msgs[0]["content"] == "Subj\nthe snippet"

    def test_a_total_fetch_failure_returns_every_message(self, cfg, monkeypatch):
        _patch_bodies(monkeypatch, {})
        msgs = [_msg("a"), _msg("b")]
        assert len(gmail.hydrate_bodies(cfg, msgs)) == 2

    def test_non_gmail_messages_are_untouched(self, cfg, monkeypatch):
        _patch_bodies(monkeypatch, {"m1": "body"})
        other = {"platform": "telegram", "id": "t1", "content": "unchanged"}
        msgs = [_msg(), other]
        gmail.hydrate_bodies(cfg, msgs)
        assert other["content"] == "unchanged"

    def test_no_gmail_messages_means_no_api_call(self, cfg, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not call the API with nothing to fetch")

        monkeypatch.setattr(gmail, "fetch_full_bodies", boom)
        gmail.hydrate_bodies(cfg, [{"platform": "telegram", "id": "t1"}])

    def test_empty_input_is_fine(self, cfg):
        assert gmail.hydrate_bodies(cfg, []) == []


class TestFetchMessagesRespectsTheFlag:
    def test_hydration_is_skipped_when_disabled(self, monkeypatch):
        called = []
        monkeypatch.setattr(gmail, "hydrate_bodies", lambda *a: called.append(1))
        monkeypatch.setattr(gmail, "_get_service", lambda cfg: None)
        gmail.fetch_messages(GmailConfig(fetch_bodies=False), _since())
        assert not called


class TestTheReadFilter:
    """Read mail was 42% of the mailbox. An invite you opened is still one."""

    @staticmethod
    def _mail():
        return [
            {"platform": "gmail", "id": "u", "is_read": False, "is_sent": False},
            {"platform": "gmail", "id": "r", "is_read": True, "is_sent": False},
            {"platform": "gmail", "id": "s", "is_read": True, "is_sent": True},
        ]

    def test_harvesting_keeps_read_mail(self):
        got = gmail.filter_read_sent(self._mail(), drop_read=False)
        assert [m["id"] for m in got] == ["u", "r"]

    def test_your_own_sent_mail_is_dropped_either_way(self):
        for drop in (True, False):
            got = gmail.filter_read_sent(self._mail(), drop_read=drop)
            assert "s" not in [m["id"] for m in got]

    def test_the_default_is_unchanged_for_triage_callers(self):
        """recruiters.py and eval_classifier.py call this with no kwarg."""
        got = gmail.filter_read_sent(self._mail())
        assert [m["id"] for m in got] == ["u"]

    def test_non_gmail_passes_through_regardless(self):
        msgs = [{"platform": "discord", "id": "d", "is_read": True}]
        assert gmail.filter_read_sent(msgs, drop_read=True) == msgs


class TestTheCouplingToBuildPrompt:
    """`gmail._PROMPT_WINDOW` is a copy of a constant that lives elsewhere.

    Everything about the content layout -- links first, small pre-context, a
    1500-char digest -- is sized against `build_prompt`'s 400-char truncation.
    If that number moves and this one does not, hydration silently goes back
    to feeding the LLM a letterhead, and no other test would notice.
    """

    def test_build_prompt_still_truncates_where_we_think(self):
        import inspect

        from event_harvester.pipeline import analysis

        src = inspect.getsource(analysis.build_prompt)
        assert f'm["content"][:{gmail._PROMPT_WINDOW}]' in src, (
            f"build_prompt no longer truncates at {gmail._PROMPT_WINDOW}; "
            "re-tune gmail._DIGEST_CHARS / _CONTEXT_BEFORE / _MAX_LINKS"
        )

    def test_the_prefix_cannot_consume_the_whole_window(self):
        """Subject + max links must leave room for the date-bearing prose."""
        worst_case_links = _MAX_LINK_CHARS * gmail._MAX_LINKS
        assert worst_case_links < gmail._PROMPT_WINDOW // 2


_MAX_LINK_CHARS = 90  # a typical tracking URL; longer ones are the exception


class TestTheOrchestratorPassesTheConfig:
    """A config flag nothing reads is the bug this guards against."""

    def test_orchestrator_forwards_drop_read(self):
        import inspect

        from event_harvester.pipeline import orchestrator

        src = inspect.getsource(orchestrator.Pipeline.run)
        assert "drop_read=self.cfg.gmail.drop_read" in src, (
            "orchestrator must pass the configured value, not the default"
        )


def _since():
    from datetime import datetime, timezone
    return datetime(2026, 8, 1, tzinfo=timezone.utc)
