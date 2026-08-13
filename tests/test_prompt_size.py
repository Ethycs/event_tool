"""Prompt composition — what the extraction call actually costs.

Measured 2026-08-13 over the 817 messages a real harvest sends, in the
contiguous batches of 10 it sends them in:

    p50 1,502   p95 2,062   max 2,219 tokens (including the system prompt)

That max is the number that matters for a locally-served model: `qwen3-8b`
behind llama.cpp with `n_ctx=2048` refused 6 of 15 batches outright, and those
six held 51 of the baseline's 78 events, because the largest batches are the
event-dense ones.

`chat_label` is the cheap part of the fix and these tests pin it. The larger
lever is `batch_size`, which is a caller's choice rather than a property of
this module: at 5 the worst batch is 1,297 tokens and every one of the 164
calls fits inside 2048.
"""

from __future__ import annotations

from event_harvester.pipeline.analysis import build_prompt, chat_label


class TestChatLabel:
    """A web source's `channel` is the event's full URL, tracking and all."""

    def test_a_tracking_url_collapses_to_host_and_section(self):
        assert chat_label("web", (
            "https://www.eventbrite.com/e/gp-dinner-199-ecocivilization-"
            "wjeremy-lent-tickets-1991291834481?aff=ebdssbcitybrowse"
        )) == "Web / eventbrite.com/e"

    def test_a_meetup_group_keeps_its_name(self):
        """The first path segment is the identifying part — the group — not
        the slug, which only repeats the event title."""
        assert chat_label("web", (
            "https://www.meetup.com/sfjava/events/316061701/"
            "?recId=31890aba&recSource=event-search"
        )) == "Web / meetup.com/sfjava"

    def test_a_query_only_url_keeps_its_path(self):
        assert chat_label("web", (
            "https://erobay.com/calendar/Calcium40.pl?CalendarName=Community&ID=1238"
        )) == "Web / erobay.com/calendar"

    def test_a_bare_host_survives(self):
        assert chat_label("web", "https://lu.ma") == "Web / lu.ma"

    def test_a_non_url_channel_is_untouched(self):
        """Telegram and Discord channels are already short names."""
        assert chat_label("telegram", "sf-parties") == "Telegram / sf-parties"
        assert chat_label("gmail", "INBOX") == "Gmail / INBOX"

    def test_several_events_from_one_site_share_a_group(self):
        """Ten one-message groups cost ten headers. This is most of the win."""
        a = chat_label("web", "https://www.eventbrite.com/e/thing-a-tickets-1?aff=x")
        b = chat_label("web", "https://www.eventbrite.com/e/thing-b-tickets-2?aff=y")
        assert a == b


def _messages(n, channel_fn, platform="web"):
    return [{
        "content": f"Event {i} on Friday at 9pm https://lu.ma/e{i}",
        "timestamp": "2026-08-12T10:00:00+00:00",
        "platform": platform, "channel": channel_fn(i),
        "author": f"@src{i}", "id": str(i),
    } for i in range(n)]


class TestPromptSize:
    def test_compacting_headers_shrinks_a_web_batch(self):
        msgs = _messages(10, lambda i: (
            f"https://www.eventbrite.com/e/some-long-event-title-here-"
            f"tickets-19912918344{i}?aff=ebdssbcitybrowse&utm_source=x"
        ))
        prompt = build_prompt(msgs, 7)
        # The tracking parameters must not reach the model at all.
        assert "aff=ebdssbcitybrowse" not in prompt
        assert "utm_source" not in prompt
        assert prompt.count("### ") == 1, "one site, one header"

    def test_a_single_message_group_omits_the_count(self):
        prompt = build_prompt(_messages(1, lambda i: "sf-parties", "telegram"), 7)
        assert "(1 messages)" not in prompt

    def test_a_multi_message_group_keeps_it(self):
        prompt = build_prompt(_messages(3, lambda i: "sf-parties", "telegram"), 7)
        assert "(3 messages)" in prompt

    def test_the_author_still_reaches_the_model(self):
        """`outputs/regions.py` reads venue names out of the `source` string to
        place events that carry no location, and `source` is built from the
        author. Shortening the channel must not take the author with it."""
        prompt = build_prompt(_messages(1, lambda i: "https://elrio.com/events"), 7)
        assert "@src0" in prompt

    def test_halving_the_batch_roughly_halves_the_prompt(self):
        """The lever that actually fits a 2048-token context window."""
        msgs = _messages(10, lambda i: f"https://site{i}.com/events/thing-{i}")
        ten = len(build_prompt(msgs, 7))
        five = len(build_prompt(msgs[:5], 7))
        assert five < ten * 0.65
