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
import re
from datetime import datetime, timedelta, timezone

import pytest

from event_harvester.sources import web_utils
from event_harvester.sources.web_modes.calendar import _hint_header, _parse_calcium_links
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


def test_filter_known_links_a_decision_on_one_date_of_a_series_keeps_the_rest(monkeypatch):
    """One ticket URL can serve every date of a series (Vox Body's monthly
    jams), so the URL alone must not hide the other dates."""
    url = "https://buytickets.at/voxbodystudio/2347339"
    monkeypatch.setattr(
        "event_harvester.pipeline.event_match.load_fingerprints",
        lambda: [{
            "link": url, "title": "Self-Tie Rope Jam",
            "date": "2026-11-18", "status": "declined",
        }],
    )
    links = [
        {"url": url, "text": "Self-Tie Rope Jam", "date_hint": "2026-11-18"},
        {"url": url, "text": "Self-Tie Rope Jam", "date_hint": "2026-12-16"},
    ]
    assert [lnk["date_hint"] for lnk in _filter_known_links(links)] == ["2026-12-16"]


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


# Regression for erobay, 2026-09-01: a whole class of events was silently
# dropped — those linking straight to an external ticket site (buytickets.at)
# instead of an internal Calcium page. They have no <noscript> fallback and no
# `Date=` in the URL, so both the noscript check and the per-link date lookup
# bailed. Real, e.g. "House of Braids: a POC-exclusive Rope Jam" (VoxBody). The
# day's date is available from the internal event above it in the same day cell.
def test_parse_calcium_links_external_ticket_event_inherits_the_day():
    html = """
<table class="c_WickedGrounds">
  <span class="TimeLabel">6:00pm - 8:00pm</span>
  <noscript><a href="/x?Op=PopupWindow&amp;Date=2026%2F9%2F1&amp;ID=99">Hoe With Purpose</a></noscript>
</table>
<table class="c_VoxBody">
  <span class="EventTag Category">VoxBody</span><br>
  <span class="TimeLabel">7:00pm - 10:00pm</span><br>
  <a href="https://buytickets.at/voxbodystudio/1986314">House of Braids: a POC-exclusive Rope Jam</a>
</table>
"""
    links = _parse_calcium_links(html)
    assert len(links) == 2
    ext = next(l for l in links if "buytickets.at" in l["url"])
    assert ext["text"] == "House of Braids: a POC-exclusive Rope Jam"
    assert ext["date_hint"] == "2026-09-01"        # carried from the day cell
    assert ext["time"] == "7:00pm - 10:00pm"
    assert ext["venue"] == "Vox Body"


# Regression for erobay, 2026-11-02: a title carrying inline markup read as no
# title (`[^<]+` stops at the first tag), so the row was dropped -- 4 upcoming
# rows, among them "Tangle! Mission Control" and "AURA".
def test_parse_calcium_links_reads_a_title_with_markup():
    html = """
<table class="c_Tangle">
  <noscript><a href="/x?Op=PopupWindow&amp;Date=2026%2F11%2F9&amp;ID=14346">Tangle! <br>Mission
Control Weekday<br>Peer Rope Jam</a></noscript>
</table>
"""
    links = _parse_calcium_links(html)
    assert [lnk["text"] for lnk in links] == ["Tangle! Mission Control Weekday Peer Rope Jam"]
    assert "ID=14346" in links[0]["url"]


def test_parse_calcium_links_reads_a_ticket_link_nested_in_the_noscript():
    # The noscript wraps the external ticket anchor; like its plain-titled
    # siblings, the row takes the ticket URL.
    html = """
<table class="c_FeederCalendar">
  <noscript><a href="/x?Op=PopupWindow&amp;Date=2026%2F12%2F11&amp;ID=3080">
    <a href="https://forbiddentickets.com/events/afterdark/9bce6fadde"
       onclick="window.open(this.href); return false;"><strong>AURA</strong><br/>
The Bay Area&#39;s Premier Event</a></a></noscript>
</table>
"""
    links = _parse_calcium_links(html)
    assert len(links) == 1
    assert links[0]["url"] == "https://forbiddentickets.com/events/afterdark/9bce6fadde"
    assert links[0]["text"] == "AURA The Bay Area's Premier Event"
    assert links[0]["date_hint"] == "2026-12-11"


# ── _hint_header (what the listing knew, handed to the detail page) ──
#
# Regression for erobay, 2026-08-29: **124 links fetched every run, 24 events
# on the page.** The fetch layer was healthy the whole time; the date the
# listing had already parsed was dropped when the message was built, and
# `rerank_messages` then binned the message as "no date/link" before
# extraction. The loss was invisible per source — the funnel line
# (`dropped ... 2151 no date/link`) is logged after every source has merged
# into one list.


class TestTheListingsDateReachesTheMessage:
    def test_the_header_leads_with_the_date(self):
        lnk = {"date_hint": "2026-09-05", "time": "8:00 PM", "venue": "Wicked Grounds"}
        assert _hint_header(lnk) == (
            "date: 2026-09-05\ntime: 8:00 PM\nlocation: Wicked Grounds\n"
        )

    def test_it_matches_the_shape_the_inline_branch_emits(self):
        """Three branches build web messages and the extraction prompt reads
        all three. `date:`/`time:`/`location:`, in that order, is the one the
        inline branch has always used."""
        keys = re.findall(r"(\w+): ", _hint_header(
            {"date_hint": "2026-09-05", "time": "8pm", "venue": "The Stud"}))
        assert keys == ["date", "time", "location"]

    def test_a_link_with_no_hints_adds_nothing(self):
        """The detail text has to survive unchanged, or every fetched page
        grows a stray leading newline."""
        assert _hint_header({}) == ""
        assert _hint_header({"url": "https://x.test/1", "text": "Thing"}) == ""

    def test_partial_hints_do_not_emit_empty_lines(self):
        assert _hint_header({"date_hint": "2026-09-05"}) == "date: 2026-09-05\n"
        assert _hint_header({"venue": "The Stud"}) == "location: The Stud\n"

    def test_every_calcium_link_carries_the_date_it_needs(self):
        """`_parse_calcium_links` skips any block with no `Date=` in the URL,
        so this is total, not typical — which is what makes dropping it a
        pure loss."""
        links = _parse_calcium_links(_CALCIUM_FIXTURE)
        assert links
        for lnk in links:
            assert lnk["date_hint"]
            assert _hint_header(lnk).startswith(f"date: {lnk['date_hint']}\n")


class TestTheHeaderIsWhatKeepsTheMessage:
    """The point of the header is surviving `rerank_messages`' pre-filter,
    which keeps a message only if it has a future date or an event link."""

    @staticmethod
    def _body() -> str:
        # A detail page written for someone who already knows what day they
        # clicked — the common case, and the one that was being dropped.
        return ("Join us for an evening of rope and conversation. Doors at 8. "
                "Members and newcomers welcome. Ask at the desk.")

    def test_without_the_date_the_message_is_dropped(self):
        from event_harvester.pipeline.reranker import _resolve_message_dates

        today = datetime.now().date()
        text = "time: 8:00 PM\nlocation: Wicked Grounds\n" + self._body()
        proximity, has_future, has_link = _resolve_message_dates(text, "", today)
        assert not (has_future or has_link), "fixture no longer reproduces the bug"
        assert proximity == 0

    def test_with_the_date_it_survives_and_scores(self):
        from event_harvester.pipeline.reranker import _resolve_message_dates

        today = datetime.now().date()
        lnk = {
            "date_hint": (today + timedelta(days=6)).isoformat(),
            "time": "8:00 PM",
            "venue": "Wicked Grounds",
        }
        proximity, has_future, has_link = _resolve_message_dates(
            _hint_header(lnk) + self._body(), "", today)
        assert has_future
        assert proximity > 0

    def test_a_past_date_is_still_dropped(self):
        """The header must not smuggle stale events past the filter — it
        supplies the date, it does not vouch for it."""
        from event_harvester.pipeline.reranker import _resolve_message_dates

        today = datetime.now().date()
        lnk = {"date_hint": (today - timedelta(days=30)).isoformat()}
        _, has_future, _ = _resolve_message_dates(
            _hint_header(lnk) + self._body(), "", today)
        assert not has_future


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


def test_extract_structured_data_does_not_repeat_the_venue_name():
    """Venues put the same string in `name` and `streetAddress`.

    Seen live 2026-08-10 via Gmail link expansion: "DG717, DG717, San
    Francisco" and "Microsoft, Microsoft, Mountain View".
    """
    html = """<html><script type="application/ld+json">
{"@type": "Event", "name": "Meetup", "startDate": "2026-08-18T17:30",
 "location": {"name": "DG717", "address": {
     "streetAddress": "DG717", "addressLocality": "San Francisco"}}}
</script></html>"""
    out = _extract_structured_data(html)
    assert out["location"] == "DG717, San Francisco"


def test_extract_structured_data_venue_dedup_is_case_insensitive():
    html = """<html><script type="application/ld+json">
{"@type": "Event", "name": "M", "startDate": "2026-08-18T17:30",
 "location": {"name": "The Bank", "address": {
     "streetAddress": "the bank", "addressLocality": "SF"}}}
</script></html>"""
    assert _extract_structured_data(html)["location"] == "The Bank, SF"


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


# ── the calendar row IS the event (Calcium) ──────────────────────────
#
# Added 2026-08-29 with the erobay rewrite. A Calcium row carries title, exact
# date, time, venue and a URL; we used to fetch 124 detail pages and then ask
# an LLM whether each was an event and whether it was past -- both already
# answered by the row, and both answered *wrongly* often enough to publish
# invented dates. These pin the row-to-message path and the forward window.


class TestTheForwardWindow:
    """The default Calcium grid is the CURRENT month, which is mostly behind
    us. Probed 2026-08-29: default grid `2026-07-26 -> 2026-09-05`, 8 forward
    days; `Date=2026/9/1` gave `2026-08-30 -> 2026-10-03`, 35 forward days."""

    def test_it_asks_for_next_month(self):
        from datetime import date as _date

        from event_harvester.sources.web_modes.calendar import _next_month_url
        url = "https://erobay.com/calendar/Calcium40.pl?Op=ShowIt&CalendarName=Community"
        assert _next_month_url(url, _date(2026, 8, 29)).endswith("&Date=2026%2F9%2F1")

    def test_december_rolls_the_year(self):
        from datetime import date as _date

        from event_harvester.sources.web_modes.calendar import _next_month_url
        assert _next_month_url("http://x/c.pl?a=1", _date(2026, 12, 3)).endswith(
            "&Date=2027%2F1%2F1")

    def test_it_starts_a_query_string_when_there_is_none(self):
        from datetime import date as _date

        from event_harvester.sources.web_modes.calendar import _next_month_url
        assert _next_month_url("http://x/c.pl", _date(2026, 8, 1)) == \
            "http://x/c.pl?Date=2026%2F9%2F1"


class TestCalciumWalksSeveralMonths:
    """One next-month grid still dropped everything from the third month on;
    erobay lists events 6+ months out. The walk covers `calendar_months`."""

    def test_month_url_offsets_including_the_current_month(self):
        from datetime import date as _date

        from event_harvester.sources.web_modes.calendar import _month_url
        u = "http://x/c.pl"
        assert _month_url(u, 0, _date(2026, 11, 5)).endswith("Date=2026%2F11%2F1")
        assert _month_url(u, 1, _date(2026, 11, 5)).endswith("Date=2026%2F12%2F1")
        assert _month_url(u, 2, _date(2026, 11, 5)).endswith("Date=2027%2F1%2F1")

    def test_december_offsets_roll_the_year(self):
        from datetime import date as _date

        from event_harvester.sources.web_modes.calendar import _month_url
        assert _month_url("http://x/c.pl", 3, _date(2026, 12, 1)).endswith(
            "Date=2027%2F3%2F1")

    def test_the_walk_visits_calendar_months_minus_one_grids(self, monkeypatch):
        """Default is DEFAULT_CALENDAR_MONTHS; the walk fetches the months after
        the current one, aggregating every grid's links."""
        from event_harvester.sources.web_modes import calendar as cal

        visited: list[str] = []

        class _Page:
            def close(self):
                pass

        class _UC:
            def open(self, url, **kw):
                visited.append(url)
                return _Page()

        monkeypatch.setattr(cal.web_checkpoint, "replay", lambda *a, **k: None)
        monkeypatch.setattr(cal.web_checkpoint, "record", lambda *a, **k: None)
        monkeypatch.setattr(cal, "_wait_cloudflare", lambda page, headless=False: "<cal>")
        monkeypatch.setattr(cal, "_parse_calcium_links",
                            lambda html: [{"url": "u", "date_hint": "2026-12-01"}])

        src = {"calendar_months": 4}
        links = cal._calcium_forward_months(_UC(), src, "http://x/c.pl", "erobay", None, None)
        assert len(visited) == 3                 # offsets 1,2,3 (current already fetched)
        assert len(links) == 3                   # one per visited grid

    def test_a_cloudflare_block_stops_the_walk(self, monkeypatch):
        from event_harvester.sources.web_modes import calendar as cal

        class _Page:
            def close(self):
                pass

        monkeypatch.setattr(cal.web_checkpoint, "replay", lambda *a, **k: None)
        monkeypatch.setattr(cal.web_checkpoint, "record", lambda *a, **k: None)
        monkeypatch.setattr(cal, "_wait_cloudflare",
                            lambda page, headless=False: "Just a moment...")
        monkeypatch.setattr(cal, "_parse_calcium_links", lambda html: [{"url": "u"}])

        class _UC:
            def open(self, url, **kw):
                return _Page()

        links = cal._calcium_forward_months(_UC(), {"calendar_months": 6},
                                            "http://x/c.pl", "erobay", None, None)
        assert links == []                       # blocked on the first extra month


class TestCalciumRowsBecomeMessages:
    @pytest.fixture(autouse=True)
    def _no_side_effects(self, monkeypatch):
        """`_save_link_fingerprint` writes to data/; the fetcher opens tabs."""
        import event_harvester.sources.web_modes.calendar as cal
        monkeypatch.setattr(cal, "_save_link_fingerprint", lambda lnk: None)
        monkeypatch.setattr(cal, "_filter_known_links", lambda links: links)
        monkeypatch.setattr(cal, "_fetch_event_details_parallel",
                            lambda *a, **k: pytest.fail("detail fetch must be opt-in"))

    @staticmethod
    def _row(day, title="Bondage 101", **kw):
        return {"url": f"https://e.test/?ID={abs(hash(title)) % 999}&Date={day}",
                "text": title, "date_hint": day, "time": "8:00 PM",
                "venue": "Wicked Grounds", **kw}

    def _run(self, links, **src):
        from event_harvester.sources.web_modes.calendar import _calcium_messages
        return _calcium_messages(None, {"name": "erobay", **src}, links, "e.test", 1000)

    def test_a_row_becomes_a_message_with_no_detail_page(self):
        today = datetime.now().date()
        msgs = self._run([self._row((today + timedelta(days=3)).isoformat())])
        assert len(msgs) == 1
        assert "Bondage 101" in msgs[0]["content"]
        # Title first, then labelled fields -- the order the inline branch
        # uses. Reversed, the extractor reads "Janus" + title as one string.
        assert msgs[0]["content"].startswith("Bondage 101\n")
        assert "\ndate: " in msgs[0]["content"]

    def test_past_rows_are_dropped_by_arithmetic_not_by_a_model(self):
        today = datetime.now().date()
        msgs = self._run([
            self._row((today - timedelta(days=1)).isoformat(), title="Yesterday"),
            self._row(today.isoformat(), title="Today"),
            self._row((today + timedelta(days=9)).isoformat(), title="Next week"),
        ])
        assert sorted(m["content"].splitlines()[0] for m in msgs) == ["Next week", "Today"]

    def test_the_timestamp_is_the_events_day_not_the_scrapes_clock(self):
        """Relative phrases resolve against this downstream, and a message
        stamped `now` puts every event on the day we happened to scrape."""
        day = (datetime.now().date() + timedelta(days=5)).isoformat()
        assert self._run([self._row(day)])[0]["timestamp"].startswith(day)

    def test_the_two_grids_overlap_and_are_deduped_by_occurrence(self):
        """A month view spills into its neighbours, so the same row arrives
        from both grids -- often under a different URL, because the query
        names the grid it came from (`DoneURL=`)."""
        day = (datetime.now().date() + timedelta(days=2)).isoformat()
        row = self._row(day)
        other_grid = {**row, "url": row["url"] + "&DoneURL=next-month"}
        assert len(self._run([row, dict(row), other_grid])) == 1

    def test_one_ticket_url_on_several_dates_is_several_rows(self):
        """Vox Body sells every monthly jam from one buytickets.at page; a
        URL-only key kept the first date and dropped the rest."""
        today = datetime.now().date()
        url = "https://buytickets.at/voxbodystudio/2347339"
        days = [(today + timedelta(days=n)).isoformat() for n in (3, 31)]
        msgs = self._run([{**self._row(d), "url": url} for d in days])
        assert sorted(m["timestamp"][:10] for m in msgs) == days
        assert len({m["id"] for m in msgs}) == 2

    def test_the_url_survives_as_the_events_page(self):
        """With no detail fetch the link *is* the detail."""
        day = (datetime.now().date() + timedelta(days=2)).isoformat()
        assert "e.test" in self._run([self._row(day)])[0]["channel"]

    def test_nothing_upcoming_yields_no_messages(self):
        old = (datetime.now().date() - timedelta(days=40)).isoformat()
        assert self._run([self._row(old)]) == []
