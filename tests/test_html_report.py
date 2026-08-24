"""Tests for the self-contained HTML report.

Two things carry real risk and are pinned hardest: that scraped text cannot
break out of the page, and that the file has no external dependencies — because
a report that needs the network to render is not forwardable.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import date, timedelta

import pytest

from event_harvester.outputs.html_report import (
    _BUCKET_ORDER,
    _bucket,
    _prepare,
    _safe_json,
    generate_html_report,
)


def _events():
    today = date.today()
    return [
        {
            "title": "Queer Night", "date": today.isoformat(), "time": "9pm",
            "location": "The Stud", "link": "https://example.test/e/1",
            "details": "Doors at 9.", "source": "instagram in stud", "platform": "web",
        },
        {
            "title": "AI Meetup", "date": (today + timedelta(days=3)).isoformat(),
            "location": "SoMa", "source": "meetup", "platform": "web",
        },
        {"title": "Undated thing", "source": "telegram", "platform": "telegram"},
    ]


@pytest.fixture
def report(tmp_path):
    path = tmp_path / "out.html"
    generate_html_report(_events(), output_path=str(path), source_counts={"web": 2},
                         total_messages=41)
    return path.read_text(encoding="utf-8")


class TestSelfContained:
    def test_no_external_resources(self, report):
        """No CDN, no fonts, no analytics — it has to render on a plane."""
        for pattern in (r'src\s*=\s*["\']https?:', r'<link[^>]+href\s*=\s*["\']https?:',
                        r'@import', r'fonts\.googleapis'):
            assert not re.search(pattern, report, re.I), f"external resource: {pattern}"

    def test_styles_and_script_are_inline(self, report):
        assert "<style>" in report and "<script>" in report

    def test_it_declares_a_mobile_viewport(self, report):
        assert 'name="viewport"' in report
        assert "width=device-width" in report

    def test_it_supports_both_colour_schemes(self, report):
        assert "prefers-color-scheme: dark" in report
        assert 'name="color-scheme"' in report

    def test_outbound_links_do_not_leak_the_opener(self, report):
        assert 'rel="noopener noreferrer"' in report or "noopener" in report


class TestLiveUpdate:
    """The page refreshes itself when served, and stays inert as a file."""

    def test_polling_is_gated_on_being_served(self, report):
        """file:// has no server; polling there fails CORS and logs forever."""
        assert "location.protocol === 'http:'" in report
        assert "if (LIVE)" in report

    def test_it_re_renders_rather_than_reloading(self, report):
        """A reload throws away the search term, filter and scroll position."""
        assert "location.reload" not in report
        assert "DATA.length = 0" in report

    def test_it_catches_up_when_a_phone_wakes(self, report):
        assert "visibilitychange" in report

    def test_it_asks_for_the_relative_endpoints(self, report):
        """So the page works behind a path prefix or a tunnel."""
        assert "fetch('healthz'" in report
        assert "fetch('events.json'" in report
        assert "fetch('/healthz'" not in report


class TestUntrustedContent:
    """Event text is scraped from arbitrary websites."""

    def test_a_script_tag_in_a_title_cannot_close_the_data_block(self, tmp_path):
        nasty = [{
            "title": "</script><script>alert(1)</script>",
            "date": date.today().isoformat(),
            "details": "</SCRIPT ><img src=x onerror=alert(2)>",
            "platform": "web",
        }]
        path = tmp_path / "x.html"
        generate_html_report(nasty, output_path=str(path))
        html = path.read_text(encoding="utf-8")
        # Counting `<script` would be the wrong invariant: inside a
        # `<script type="application/json">` block the HTML parser looks for
        # *closing* `</script` and nothing else, so a literal `<script>` in the
        # payload is inert text. The thing that must not exist is an extra
        # closer — that is what would end the block early and turn the rest of
        # the payload into executable markup.
        assert html.count("</script") == 3, "an injected </script closed the block early"
        assert "</script><script>alert(1)" not in html
        assert "<\\/script>" in html, "the injected closer should be escaped, not stripped"

    def test_line_separators_are_escaped(self):
        """U+2028 is valid JSON and a line terminator to a JS parser."""
        out = _safe_json({"t": "a b c"})
        assert " " not in out and " " not in out
        assert "\\u2028" in out and "\\u2029" in out

    def test_the_payload_still_round_trips(self):
        payload = [{"title": "Café </script> ok", "n": 1}]
        embedded = _safe_json(payload)
        assert json.loads(embedded.replace("<\\/", "</")) == payload

    def test_markup_survives_as_text_not_as_markup(self, tmp_path):
        path = tmp_path / "x.html"
        generate_html_report([{"title": "<b>Bold</b> Night", "platform": "web"}],
                             output_path=str(path))
        html = path.read_text(encoding="utf-8")
        assert "<b>Bold</b> Night" not in html.split("events-data")[0], (
            "raw markup appeared in the document body"
        )


class TestBuckets:
    def test_today_tomorrow_and_later(self):
        today = date.today()
        assert _bucket(today.isoformat(), today) == "Today"
        assert _bucket((today + timedelta(days=1)).isoformat(), today) == "Tomorrow"
        assert _bucket((today + timedelta(days=4)).isoformat(), today) == "This week"
        assert _bucket((today + timedelta(days=20)).isoformat(), today) == "This month"
        assert _bucket((today + timedelta(days=200)).isoformat(), today) == "Later"
        assert _bucket((today - timedelta(days=1)).isoformat(), today) == "Past"

    def test_a_missing_or_broken_date_is_undated_not_a_crash(self):
        today = date.today()
        for value in (None, "", "not-a-date", "2026-13-45"):
            assert _bucket(value, today) == "Undated"

    def test_undated_and_past_sort_last(self):
        """A stale event must not head the page."""
        assert _BUCKET_ORDER.index("Undated") > _BUCKET_ORDER.index("Later")
        assert _BUCKET_ORDER.index("Past") == len(_BUCKET_ORDER) - 1


class TestLocalDateBucketing:
    """Event dates are local wall-clock dates, not instants."""

    def test_tonight_is_today_not_past(self, tmp_path):
        """The bug a live render caught and the unit tests could not.

        Bucketing local event dates against the *UTC* date put everything
        happening tonight into "Past" from 5pm PDT onward — precisely when the
        page is opened to decide what to do that evening. Both sides of the
        comparison have to be local.
        """
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        utc_today = _dt.now(_tz.utc).date()
        local_today = _dt.now().astimezone().date()
        tonight = [{"title": "Tonight", "date": local_today.isoformat(), "platform": "web"}]

        prepared = _prepare(tonight, local_today)
        assert prepared[0]["bucket"] == "Today"

        if utc_today != local_today:
            # Only meaningful in a timezone where the two currently differ;
            # this is the exact condition that produced the bug.
            assert _prepare(tonight, utc_today)[0]["bucket"] == "Past", (
                "sanity: bucketing against UTC is what mis-sorted tonight"
            )

    def test_today_can_be_injected_for_determinism(self, tmp_path):
        fixed = date(2026, 7, 30)
        path = tmp_path / "fixed.html"
        generate_html_report(
            [{"title": "On the day", "date": "2026-07-30", "platform": "web"}],
            output_path=str(path), today=fixed,
        )
        assert '"bucket": "Today"' in path.read_text(encoding="utf-8")


class TestPrepare:
    def test_only_display_fields_are_embedded(self):
        """The file gets forwarded; internals should not ride along."""
        prepared = _prepare([{
            "title": "X", "platform": "web",
            "_status": "approved", "_internal": "secret", "raw_message": "...",
        }], date.today())
        assert set(prepared[0]) == {
            "i", "title", "date", "time", "location", "link", "details",
            "source", "author", "platform", "region", "priority", "bucket",
            "ticktick", "thumb",
        }, (
            "`author` was added 2026-08-11 for the card byline. It is derived "
            "from `source`, which is already embedded, so it widens the "
            "rendering surface without widening what the forwarded file "
            "discloses — which is the property this test exists to protect. "
            "`thumb` (2026-08-23) is the flyer picture, display content the "
            "card already embedded via the link-thumbnail step; carrying it "
            "here lets the OCR-embedded flyer win, and it is meant to be shown."
        )

    def test_every_event_gets_a_ticktick_deep_link(self):
        prepared = _prepare(_events(), date.today())
        assert all(ev["ticktick"].startswith("ticktick://") for ev in prepared)

    def test_long_details_are_truncated(self):
        prepared = _prepare([{"title": "X", "details": "y" * 5000}], date.today())
        assert len(prepared[0]["details"]) <= 1200


class TestTheFilterBar:
    """Regressions from a screenshot, 2026-08-11.

    The nav showed **22 chips for 33 events**, three of them completely blank,
    and the same sender twice under two spellings. All three had the same
    root: the chip label was the LLM's free-text `source` string.
    """

    @staticmethod
    def _html(events, tmp_path):
        from event_harvester.outputs.html_report import generate_html_report
        p = tmp_path / "r.html"
        generate_html_report(events, output_path=str(p))
        return p.read_text(encoding="utf-8")

    def test_an_angle_bracket_sender_cannot_inject_markup(self, tmp_path):
        """`<noreply@x.com>` rendered as an unknown element — a blank chip.

        This is the bug, and it is an escaping hole before it is a visual one:
        model-derived text was reaching the document as markup.
        """
        html = self._html([{
            "title": "T", "date": "2026-09-01",
            "source": "<noreply@reminder.eventbrite.com> in #INBOX",
            "_platforms": ["gmail"],
        }], tmp_path)
        # Scoped to the markup above <main>. The raw string does still appear
        # inside the `application/json` data block, which is correct and is
        # `_safe_json`'s job — the defect was it reaching the *document*.
        head = html.split("<main")[0]
        assert "<noreply@reminder.eventbrite.com>" not in head
        assert "&lt;noreply" not in head, (
            "the address should not reach the chip at all — the chip is a "
            "platform now, not a sender"
        )

    def test_a_quote_in_a_sender_cannot_break_out_of_the_attribute(self, tmp_path):
        html = self._html([{
            "title": "T", "platform": 'w" onclick="alert(1)',
        }], tmp_path)
        assert 'onclick="alert(1)' not in html

    def test_chips_are_platforms_not_senders(self, tmp_path):
        """Twelve events from ten senders on one platform is one chip."""
        events = [
            {"title": f"E{i}", "date": "2026-09-01",
             "source": f"person{i}@example.com in #INBOX", "_platforms": ["gmail"]}
            for i in range(12)
        ]
        html = self._html(events, tmp_path)
        # Scoped to the source axis: the place row has chips of its own.
        assert html.count("data-source=") == 2, "just All + gmail"
        assert "person0@example.com" not in html.split("<main")[0]

    def test_each_chip_carries_its_count(self, tmp_path):
        events = (
            [{"title": "a", "_platforms": ["gmail"]}] * 3
            + [{"title": "b", "_platforms": ["web"]}]
        )
        head = self._html(events, tmp_path).split("<main")[0]
        assert '<span class="tally">3</span>' in head
        assert '<span class="tally">4</span>' in head, "the All chip totals them"

    def test_there_is_an_explicit_all_chip(self, tmp_path):
        """Clearing a filter previously meant re-clicking the active chip —
        discoverable only by accident."""
        head = self._html(_events(), tmp_path).split("<main")[0]
        assert 'data-source=""' in head
        assert "All" in head

    def test_chips_are_ordered_by_size(self, tmp_path):
        head = self._html(
            [{"title": "a", "_platforms": ["web"]}]
            + [{"title": "b", "_platforms": ["gmail"]}] * 5, tmp_path,
        ).split("<main")[0]
        assert head.index('data-source="gmail"') < head.index('data-source="web"')


class TestAuthorDisplay:
    def test_a_from_header_becomes_a_name(self):
        from event_harvester.outputs.html_report import _clean_author
        assert _clean_author("Ben Holfeld <Ben@luma-mail.com> in #INBOX") \
            == "Ben Holfeld"

    def test_a_bare_address_loses_its_brackets(self):
        from event_harvester.outputs.html_report import _clean_author
        assert _clean_author("<noreply@eventbrite.com>") == "noreply@eventbrite.com"

    def test_a_plain_handle_is_untouched(self):
        from event_harvester.outputs.html_report import _clean_author
        assert _clean_author("@qbarsf in #stories") == "@qbarsf"

    def test_quotes_are_stripped(self):
        from event_harvester.outputs.html_report import _clean_author
        assert _clean_author('"Llama Lounge" <ll@x.com>') == "Llama Lounge"


class TestPlatformResolution:
    def test_an_explicit_platform_wins(self):
        from event_harvester.outputs.html_report import _platform_of
        assert _platform_of({"platform": "web", "_platforms": ["gmail"]}, "x") == "web"

    def test_a_platform_outside_the_vocabulary_is_ignored(self):
        """`data/.site/events.json` is already-prepared output, so a rebuild
        feeds this the `platform` an older build wrote — the author's email.
        Trusting any non-empty string made the bad chips survive every rebuild
        and heal only after a fresh harvest."""
        from event_harvester.outputs.html_report import _platform_of
        stale = {"platform": "Ben Holfeld <ben@x.com>"}
        assert _platform_of(stale, "Ben in Gmail / INBOX") == "gmail"

    def test_a_single_batch_platform_is_used(self):
        from event_harvester.outputs.html_report import _platform_of
        assert _platform_of({"_platforms": ["gmail"]}, "a in b") == "gmail"

    def test_an_ambiguous_batch_says_so_rather_than_guessing(self):
        from event_harvester.outputs.html_report import _platform_of
        assert _platform_of({"_platforms": ["gmail", "web"]}, "a in b") == "mixed"

    @pytest.mark.parametrize("source,expected", [
        ("@someone in Gmail / INBOX", "gmail"),
        ("Ben <b@x.com> in Gmail/INBOX", "gmail"),
        ("@qbarsf in Web / instagram/@qbarsf", "web"),
        ("@qbarsf in instagram/@qbarsf", "web"),
        ("bot in Telegram / Lonely Hackers Club", "telegram"),
        ("x in Web", "web"),
    ])
    def test_the_channel_half_yields_the_platform_without_a_reharvest(
        self, source, expected,
    ):
        """`build_prompt` groups as "Platform / channel" and the model echoes
        it back, so the platform was in the string all along — on the other
        side of the `" in "` the old code split on. This is what fixes the
        already-cached events."""
        from event_harvester.outputs.html_report import _platform_of
        assert _platform_of({}, source) == expected

    @pytest.mark.parametrize("source", [
        "Telegram / Frolic Party SF 21+ SFW [Unofficial]",
        "Gmail / INBOX",
        "Web / instagram/@qbarsf",
    ])
    def test_a_bare_group_header_with_no_in_separator(self, source):
        """The prompt asks for "@author in #channel" and the model often
        obliges — but it also echoes the group header verbatim. Requiring the
        separator left 148 of 240 events with no source chip."""
        from event_harvester.outputs.html_report import _platform_of
        assert _platform_of({}, source) in ("telegram", "gmail", "web")

    def test_an_unreadable_source_yields_no_chip_rather_than_a_sender(self):
        from event_harvester.outputs.html_report import _platform_of
        assert _platform_of({}, "@someone in #chan") == ""
        assert _platform_of({}, "nonsense") == ""


class TestOutput:
    def test_it_returns_the_path_and_writes_there(self, tmp_path):
        target = tmp_path / "nested" / "r.html"
        result = generate_html_report(_events(), output_path=str(target))
        assert result == str(target)
        assert target.exists()

    def test_an_empty_harvest_still_produces_a_page(self, tmp_path):
        path = tmp_path / "empty.html"
        generate_html_report([], output_path=str(path))
        html = path.read_text(encoding="utf-8")
        assert "0 event(s)" in html

    def test_raw_events_are_used_when_there_are_no_validated_ones(self, tmp_path):
        path = tmp_path / "raw.html"
        generate_html_report([], raw_events=_events(), output_path=str(path))
        assert "3 event(s)" in path.read_text(encoding="utf-8")

    def test_source_chips_come_from_the_data(self, report):
        assert 'data-source="web"' in report
        assert 'data-source="telegram"' in report


class TestMarkdownDeprecation:
    def test_the_markdown_generator_warns(self, tmp_path):
        from event_harvester.outputs.report import generate_report

        with pytest.warns(DeprecationWarning, match="deprecated"):
            generate_report(
                validated_events=_events(), raw_events=[], links=[],
                source_counts={}, total_messages=0,
                output_path=str(tmp_path / "r.md"),
            )

    def test_the_deep_link_builder_moved_but_still_imports_from_the_old_home(self):
        from event_harvester.outputs.deeplinks import ticktick_deep_link as canonical
        from event_harvester.outputs.report import ticktick_deep_link as legacy

        assert canonical is legacy


class TestDetailIsShownNotHidden:
    """Detail sits on the card. It used to be behind a `Details` button.

    Measured on live data, `details` runs 50-146 characters — the button was
    concealing a single line, and part of that line duplicated the card's own
    location row and Open button.
    """

    @staticmethod
    def _js(tmp_path=None, events=None):
        import tempfile
        target = pathlib.Path(tmp_path or tempfile.mkdtemp()) / "r.html"
        generate_html_report(
            events or [{"title": "X", "date": "2026-08-02"}],
            output_path=str(target),
        )
        return target.read_text(encoding="utf-8")

    def test_details_are_not_rendered_hidden(self):
        html = self._js()
        assert "body.hidden = true" not in html, "detail must not start hidden"
        assert "class='details'" in html or "'details'" in html

    def test_the_clamp_is_three_lines(self):
        html = self._js()
        assert "-webkit-line-clamp: 3" in html
        assert ".details.open" in html, "the clamp must be releasable"

    def test_a_hidden_button_is_actually_hidden(self):
        """`display: inline-flex` on .btn overrides the UA [hidden] rule."""
        html = self._js()
        assert "button.btn[hidden]" in html, (
            "without this rule the More button renders even when hidden"
        )

    def test_the_toggle_is_measured_against_real_layout(self):
        """Whether the clamp bit is only knowable after layout."""
        html = self._js()
        assert "scrollHeight" in html and "clientHeight" in html

    def test_measurement_is_deferred_not_done_for_every_card_at_once(self):
        """The old version read scrollHeight for every `.details` in one loop —
        240 forced reflows, and the main cause of the page freezing. Each card
        is now measured when it approaches the viewport."""
        html = self._js()
        assert "IntersectionObserver" in html
        assert "watchToggle" in html

    def test_a_browser_without_the_observer_still_gets_toggles(self):
        html = self._js()
        assert "'IntersectionObserver' in window" in html

    def test_the_toggle_is_resynced_when_card_width_changes(self):
        """Grid and carousel are different widths; so is a window resize."""
        html = self._js()
        assert "addEventListener('resize'" in html
        assert "syncDetailToggles" in html

    def test_duplicate_segments_are_stripped_from_the_inline_text(self):
        html = self._js()
        assert "function detailText" in html
        # The location row and the Open button already carry these.
        assert "ev.location" in html
        assert "^https?:" in html

    def test_search_still_matches_the_raw_details(self):
        """Stripping is for display only — a URL must stay findable."""
        html = self._js()
        # Bounded to the function body rather than a fixed character count —
        # a comment added inside `matching()` pushed the haystack past 400 and
        # failed this test without changing any behaviour.
        body = html.split("function matching()")[1].split("\n}")[0]
        assert "ev.details" in body

    def test_detail_text_is_never_interpolated_as_html(self):
        html = self._js(events=[{
            "title": "X", "date": "2026-08-02",
            "details": "</script><img src=x onerror=alert(1)>",
        }])
        # The payload must reach the DOM through textContent, and must not
        # have closed the JSON block on its way in.
        assert "body.textContent = detail" in html
        assert "</script><img" not in html, "the payload closed the script block"
