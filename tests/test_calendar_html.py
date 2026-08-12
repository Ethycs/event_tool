"""Tests for the month-grid page and the nav that finally links to it.

The grid carries one risk the list page does not: **every cell is a date**, and
a page that renders dates through the browser's UTC parser is wrong in all
forty-two of them at once. So the date handling is pinned by asserting the page
contains no call that would reintroduce it, rather than only by checking output
— there is no DOM here to inspect.

The other pinned property is the one the list page already has: scraped text
reaches the file as JSON data, never as markup.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from event_harvester.outputs.calendar_html import generate_calendar_html
from event_harvester.outputs.ics import ICS_JS
from event_harvester.outputs.nav import PAGES, nav_html


def _events():
    today = date.today()
    return [
        {
            "title": "Queer Night", "date": today.isoformat(), "time": "9pm",
            "location": "The Stud, San Francisco", "link": "https://example.test/e/1",
            "details": "Doors at 9.", "source": "instagram in stud", "platform": "web",
        },
        {
            "title": "AI Meetup", "date": (today + timedelta(days=3)).isoformat(),
            "location": "Oakland", "source": "meetup", "platform": "web",
        },
        {"title": "Undated thing", "source": "telegram", "platform": "telegram"},
    ]


@pytest.fixture
def page(tmp_path):
    path = tmp_path / "calendar.html"
    generate_calendar_html(_events(), output_path=str(path))
    return path.read_text(encoding="utf-8")


def _code(page: str) -> str:
    """The page with `//` comments removed.

    Needed because the comments name the things they warn against — the line
    explaining why `toISOString` is wrong contains the word `toISOString`, so
    an assertion against the raw text fails on the documentation rather than
    on the defect.
    """
    return re.sub(r"//[^\n]*", "", page)


class TestSelfContained:
    """Same contract as the list page: it has to render on a plane."""

    def test_no_external_resources(self, page):
        for pattern in (r'src\s*=\s*["\']https?:', r'<link[^>]+href\s*=\s*["\']https?:',
                        r"@import", r"fonts\.googleapis"):
            assert not re.search(pattern, page, re.I), f"external resource: {pattern}"

    def test_styles_and_script_are_inline(self, page):
        assert "<style>" in page and "<script>" in page

    def test_it_declares_a_mobile_viewport(self, page):
        assert 'name="viewport"' in page and "width=device-width" in page

    def test_it_supports_both_colour_schemes(self, page):
        assert "prefers-color-scheme: dark" in page

    def test_outbound_links_do_not_leak_the_opener(self, page):
        assert "noopener" in page

    def test_it_carries_no_thumbnails(self, page):
        """The whole reason this page is the fast one to open."""
        assert "data:image" not in page


class TestDateHandling:
    """The defect this page is most exposed to, pinned three ways."""

    def test_it_never_parses_an_iso_string_into_a_date(self, page):
        """`new Date('2026-08-12')` is UTC midnight — a day early out west."""
        assert not re.search(r"new Date\(\s*['\"]", _code(page))
        assert not re.search(r"new Date\(\s*ev\.date", _code(page))

    def test_it_never_formats_a_day_key_through_utc(self, page):
        assert "toISOString" not in _code(page)

    def test_the_build_date_is_embedded_as_a_local_iso_date(self, page):
        m = re.search(r'<body data-today="(\d{4}-\d{2}-\d{2})"', page)
        assert m, "the page must carry the date it was built for"
        assert m.group(1) == date.today().isoformat()

    def test_an_explicit_today_wins(self, tmp_path):
        path = tmp_path / "c.html"
        generate_calendar_html(_events(), output_path=str(path),
                               today=date(2026, 1, 15))
        assert 'data-today="2026-01-15"' in path.read_text(encoding="utf-8")


class TestUntrustedText:
    def test_a_title_cannot_close_the_data_block(self, tmp_path):
        """The payload stays inside the JSON island, which is the whole
        defence — it is data there, however it is spelled."""
        path = tmp_path / "c.html"
        generate_calendar_html(
            [{"title": "</script><script>alert(1)</script>",
              "details": "</SCRIPT ><img src=x onerror=alert(2)>",
              "date": "2026-08-20", "source": "web"}],
            output_path=str(path),
        )
        html = path.read_text(encoding="utf-8")
        assert "</script><script>alert(1)" not in html
        assert "</SCRIPT ><img" not in html

    def test_the_payload_is_valid_json(self, page):
        m = re.search(r'<script type="application/json" id="events-data">(.*?)</script>',
                      page, re.S)
        assert m
        data = json.loads(m.group(1))
        assert [ev["title"] for ev in data] == ["Queer Night", "AI Meetup",
                                                "Undated thing"]

    def test_titles_are_written_through_text_nodes(self, page):
        """The one place a title reaches the DOM in a grid cell."""
        assert "createTextNode(ev.title)" in page
        assert "innerHTML" not in _code(page)


class TestWhatThePageOffers:
    def test_undated_events_are_reachable(self, page):
        """A grid cannot place them, so they get their own tray rather than
        silently vanishing — 1 of the 3 fixtures has no date."""
        assert 'id="undated"' in page

    def test_it_counts_dated_events_in_the_title(self, page):
        assert "<title>Calendar — 2</title>" in page

    def test_month_navigation_exists(self, page):
        for id_ in ("prev", "next", "today-btn", "month-label", "grid"):
            assert f'id="{id_}"' in page

    def test_both_filter_axes_are_present(self, page):
        assert 'data-region="sf"' in page
        assert 'data-source="web"' in page
        assert 'data-region=""' in page and 'data-source=""' in page

    def test_it_can_export_a_whole_month(self, page):
        """Thirty separate downloads is not an import."""
        assert "icsForMany" in page
        assert 'id="export"' in page

    def test_the_ics_builder_is_the_shared_one(self, page):
        assert "PRODID:-//event-harvester//EN" in page
        assert "function icsForMany" in ICS_JS


class TestNav:
    def test_off_by_default_because_a_forwarded_file_has_no_origin(self, page):
        assert 'href="/admin"' not in page
        assert "sitenav" not in page

    def test_on_when_served(self, tmp_path):
        path = tmp_path / "c.html"
        generate_calendar_html(_events(), output_path=str(path), nav=True)
        html = path.read_text(encoding="utf-8")
        for target, label, _ in PAGES:
            assert f'href="{target}"' in html
            assert f">{label}</a>" in html

    def test_the_current_page_is_marked(self):
        assert 'href="/calendar" title="The same events on a month grid" '\
               'aria-current="page"' in nav_html("/calendar")
        assert nav_html("/calendar").count("aria-current") == 1

    def test_no_active_page_means_no_nav(self):
        assert nav_html("") == ""

    def test_the_list_page_links_here_when_served(self, tmp_path):
        from event_harvester.outputs.html_report import generate_html_report

        path = tmp_path / "index.html"
        generate_html_report(_events(), output_path=str(path), nav=True)
        html = path.read_text(encoding="utf-8")
        assert 'href="/calendar"' in html and 'href="/admin"' in html
        assert 'href="/" title="Everything found, newest harvest" '\
               'aria-current="page"' in html

    def test_the_list_page_stays_linkless_by_default(self, tmp_path):
        from event_harvester.outputs.html_report import generate_html_report

        path = tmp_path / "index.html"
        generate_html_report(_events(), output_path=str(path))
        assert 'href="/admin"' not in path.read_text(encoding="utf-8")


class TestEmpty:
    def test_a_harvest_with_nothing_in_it_still_renders(self, tmp_path):
        path = tmp_path / "c.html"
        generate_calendar_html([], output_path=str(path))
        html = path.read_text(encoding="utf-8")
        assert "<title>Calendar — 0</title>" in html
        assert '<script type="application/json" id="events-data">[]</script>' in html
