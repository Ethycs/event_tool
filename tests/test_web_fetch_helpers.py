"""Characterization tests for the pure helpers backing the web modes.

These pin down the extraction primitives (Calcium parser, JSON-LD,
Instagram GraphQL walker, text decimation, cooldowns, junk-title
filter, fingerprint filter, dead-page detector) so the
sources/web_modes/ split can't regress LLM-input quality.

None of these tests touch Playwright. They exercise the
"data-in / data-out" surface that used to live in web_fetch.py and
now lives in web_utils.py + the per-mode files.

Import note: tests target the canonical homes (web_utils for shared
helpers, web_modes.calendar for _parse_calcium_links, web_modes.feed
for _extract_posts_from_api). monkeypatch needs the real binding, not
a re-export, so the back-compat shim in web_fetch.py isn't usable here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from event_harvester.sources import web_utils
from event_harvester.sources.web_modes.calendar import _parse_calcium_links
from event_harvester.sources.web_modes.feed import _extract_posts_from_api
from event_harvester.sources.web_utils import (
    _decimate_text,
    _extract_structured_data,
    _extract_text,
    _filter_known_links,
    _is_dead_page,
    _is_junk_link_title,
    _is_on_cooldown,
    _load_cooldowns,
    _load_web_sources,
    _save_cooldown,
)

# ── _is_junk_link_title ──────────────────────────────────────────────


@pytest.mark.parametrize("title", [
    "",
    "  ",
    "next",
    "View All",
    "VIEW MORE",
    "menu",
    "abc",  # < 5 chars
])
def test_is_junk_link_title_rejects(title):
    assert _is_junk_link_title(title) is True


@pytest.mark.parametrize("title", [
    "AI Meetup San Francisco",
    "Friday Night Live Jazz",
    "Open Source Summit 2026",
])
def test_is_junk_link_title_accepts(title):
    assert _is_junk_link_title(title) is False


# ── cooldown round trip ──────────────────────────────────────────────


@pytest.fixture
def cooldown_in_tmp(tmp_path, monkeypatch):
    """Redirect _COOLDOWN_FILE to a tmp path so tests don't write to data/."""
    target = tmp_path / "cooldowns.json"
    monkeypatch.setattr(web_utils, "_COOLDOWN_FILE", target)
    return target


def test_load_cooldowns_missing_file_returns_empty(cooldown_in_tmp):
    assert _load_cooldowns() == {}


def test_load_cooldowns_corrupt_file_returns_empty(cooldown_in_tmp):
    cooldown_in_tmp.write_text("not json {", encoding="utf-8")
    assert _load_cooldowns() == {}


def test_save_and_load_cooldown_round_trip(cooldown_in_tmp):
    _save_cooldown("https://example.com/a")
    _save_cooldown("https://example.com/b")
    data = _load_cooldowns()
    assert set(data.keys()) == {"https://example.com/a", "https://example.com/b"}
    # Timestamps parse as ISO-8601
    for ts in data.values():
        datetime.fromisoformat(ts)


def test_is_on_cooldown_true_when_just_saved(cooldown_in_tmp):
    _save_cooldown("https://example.com/recent")
    assert _is_on_cooldown("https://example.com/recent") is True


def test_is_on_cooldown_false_when_outside_window(cooldown_in_tmp):
    # Write a stale timestamp manually
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cooldown_in_tmp.write_text(
        json.dumps({"https://example.com/old": stale}), encoding="utf-8",
    )
    assert _is_on_cooldown("https://example.com/old", cooldown=timedelta(hours=1)) is False


def test_is_on_cooldown_false_for_unknown_url(cooldown_in_tmp):
    assert _is_on_cooldown("https://never-saved.com") is False


# ── _load_web_sources fallback ───────────────────────────────────────


def test_load_web_sources_when_file_missing_returns_empty_dict(monkeypatch, tmp_path):
    """Documents a latent bug: load_json(_, default=None) returns {} on
    missing file (not None), so _load_web_sources's `is not None` check
    never triggers the _DEFAULT_SOURCES fallback. Pinning current behavior
    so a fix later is intentional and visible.
    """
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(web_utils, "_WEB_SOURCES_FILE", missing)
    sources = _load_web_sources()
    assert sources == {}  # ← would be list(_DEFAULT_SOURCES) if the check used `if not sources`


def test_load_web_sources_reads_user_file(monkeypatch, tmp_path):
    src_file = tmp_path / "web_sources.json"
    src_file.write_text(json.dumps([
        {"url": "https://x.com", "name": "x", "mode": "raw"},
    ]), encoding="utf-8")
    monkeypatch.setattr(web_utils, "_WEB_SOURCES_FILE", src_file)
    sources = _load_web_sources()
    assert isinstance(sources, list)
    assert sources[0]["name"] == "x"


# ── _filter_known_links ──────────────────────────────────────────────


def test_filter_known_links_empty_store_passes_through(monkeypatch):
    monkeypatch.setattr(
        "event_harvester.pipeline.event_match.load_fingerprints",
        lambda: [],
    )
    links = [
        {"url": "https://x/1", "text": "Event A", "date_hint": "2026-06-01"},
        {"url": "https://x/2", "text": "Event B", "date_hint": "2026-06-02"},
    ]
    assert _filter_known_links(links) == links


def test_filter_known_links_drops_exact_url_match(monkeypatch):
    """A link the user has DECIDED on is skipped before the detail fetch."""
    monkeypatch.setattr(
        "event_harvester.pipeline.event_match.load_fingerprints",
        lambda: [{
            "link": "https://x/1", "title": "anything",
            "date": "2026-06-01", "status": "approved",
        }],
    )
    links = [
        {"url": "https://x/1", "text": "Event A", "date_hint": "2026-06-01"},
        {"url": "https://x/2", "text": "Event B", "date_hint": "2026-06-02"},
    ]
    result = _filter_known_links(links)
    assert len(result) == 1
    assert result[0]["url"] == "https://x/2"


def test_filter_known_links_ignores_auto_fingerprints(monkeypatch):
    """A prior *sighting* must NOT hide an event the user never decided on.

    Regression test. _save_link_fingerprint used to fingerprint every detail
    page it fetched, and _filter_known_links honoured those entries, so an
    event seen once was never shown again -- which silently hid 90%+ of the
    calendar. Only approved/declined count as "known" now; status-less
    (legacy auto) entries are ignored.
    """
    monkeypatch.setattr(
        "event_harvester.pipeline.event_match.load_fingerprints",
        lambda: [
            {"link": "https://x/1", "title": "Event A", "date": "2026-06-01"},
            {"link": "https://x/2", "title": "Event B",
             "date": "2026-06-02", "status": "auto"},
        ],
    )
    links = [
        {"url": "https://x/1", "text": "Event A", "date_hint": "2026-06-01"},
        {"url": "https://x/2", "text": "Event B", "date_hint": "2026-06-02"},
    ]
    assert _filter_known_links(links) == links


def test_filter_known_links_drops_declined(monkeypatch):
    """Declined events stay skipped -- the user said no."""
    monkeypatch.setattr(
        "event_harvester.pipeline.event_match.load_fingerprints",
        lambda: [{
            "link": "https://x/2", "title": "Event B",
            "date": "2026-06-02", "status": "declined",
        }],
    )
    links = [
        {"url": "https://x/1", "text": "Event A", "date_hint": "2026-06-01"},
        {"url": "https://x/2", "text": "Event B", "date_hint": "2026-06-02"},
    ]
    result = _filter_known_links(links)
    assert [l["url"] for l in result] == ["https://x/1"]


# ── _parse_calcium_links (CALENDAR mode core) ────────────────────────


_CALCIUM_FIXTURE = """
<html>
<body>
<table class="c_WickedGrounds">
  <tr><td class="TimeLabel">8:00 PM</td></tr>
  <noscript><a href="/event?ID=42&amp;Date=2026%2F6%2F15">Bondage 101</a></noscript>
</table>
<table class="c_PowerExchange">
  <tr><td class="TimeLabel">10:00 PM</td></tr>
  <noscript><a href="/event?ID=43&amp;Date=2026%2F6%2F16">Latex Night</a></noscript>
</table>
<table class="c_WickedGrounds">
  <tr><td class="TimeLabel">8:00 PM</td></tr>
  <noscript><a href="/event?ID=42&amp;Date=2026%2F6%2F15">Bondage 101 (dup)</a></noscript>
</table>
</body>
</html>
"""


def test_parse_calcium_links_extracts_event_metadata():
    links = _parse_calcium_links(_CALCIUM_FIXTURE)
    assert len(links) == 2  # third is deduped by ID+date
    a, b = links
    assert a["text"] == "Bondage 101"
    assert a["date_hint"] == "2026-06-15"
    assert a["time"] == "8:00 PM"
    assert a["venue"] == "Wicked Grounds"  # CamelCase split
    assert "ID=42" in a["url"]
    assert b["venue"] == "Power Exchange"


def test_parse_calcium_links_empty_html_returns_empty():
    assert _parse_calcium_links("<html></html>") == []


def test_parse_calcium_links_unescapes_html_entities():
    html = """
<table class="c_Venue">
  <noscript><a href="/x?Date=2026%2F1%2F2&amp;ID=1">Tom &amp; Jerry Night</a></noscript>
</table>
"""
    links = _parse_calcium_links(html)
    assert len(links) == 1
    assert links[0]["text"] == "Tom & Jerry Night"
    assert "&amp;" not in links[0]["url"]


# ── _extract_posts_from_api (FEED mode core) ─────────────────────────


def test_extract_posts_from_instagram_edges_shape():
    """Instagram GraphQL: data → user → edge_owner_to_timeline_media → edges → node → media."""
    api_response = {
        "data": {
            "user": {
                "edge_owner_to_timeline_media": {
                    "edges": [
                        {
                            "node": {
                                "media": {
                                    "id": "12345",
                                    "user": {"username": "lumasf"},
                                    "caption": {"text": "Join us at the AI meetup Friday June 14 7pm!"},
                                    "taken_at": 1718380800,
                                }
                            }
                        },
                        {
                            "node": {
                                "media": {
                                    "id": "12346",
                                    "user": {"username": "lumasf"},
                                    "caption": {"text": "short"},  # too short, dropped
                                }
                            }
                        },
                    ]
                }
            }
        }
    }
    posts = _extract_posts_from_api(api_response, "https://instagram.com", "instagram")
    assert len(posts) == 1
    p = posts[0]
    assert p["author"] == "@lumasf"
    assert "AI meetup" in p["content"]
    assert p["id"] == "ig:12345"
    assert p["channel"] == "instagram-feed"
    # `platform` is the constant "web" per EH-STD-MSG-v0; the source name
    # rides in `channel`. This test used to pass "web" as the third argument
    # — the value the *caller* should have been sending — so it asserted the
    # right shape while production sent the source name and got
    # `platform: "instagram"`. Passing the real source name here is what makes
    # the assertion mean anything.
    assert p["platform"] == "web"


def test_extract_posts_handles_items_shape():
    api_response = {
        "items": [
            {
                "id": "p99",
                "user": {"username": "alice"},
                "caption": {"text": "Tech mixer next Tuesday at the Mission"},
            }
        ]
    }
    posts = _extract_posts_from_api(api_response, "https://x.com", "instagram")
    assert len(posts) == 1
    assert posts[0]["author"] == "@alice"
    assert posts[0]["id"] == "ig:p99"


def test_extract_posts_skips_missing_caption():
    api_response = {"data": {"edges": [{"node": {"media": {"id": "x"}}}]}}
    assert _extract_posts_from_api(api_response, "https://x.com", "instagram") == []


def test_extract_posts_recurses_into_nested_dicts():
    """The walker recurses up to depth 5 to find edges in nested API responses."""
    api_response = {
        "wrapper": {
            "inner": {
                "edges": [{
                    "node": {
                        "media": {
                            "id": "deep1",
                            "user": {"username": "nested"},
                            "caption": "Nested event tomorrow",  # caption as string, not dict
                        }
                    }
                }]
            }
        }
    }
    posts = _extract_posts_from_api(api_response, "https://x.com", "instagram")
    assert len(posts) == 1
    assert posts[0]["id"] == "ig:deep1"


# ── _extract_text (RAW mode core) ────────────────────────────────────


def test_extract_text_strips_tags():
    html = "<p>Hello <b>world</b></p>"
    assert _extract_text(html) == "Hello world"


def test_extract_text_strips_script_and_style_blocks():
    html = "<html><script>var x = 1;</script><p>Visible</p><style>p{color:red}</style></html>"
    out = _extract_text(html)
    assert "var x" not in out
    assert "color:red" not in out
    assert "Visible" in out


def test_extract_text_collapses_whitespace():
    html = "<p>A\n\n  B\t\tC</p>"
    assert _extract_text(html) == "A B C"


# ── _decimate_text ───────────────────────────────────────────────────


def test_decimate_text_short_passes_through():
    text = "Short event on Friday June 14, 7pm at the Mission."
    assert _decimate_text(text, max_chars=2000) == text


def test_decimate_text_long_extracts_date_cluster():
    """When the text exceeds max_chars, the algorithm centers the output
    region on the densest date cluster (with up to 300 chars of leading
    context). The event region must be fully included; far-distant nav
    must be excluded.
    """
    unique_nav_marker = "UNIQUE_NAV_HEADER_TOKEN "
    nav = unique_nav_marker + "Home About Login Sign Up " * 50  # ~1275 chars
    body = "AI Meetup Friday June 14, 2026 at 7pm at the Mission, San Francisco. "
    event_region = body * 5  # ~350 chars of dense dates
    unique_footer_marker = " UNIQUE_FOOTER_TOKEN"
    footer = "Footer Privacy Terms " * 50 + unique_footer_marker  # ~1070 chars
    text = nav + event_region + footer
    out = _decimate_text(text, max_chars=1000)
    assert len(out) <= 1300  # max_chars + context_before slack
    assert "AI Meetup" in out
    assert unique_nav_marker not in out      # nav header far away → excluded
    assert unique_footer_marker not in out   # footer tail far away → excluded


def test_decimate_text_no_dates_falls_back_to_middle():
    text = "x" * 5000
    out = _decimate_text(text, max_chars=2000)
    assert len(out) == 2000
    # Starts at len/5, so position 1000
    assert out == "x" * 2000


# ── _extract_structured_data (JSON-LD) ───────────────────────────────


def test_extract_structured_data_finds_event():
    html = """<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Event",
  "name": "AI Summit 2026",
  "startDate": "2026-06-14T19:00",
  "endDate": "2026-06-14T22:00",
  "description": "Talks and networking.",
  "url": "https://example.com/summit",
  "location": {
    "@type": "Place",
    "name": "Mission Hall",
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "123 Main St",
      "addressLocality": "San Francisco"
    }
  }
}
</script>
</head></html>"""
    out = _extract_structured_data(html)
    assert out is not None
    assert out["title"] == "AI Summit 2026"
    assert out["date"] == "2026-06-14"
    assert out["time"] == "19:00 - 22:00"
    assert "Mission Hall" in out["location"]
    assert "123 Main St" in out["location"]
    assert out["link"] == "https://example.com/summit"


def test_extract_structured_data_handles_graph_wrapper():
    html = """<script type="application/ld+json">
{"@context":"x","@graph":[
  {"@type":"WebPage","name":"ignored"},
  {"@type":"Event","name":"Real event","startDate":"2026-07-01"}
]}</script>"""
    out = _extract_structured_data(html)
    assert out is not None
    assert out["title"] == "Real event"
    assert out["date"] == "2026-07-01"


def test_extract_structured_data_handles_type_list():
    html = """<script type="application/ld+json">
{"@type":["Event","BroadcastEvent"],"name":"Stream","startDate":"2026-08-01"}
</script>"""
    out = _extract_structured_data(html)
    assert out is not None
    assert out["title"] == "Stream"


def test_extract_structured_data_returns_none_when_absent():
    assert _extract_structured_data("<html><body>no ld+json</body></html>") is None


def test_extract_structured_data_tolerates_bad_json():
    html = """<script type="application/ld+json">{not json}</script>"""
    assert _extract_structured_data(html) is None


# ── _is_dead_page ────────────────────────────────────────────────────


@pytest.mark.parametrize("html_fragment", [
    "<p>This event has been deleted</p>",
    "<p>Event removed by organizer</p>",
    "<h1>404 Not Found</h1>",
    "<p>page not found</p>",
    "<p>This event is no longer available</p>",
    "<p>Event has been cancelled</p>",
])
def test_is_dead_page_detects(html_fragment):
    assert _is_dead_page(html_fragment) is True


@pytest.mark.parametrize("html_fragment", [
    "<p>AI Meetup tonight at 7pm</p>",
    "<p>Sold out — join the waitlist</p>",
])
def test_is_dead_page_negative(html_fragment):
    assert _is_dead_page(html_fragment) is False


def test_is_dead_page_only_scans_first_3000_chars():
    """A "page not found" deep in the page (after 3000 chars) should NOT trigger."""
    prelude = "x " * 1600  # 3200 chars
    html = prelude + "<p>page not found</p>"
    assert _is_dead_page(html) is False


# ── search-mode query normalization ──────────────────────────────────


class TestNormalizeQueries:
    """`search` mode accepted exactly one query string.

    That is one keyhole into a large site — `meetup` shipped configured as
    `"query": "AI meetup"` and saw nothing else, not because the site was
    hard but because the schema could only hold one string.
    """

    def _norm(self, src):
        from event_harvester.sources.web_modes.search import _normalize_queries
        return _normalize_queries(src)

    def test_single_string_still_works(self):
        assert self._norm({"query": "AI meetup"}) == ["AI meetup"]

    def test_list_under_query(self):
        assert self._norm({"query": ["ai", "queer party"]}) == ["ai", "queer party"]

    def test_queries_key_is_preferred(self):
        src = {"queries": ["a", "b"], "query": "ignored"}
        assert self._norm(src) == ["a", "b"]

    def test_absent_means_no_search(self):
        assert self._norm({}) == []
        assert self._norm({"query": None}) == []

    def test_blanks_and_duplicates_are_dropped(self):
        assert self._norm({"queries": ["a", "  ", "a", " b "]}) == ["a", "b"]

    def test_non_strings_are_coerced(self):
        assert self._norm({"queries": [2026, "x"]}) == ["2026", "x"]
