"""The reasoning a human does when handed a URL, pinned against fixed HTML.

`onboard` automates blueprint step 3 — "find the page that actually has the
events" — which that document's own "known rough edges" section admits was
manual. The cases below are the ones it was built from, and two of them are
mistakes this module made on 2026-08-14 before the tests existed.

No network. Every test drives fixed HTML through the same functions `explore`
calls, because a test that depends on a venue's live homepage tests the venue.
"""

from __future__ import annotations

from event_harvester.sources.onboard import (
    RAW_DENSE_DATES,
    OnboardReport,
    PageEvidence,
    count_structured_events,
    measure,
    nav_candidates,
    recommend_mode,
    squarespace_sections,
)

BASE = "https://venue.test"


def _page(*, links: str = "", text: str = "", head: str = "") -> str:
    return f"<html><head>{head}</head><body>{links}<p>{text}</p></body></html>"


class TestFindingTheBetterUrl:
    """`thebitterendsf.com` → `/calendar`; `ma.to` → `/events/san-francisco`."""

    def test_nav_links_that_look_like_listings_are_found(self):
        html = _page(links="""
            <a href="/about">About</a>
            <a href="/calendar">Calendar</a>
            <a href="/events/san-francisco">SF Events</a>
            <a href="/menu">Menu</a>
        """)
        found = nav_candidates(html, BASE)
        assert f"{BASE}/calendar" in found
        assert f"{BASE}/events/san-francisco" in found
        assert not any("/about" in u or "/menu" in u for u in found)

    def test_a_squarespace_section_is_a_real_page(self):
        """`<div data-url-id="calendar">` means /calendar exists — which is how
        the Bitter End's schedule turned out to be addressable at all."""
        html = '<div id="calendar-section" data-url-id="calendar"></div>'
        assert squarespace_sections(html, BASE) == [f"{BASE}/calendar"]

    def test_submission_forms_are_not_listings(self):
        html = _page(links='<a href="/events/new">Submit an event</a>')
        assert nav_candidates(html, BASE) == []

    def test_a_room_booking_page_is_not_a_listing(self):
        """It matched on "event" and beat the real calendar. 2026-08-14."""
        html = _page(links='<a href="/event-party-booking">Book a party</a>')
        assert nav_candidates(html, BASE) == []

    def test_a_single_events_page_is_not_a_listing(self):
        """The most seductive wrong answer: a detail page is full of dates, so
        it outranks the listing that links to it on every signal measured here.
        `ma.to/event/bagels-with-buddies-canyon-coffee-14-aug-2026` won before
        `_DETAIL_PATH_RE` existed, and would have pinned the source to one
        night for ever."""
        html = _page(links="""
            <a href="/event/bagels-with-buddies-14-aug-2026">Bagels</a>
            <a href="/events/san-francisco">SF</a>
        """)
        assert nav_candidates(html, BASE) == [f"{BASE}/events/san-francisco"]

    def test_offsite_links_are_ignored(self):
        html = _page(links='<a href="https://elsewhere.test/calendar">Cal</a>')
        assert nav_candidates(html, BASE) == []


class TestRankingUsesEvidenceNotSize:
    def test_dated_links_decide_when_present(self):
        """The `ma.to` discriminator: 18 dated links against 0."""
        listing = PageEvidence(url="a", ok=True, dated_links=18, chars=3756)
        homepage = PageEvidence(url="b", ok=True, dated_links=0, chars=9000)
        assert min([homepage, listing], key=lambda e: e.rank_key) is listing

    def test_density_breaks_a_tie_at_zero_links(self):
        """The Bitter End discriminator: 821 chars of schedule beat 4,619 of
        nav. Concentration, not brevity."""
        schedule = PageEvidence(url="a", ok=True, chars=821, date_tokens=30)
        nav = PageEvidence(url="b", ok=True, chars=4619, date_tokens=1)
        assert min([nav, schedule], key=lambda e: e.rank_key) is schedule

    def test_size_alone_never_decides(self):
        """An earlier rank key ended in `self.chars` to encode "fewer
        characters won". Wrong reading — higher *concentration* won, and they
        merely coincided. With every signal at zero, a size term becomes the
        whole ranking and picks the emptiest page on the domain."""
        tiny = PageEvidence(url="tiny", ok=True, chars=406, hint_rank=5)
        calendar = PageEvidence(url="cal", ok=True, chars=827, hint_rank=0)
        assert min([tiny, calendar], key=lambda e: e.rank_key) is calendar

    def test_structured_events_outrank_bare_density(self):
        spa = PageEvidence(url="a", ok=True, structured_events=45)
        wordy = PageEvidence(url="b", ok=True, chars=1000, date_tokens=9)
        assert min([wordy, spa], key=lambda e: e.rank_key) is spa


class TestItRefusesToGuessWithoutEvidence:
    """A JS-rendered page returns a nav bar and a cookie banner to `requests`.
    Every number reads zero, and a ranking built from zeroes is noise wearing a
    recommendation's clothes."""

    def test_a_blank_page_has_no_signal(self):
        assert not PageEvidence(url="a", ok=True, chars=4530).has_signal

    def test_any_real_signal_counts(self):
        assert PageEvidence(url="a", ok=True, dated_links=1).has_signal
        assert PageEvidence(url="a", ok=True, structured_events=1).has_signal
        assert PageEvidence(url="a", ok=True, date_tokens=1).has_signal

    def test_a_declared_feed_settles_it_without_a_browser(self):
        """A page publishing RSS has told you where its events are, and that
        does not depend on whether its HTML renders server-side."""
        assert PageEvidence(url="a", ok=True, rss_feeds=("/feed",)).has_signal

    def test_the_report_can_say_it_does_not_know(self):
        r = OnboardReport(given_url="u", final_url="u", needs_browser=True)
        assert r.to_dict()["needs_browser"] is True


class TestModeRecommendation:
    def test_a_calendar_feed_beats_scraping_outright(self):
        mode, _ = recommend_mode(PageEvidence(url=BASE, dated_links=50), "http://f.ics")
        assert mode == "ics"

    def test_rss_beats_scraping_too(self):
        ev = PageEvidence(url=BASE, dated_links=50, rss_feeds=("http://f/rss",))
        assert recommend_mode(ev, None)[0] == "rss"

    def test_luma_is_never_scraped(self):
        """Configured as `calendar` it was measured returning 1 event from a
        listing of 80."""
        assert recommend_mode(PageEvidence(url="https://lu.ma/sf"), None)[0] == "luma"
        assert recommend_mode(
            PageEvidence(url="https://partiful.com/e/x"), None)[0] == "partiful"

    def test_dated_links_mean_calendar(self):
        assert recommend_mode(PageEvidence(url=BASE, dated_links=18), None)[0] == "calendar"

    def test_raw_argues_for_itself_on_a_dense_page(self):
        """The Bottom of the Hill trap: `raw` sends ONE message however many
        shows the page lists. A 338 KB listing of ~40 shows yielded 1 event;
        the same venue's RSS yielded 65."""
        mode, warnings = recommend_mode(
            PageEvidence(url=BASE, chars=30000, date_tokens=RAW_DENSE_DATES + 5), None)
        assert mode == "raw"
        assert warnings and "ONE message" in warnings[0]

    def test_a_sparse_raw_page_is_not_nagged(self):
        """A weekly-schedule bar page is the legitimate `raw` case. A warning
        that fires on every run is one the reader learns to skip past."""
        mode, warnings = recommend_mode(
            PageEvidence(url=BASE, chars=800, date_tokens=2), None)
        assert mode == "raw" and warnings == []

    def test_a_cloudflare_challenge_is_named(self):
        _, warnings = recommend_mode(PageEvidence(url=BASE, blocked=True), None)
        assert any("native_chrome" in w for w in warnings)


class TestStructuredData:
    """"Do not assume a SPA renders client-side — measure it." Luma is React
    and still ships its events as JSON-LD."""

    def test_it_counts_events_not_just_finds_one(self):
        html = ('<script type="application/ld+json">'
                '[{"@type":"Event","name":"a"},{"@type":"Event","name":"b"}]'
                "</script>")
        assert count_structured_events(html) == 2

    def test_it_reads_a_graph_wrapper(self):
        html = ('<script type="application/ld+json">'
                '{"@graph":[{"@type":"Event"},{"@type":"Organization"}]}'
                "</script>")
        assert count_structured_events(html) == 1

    def test_a_list_valued_type_still_counts(self):
        html = ('<script type="application/ld+json">'
                '{"@type":["Event","MusicEvent"]}</script>')
        assert count_structured_events(html) == 1

    def test_malformed_json_ld_is_survivable(self):
        assert count_structured_events(
            '<script type="application/ld+json">{oh no</script>') == 0


class TestFeedDetection:
    def test_a_declared_rss_link_is_found(self):
        html = _page(head='<link rel="alternate" type="application/rss+xml" '
                          'href="/blog/rss.xml">')
        assert measure(BASE, html).rss_feeds == (f"{BASE}/blog/rss.xml",)

    def test_a_plain_feed_href_is_found(self):
        assert measure(BASE, _page(links='<a href="/feed/">RSS</a>')).rss_feeds

    def test_vertical_align_is_not_a_calendar_feed(self):
        """The blueprint's own warning: a naive `ical` grep matches the CSS
        property `vertical-align`."""
        html = _page(head="<style>.x{vertical-align:top}</style>")
        assert measure(BASE, html).rss_feeds == ()


class TestMeasurementIsOneFetch:
    def test_every_cheap_signal_comes_from_the_same_bytes(self):
        """The blueprint spends four separate curls on these."""
        html = _page(
            head='<link rel="alternate" type="application/rss+xml" href="/rss">',
            links='<a href="/events/2026-08-14/show">Aug 14 show</a>',
            text="Friday August 15 at 9pm",
        )
        ev = measure(BASE, html)
        assert ev.dated_links == 1
        assert ev.date_tokens >= 1 and ev.time_tokens >= 1
        assert ev.rss_feeds and ev.chars > 0

    def test_density_is_per_thousand_chars(self):
        ev = PageEvidence(url="a", chars=1000, date_tokens=5)
        assert ev.date_density == 5.0

    def test_density_of_an_empty_page_does_not_divide_by_zero(self):
        assert PageEvidence(url="a", chars=0, date_tokens=3).date_density == 0.0

    def test_evidence_is_json_serialisable(self):
        """`to_dict` is the contract for the CLI, the MCP tool and the screen —
        one module, three frontings, and none of them re-derives this."""
        import json
        json.dumps(measure(BASE, _page(text="Aug 14")).to_dict())
