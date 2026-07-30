"""Tests for browser-free calendar-feed discovery.

Every test drives the real ``probe_feeds`` against a fake HTTP layer, so what
is pinned is the *effect* — which URL comes back and why — not which helper
was called. A wrong pattern in the fingerprint table should surface here as a
missing or misranked feed.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from event_harvester.sources.feed_probe import FeedCandidate, best_feed, probe_feeds

# ── Fake HTTP layer ───────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.content = body
        self.status_code = status
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class _FakeHTTP:
    """Serves a URL->body map; anything unmapped is a 404.

    Records every URL requested so tests can assert we did *not* poke a
    server we had no reason to poke.
    """

    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.requested: list[str] = []

    def get(self, url, **kw):
        self.requested.append(url)
        for known, body in self.routes.items():
            if url.rstrip("/") == known.rstrip("/"):
                return _FakeResponse(body)
        return _FakeResponse(b"<html>404</html>", status=404)


def _ics(*offsets_days: int, name: str = "Test Event") -> bytes:
    """Minimal valid iCalendar with one VEVENT per offset from now."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN"]
    for i, off in enumerate(offsets_days):
        dt = datetime.now(timezone.utc) + timedelta(days=off)
        lines += [
            "BEGIN:VEVENT",
            f"UID:evt-{i}@test",
            f"DTSTART:{dt.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{name} {i}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode()


@pytest.fixture
def http():
    """Patch requests.get for the duration of a test."""
    holder = {}

    def _install(routes):
        fake = _FakeHTTP(routes)
        holder["fake"] = fake
        patcher = patch("requests.get", side_effect=fake.get)
        patcher.start()
        holder["patcher"] = patcher
        return fake

    yield _install
    if "patcher" in holder:
        holder["patcher"].stop()


# ── Discovery lanes ───────────────────────────────────────────────────


class TestDiscovery:
    def test_link_rel_alternate_is_found(self, http):
        http({
            "https://venue.example": (
                b'<html><head><link rel="alternate" type="text/calendar" '
                b'href="/feed.ics"></head><body>hi</body></html>'
            ),
            "https://venue.example/feed.ics": _ics(1, 5),
        })
        found = probe_feeds("https://venue.example")
        assert len(found) == 1
        assert found[0].url == "https://venue.example/feed.ics"
        assert found[0].origin == "link-rel"
        assert found[0].events == 2
        assert found[0].future_events == 2

    def test_wordpress_the_events_calendar_fingerprint(self, http):
        """The pattern that already pays off for powerhousebar.com."""
        fake = http({
            "https://bar.example": (
                b'<html><body class="tribe-events-page">'
                b'<div id="tribe-events"></div></body></html>'
            ),
            "https://bar.example/events/?ical=1": _ics(2, 9, 30),
        })
        found = probe_feeds("https://bar.example")
        assert found, "tribe fingerprint should have produced a feed"
        assert found[0].url == "https://bar.example/events/?ical=1"
        assert found[0].platform == "wordpress-the-events-calendar"
        assert found[0].origin == "platform"
        # Fingerprint matched, so blind paths must not have been tried.
        assert not any("calendar.ics" in u for u in fake.requested)

    def test_google_calendar_embed_becomes_ics_url(self, http):
        http({
            "https://venue.example": (
                b'<html><body><iframe src="https://calendar.google.com/calendar/'
                b'embed?src=abc%40group.calendar.google.com&ctz=America/Los_Angeles">'
                b"</iframe></body></html>"
            ),
            "https://calendar.google.com/calendar/ical/"
            "abc@group.calendar.google.com/public/basic.ics": _ics(3),
        })
        found = probe_feeds("https://venue.example")
        assert len(found) == 1
        assert found[0].platform == "google-calendar"
        assert found[0].origin == "embed"

    def test_visible_anchor_to_ics(self, http):
        http({
            "https://venue.example": (
                b'<html><body><a href="/calendar/export.ics">Subscribe</a></body></html>'
            ),
            "https://venue.example/calendar/export.ics": _ics(4),
        })
        found = probe_feeds("https://venue.example")
        assert found[0].origin == "anchor"

    def test_blind_probe_only_when_nothing_declared(self, http):
        fake = http({
            "https://plain.example": b"<html><body>Come to our bar</body></html>",
            "https://plain.example/events/?ical=1": _ics(1),
        })
        found = probe_feeds("https://plain.example")
        assert found[0].origin == "blind"
        assert found[0].url == "https://plain.example/events/?ical=1"
        # Blind mode is a last resort but must stay bounded.
        assert len(fake.requested) <= 6

    def test_blind_probe_can_be_disabled(self, http):
        http({
            "https://plain.example": b"<html><body>nothing here</body></html>",
            "https://plain.example/events/?ical=1": _ics(1),
        })
        assert probe_feeds("https://plain.example", allow_blind=False) == []

    def test_already_an_ics_url_skips_discovery(self, http):
        fake = http({"https://cdn.example/cal.ics": _ics(1, 2)})
        found = probe_feeds("https://cdn.example/cal.ics")
        assert len(found) == 1
        assert fake.requested == ["https://cdn.example/cal.ics"], "should not fetch a page first"

    def test_webcal_scheme_is_normalized(self, http):
        http({"https://cdn.example/cal.ics": _ics(1)})
        found = probe_feeds("webcal://cdn.example/cal.ics")
        assert len(found) == 1


# ── Validation is the filter ──────────────────────────────────────────


class TestValidation:
    def test_html_404_at_a_guessed_path_is_rejected(self, http):
        """A WordPress 404 is a 200 with HTML — the pattern must not be trusted."""
        http({
            "https://venue.example": b"<html><body>hi</body></html>",
            # Every blind path resolves, but to a soft-404 page, not a feed.
            "https://venue.example/events/?ical=1": b"<html><body>Not found</body></html>",
        })
        assert probe_feeds("https://venue.example") == []

    def test_feed_with_no_events_is_not_valid(self, http):
        http({
            "https://venue.example": (
                b'<html><head><link rel="alternate" type="text/calendar" '
                b'href="/empty.ics"></head></html>'
            ),
            "https://venue.example/empty.ics": b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR",
        })
        assert probe_feeds("https://venue.example") == []

    def test_past_only_feed_is_valid_but_has_no_future_events(self, http):
        http({
            "https://venue.example": (
                b'<html><head><link rel="alternate" type="text/calendar" '
                b'href="/old.ics"></head></html>'
            ),
            "https://venue.example/old.ics": _ics(-40, -10),
        })
        found = probe_feeds("https://venue.example")
        assert len(found) == 1
        assert found[0].events == 2
        assert found[0].future_events == 0

    def test_unreachable_page_returns_empty(self, http):
        http({})
        assert probe_feeds("https://gone.example") == []
        assert best_feed("https://gone.example") is None

    def test_per_event_export_links_are_not_feeds(self, http):
        """Regression: the real studsf.com/calendar case.

        Squarespace puts an "add to calendar" export at every event's dated
        permalink. Each one is valid iCalendar containing exactly that event,
        so validation alone cannot reject them -- and adopting one would pin
        the source to a single night forever.
        """
        fake = http({
            "https://studlike.example/calendar": (
                b"<html><body>"
                b'<a href="/calendar/2026/7/30/olivia-lauren?format=ical">Add to Calendar</a>'
                b'<a href="/calendar/2026/8/14/drag-night?format=ical">Add to Calendar</a>'
                b"</body></html>"
            ),
            "https://studlike.example/calendar/2026/7/30/olivia-lauren?format=ical": _ics(1),
            "https://studlike.example/calendar/2026/8/14/drag-night?format=ical": _ics(16),
        })
        assert probe_feeds("https://studlike.example/calendar", allow_blind=False) == []
        # Rejected at discovery, so they never cost a request.
        assert not any("olivia-lauren" in u for u in fake.requested)

    def test_undated_per_event_slug_is_rejected(self, http):
        """The second half of the studsf.com case — an opaque slug, no date.

        Nothing in the URL says "single event"; only the combination of
        "nested under the page I probed" and "contains one event" does.
        """
        http({
            "https://studlike.example/calendar": (
                b"<html><body>"
                b'<a href="/calendar/h4wr7zbsakm3fye-ayfjb-srz3e?format=ical">Add</a>'
                b"</body></html>"
            ),
            "https://studlike.example/calendar/h4wr7zbsakm3fye-ayfjb-srz3e?format=ical": _ics(-2),
        })
        assert probe_feeds("https://studlike.example/calendar", allow_blind=False) == []

    def test_nested_feed_with_several_events_is_kept(self, http):
        """The rule must not eat a real feed that happens to live one level down."""
        http({
            "https://venue.example/calendar": (
                b'<html><body><a href="/calendar/all.ics">Subscribe</a></body></html>'
            ),
            "https://venue.example/calendar/all.ics": _ics(1, 4, 9),
        })
        found = probe_feeds("https://venue.example/calendar", allow_blind=False)
        assert len(found) == 1
        assert found[0].url == "https://venue.example/calendar/all.ics"

    def test_real_feed_still_wins_on_a_page_full_of_per_event_links(self, http):
        http({
            "https://venue.example/calendar": (
                b"<html><head>"
                b'<link rel="alternate" type="text/calendar" href="/all.ics">'
                b"</head><body>"
                b'<a href="/calendar/2026/7/30/show?format=ical">Add</a>'
                b"</body></html>"
            ),
            "https://venue.example/all.ics": _ics(1, 2, 3),
            "https://venue.example/calendar/2026/7/30/show?format=ical": _ics(1),
        })
        found = probe_feeds("https://venue.example/calendar")
        assert len(found) == 1
        assert found[0].url == "https://venue.example/all.ics"


# ── Ranking ───────────────────────────────────────────────────────────


class TestRanking:
    def test_more_upcoming_events_wins(self, http):
        http({
            "https://venue.example": (
                b'<html><head><link rel="alternate" type="text/calendar" '
                b'href="/thin.ics"></head>'
                b'<body class="tribe-events"><a href="/fat.ics">all events</a></body></html>'
            ),
            "https://venue.example/thin.ics": _ics(1),
            "https://venue.example/fat.ics": _ics(1, 2, 3, 4, 5),
        })
        found = probe_feeds("https://venue.example")
        assert found[0].url == "https://venue.example/fat.ics"
        assert found[0].future_events == 5

    def test_origin_breaks_ties(self, http):
        http({
            "https://venue.example": (
                b'<html><head><link rel="alternate" type="text/calendar" '
                b'href="/declared.ics"></head>'
                b'<body><a href="/linked.ics">ical</a></body></html>'
            ),
            "https://venue.example/declared.ics": _ics(1, 2),
            "https://venue.example/linked.ics": _ics(3, 4),
        })
        found = probe_feeds("https://venue.example")
        assert found[0].origin == "link-rel", "a declared feed outranks a scraped anchor"

    def test_rank_key_ordering(self):
        declared = FeedCandidate(url="a", origin="link-rel", future_events=2, events=2)
        guessed = FeedCandidate(url="b", origin="blind", future_events=2, events=2)
        richer = FeedCandidate(url="c", origin="blind", future_events=9, events=9)
        assert sorted([guessed, richer, declared], key=lambda c: c.rank_key)[0] is richer
        assert sorted([guessed, declared], key=lambda c: c.rank_key)[0] is declared
