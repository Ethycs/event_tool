"""Tests for RSS mode.

The mode exists because `raw` cannot do this job: Bottom of the Hill's
calendar page is ~40 shows in one 338 KB table, and scraped as a single
30,164-character message the LLM returned exactly one event from it — the page
itself. One message per show is the point, so the tests are about the two
things that decide whether that works: getting the *event* date (not the
publication date) and keeping the archive out.
"""

from __future__ import annotations

from datetime import date

import pytest

from event_harvester.sources.web_modes import rss


def _feed(items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>t</title>'
        f"{items}</channel></rss>"
    ).encode("utf-8")


def _item(title: str, link: str = "", desc: str = "", guid: str = "") -> str:
    return (
        "<item>"
        f"<title>{title}</title>"
        f"<link>{link}</link>"
        f"<description>{desc}</description>"
        f'<guid isPermaLink="false">{guid}</guid>'
        "</item>"
    )


class TestTheEventDate:
    """`pubDate` is when the listing was edited, not when the show is.

    A November show routinely carries an August pubDate, so using it would put
    every event on the wrong day and break every date filter downstream.
    """

    def test_the_link_is_the_primary_source(self):
        got = rss.event_date_from(
            "http://www.bottomofthehill.com/20261103.html", "irrelevant"
        )
        assert got == date(2026, 11, 3)

    def test_the_title_is_the_fallback(self):
        assert rss.event_date_from("", "2026  11/03  :  Sex Mex") == date(2026, 11, 3)

    def test_the_link_wins_over_the_title(self):
        got = rss.event_date_from(
            "http://x/20261103.html", "2026  01/01  : stale title"
        )
        assert got == date(2026, 11, 3)

    def test_a_multi_night_link_suffix_still_parses(self):
        """`2026082023.html` is the 8/20-8/23 festival page."""
        assert rss.event_date_from("http://x/2026082023.html", "") == date(2026, 8, 20)

    def test_an_impossible_date_is_none_not_a_crash(self):
        assert rss.event_date_from("http://x/20261345.html", "") is None

    def test_no_date_anywhere_is_none(self):
        assert rss.event_date_from("http://x/tickets.html", "Some Band") is None


class TestTheWindow:
    """The feed is an archive — 964 items going back years, measured."""

    @staticmethod
    def _run(payload, today=date(2026, 8, 7), **over):
        return self_run(payload, today, **over)

    def test_past_shows_are_dropped(self, monkeypatch):
        payload = _feed(
            _item("old", "http://x/20200101.html", guid="a")
            + _item("soon", "http://x/20260901.html", guid="b")
        )
        _patch_get(monkeypatch, payload)
        out = self._run(payload)
        assert [m["id"] for m in out] == ["rss:v:b"]

    def test_far_future_shows_are_dropped(self, monkeypatch):
        payload = _feed(
            _item("way out", "http://x/20991231.html", guid="a")
            + _item("soon", "http://x/20260901.html", guid="b")
        )
        _patch_get(monkeypatch, payload)
        out = self._run(payload)
        assert [m["id"] for m in out] == ["rss:v:b"]

    def test_the_window_is_configurable(self, monkeypatch):
        payload = _feed(_item("later", "http://x/20261201.html", guid="a"))
        _patch_get(monkeypatch, payload)
        assert self._run(payload, forward_days=5) == []
        assert len(self._run(payload, forward_days=365)) == 1

    def test_a_show_earlier_today_survives(self, monkeypatch):
        """back_days exists so a timezone edge does not drop tonight's gig."""
        payload = _feed(_item("tonight", "http://x/20260807.html", guid="a"))
        _patch_get(monkeypatch, payload)
        assert len(self._run(payload)) == 1

    def test_an_undated_item_is_kept_not_discarded(self, monkeypatch):
        """More likely a real listing than noise; the LLM can still read it."""
        payload = _feed(_item("Mystery show", "http://x/tickets.html", guid="a"))
        _patch_get(monkeypatch, payload)
        assert len(self._run(payload)) == 1


class TestContent:
    def test_a_show_becomes_one_readable_message(self, monkeypatch):
        payload = _feed(_item(
            "2026  11/03  :  Sex Mex : Sutros",
            "http://x/20261103.html",
            desc="&lt;b&gt;Sex Mex &lt;br /&gt;Sutros&lt;/b&gt;&lt;br /&gt;7:30PM doors",
            guid="g1",
        ))
        _patch_get(monkeypatch, payload)
        msg = self_run(payload)[0]
        assert msg["platform"] == "web"
        assert "Date: 2026-11-03" in msg["content"]
        assert "URL: http://x/20261103.html" in msg["content"]
        assert "Sex Mex" in msg["content"]
        assert "7:30PM doors" in msg["content"]
        assert "<b>" not in msg["content"], "tags must be stripped"
        assert "&lt;" not in msg["content"], "entities must be unescaped"

    def test_the_timestamp_is_the_show_date(self, monkeypatch):
        payload = _feed(_item("x", "http://x/20261103.html", guid="g"))
        _patch_get(monkeypatch, payload)
        assert self_run(payload)[0]["timestamp"].startswith("2026-11-03")

    def test_ids_are_stable_across_runs(self, monkeypatch):
        payload = _feed(_item("x", "http://x/20260901.html", guid="stable-guid"))
        _patch_get(monkeypatch, payload)
        first = self_run(payload)[0]["id"]
        second = self_run(payload)[0]["id"]
        assert first == second == "rss:v:stable-guid"


class TestRobustness:
    def test_a_dtd_is_refused_rather_than_parsed(self):
        """No legitimate feed needs one, and it is where entity attacks live."""
        payload = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "b">]><rss/>'
        with pytest.raises(ValueError, match="DTD"):
            rss._parse_items(payload)

    def test_a_fetch_failure_is_empty_not_an_exception(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(rss.requests, "get", boom)
        assert rss._do_rss_inner({"url": "https://x", "name": "v"}) == []

    def test_malformed_xml_is_empty_not_an_exception(self, monkeypatch):
        _patch_get(monkeypatch, b"<rss><channel>truncated")
        assert rss._do_rss_inner({"url": "https://x", "name": "v"}) == []

    def test_an_item_with_no_title_is_skipped(self, monkeypatch):
        payload = _feed(_item("", "http://x/20260901.html", guid="a"))
        _patch_get(monkeypatch, payload)
        assert rss._do_rss_inner(
            {"url": "https://x", "name": "v"}, date(2026, 8, 7)
        ) == []

    def test_atom_entries_are_understood_too(self, monkeypatch):
        payload = (
            '<?xml version="1.0"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><title>2026  09/01  : Band</title>"
            '<link href="http://x/20260901.html"/>'
            "<summary>desc</summary><id>atom-1</id></entry>"
            "</feed>"
        ).encode()
        _patch_get(monkeypatch, payload)
        out = rss._do_rss_inner({"url": "https://x", "name": "v"}, date(2026, 8, 7))
        assert len(out) == 1
        assert "Band" in out[0]["content"]


class TestDescriptionCleaning:
    def test_breaks_become_newlines(self):
        assert "\n" in rss.clean_description("a&lt;br /&gt;b")

    def test_output_is_bounded(self):
        assert len(rss.clean_description("x" * 5000, limit=100)) <= 100

    def test_empty_is_empty(self):
        assert rss.clean_description("") == ""
        assert rss.clean_description(None) == ""


# ── helpers ───────────────────────────────────────────────────────────────


def _patch_get(monkeypatch, payload: bytes):
    class Response:
        content = payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr(rss.requests, "get", lambda *a, **k: Response())


def self_run(payload, today=date(2026, 8, 7), **over):
    src = {"url": "https://x/RSS.xml", "name": "v", **over}
    return rss._do_rss_inner(src, today)
