"""Tests for the location filter.

Every case below is a real location string from a live harvest on 2026-08-12,
because the point of this module is that the same city arrives in four shapes
and a naive `"San Francisco" in text` finds only one of them.
"""

from __future__ import annotations

import pytest

from event_harvester.outputs.regions import (
    BROADER,
    OAKLAND,
    SF,
    UNKNOWN,
    classify,
)


class TestSanFrancisco:
    @pytest.mark.parametrize("loc", [
        "San Francisco, CA",
        "San Francisco, California",
        "Roxie Theater, San Francisco, CA",
        "The STUD, San Francisco",
        "Mother Bar, SF",
        "QBar SF",
    ])
    def test_by_name(self, loc):
        assert classify(loc) == SF

    @pytest.mark.parametrize("loc", [
        "484 5th Street, San Francisco, CA 94107",
        "1691 John F Kennedy Drive, San Francisco, CA 94121",
        "Mission Bowling Club, 3176 17th Street, San Francisco, CA 94110",
        "Palace of Fine Arts, 3301 Lyon Street, San Francisco, CA 94123",
    ])
    def test_by_zip(self, loc):
        assert classify(loc) == SF

    @pytest.mark.parametrize("loc", ["El Rio", "El Rio, Front Room", "The Stud",
                                     "The Bank", "Club Six - Both Floors!"])
    def test_a_bare_venue_with_no_city(self, loc):
        """The `raw` venue sources are SF bars scraping their own homepages,
        and a bar does not write its own city on its own site. Without the
        venue table these land in "unplaced" — which is precisely where the
        local nightlife would end up."""
        assert classify(loc) == SF

    def test_a_venue_known_only_from_the_source(self):
        """Several venue posts carry no location at all."""
        assert classify("", "@rileyvicexo in Web / instagram/@jolenessf") == SF


class TestEastBay:
    @pytest.mark.parametrize("loc", [
        "Oakland, CA", "The New Parish, Oakland",
        "2347 Prospect St, Berkeley, CA 94704",
        "Emeryville, CA", "White Horse Inn",
    ])
    def test_it_is_oakland(self, loc):
        assert classify(loc) == OAKLAND

    def test_berkeley_by_zip(self):
        assert classify("1900 Shattuck Ave, CA 94704") == OAKLAND


class TestBroader:
    @pytest.mark.parametrize("loc", [
        "Microsoft, Mountain View, CA",
        "Sacramento, CA",
        "Jessup University, 2121 University Avenue, Rocklin, CA 95765",
        "Aerospike HQ, 2440 W. El Camino Real, 7th Floor, Mountain View, CA 94040",
        "Singapore",
    ])
    def test_identifiable_elsewhere(self, loc):
        assert classify(loc) == BROADER

    def test_a_named_place_beats_a_venue_substring(self):
        """"DoubleTree San Jose" must not match an SF venue and be filed local."""
        assert classify("DoubleTree San Jose, TBD") == BROADER


class TestUnknown:
    def test_an_empty_location_with_no_usable_source(self):
        assert classify("", "noreply@autodesk.com in Gmail/INBOX") == UNKNOWN

    def test_an_unplaceable_venue(self):
        assert classify("DEF CON, TBD") == UNKNOWN

    def test_online_is_not_a_place(self):
        """Kept out of `broader` so "elsewhere" does not fill with Zoom links."""
        for loc in ("Online Event", "Zoom", "YouTube", "Virtual"):
            assert classify(loc) == UNKNOWN

    def test_unknown_is_distinct_from_broader(self):
        """"could not tell" and "definitely elsewhere" are different answers,
        and only one of them is a reason to improve this module."""
        assert classify("DEF CON, TBD") != classify("Sacramento, CA")


class TestRobustness:
    def test_case_does_not_matter(self):
        assert classify("SAN FRANCISCO, CA") == SF
        assert classify("oakland") == OAKLAND

    def test_none_and_empty_are_unknown(self):
        assert classify(None) == UNKNOWN
        assert classify("") == UNKNOWN
        assert classify("   ") == UNKNOWN

    def test_a_zip_outside_the_bay_is_broader_not_unknown(self):
        assert classify("123 Main St, Springfield, IL 62704") == BROADER

    def test_every_result_is_a_known_region(self):
        from event_harvester.outputs.regions import REGIONS
        for loc in ["", "SF", "Oakland", "Sacramento", "???", "Online"]:
            assert classify(loc) in REGIONS


class TestReportIntegration:
    def test_the_page_gets_a_place_filter(self, tmp_path):
        from event_harvester.outputs.html_report import generate_html_report

        p = tmp_path / "r.html"
        generate_html_report([
            {"title": "A", "date": "2026-09-01", "location": "San Francisco, CA"},
            {"title": "B", "date": "2026-09-01", "location": "Oakland, CA"},
            {"title": "C", "date": "2026-09-01", "location": "Sacramento, CA"},
        ], output_path=str(p))
        head = p.read_text(encoding="utf-8").split("<main")[0]
        assert 'data-region="sf"' in head
        assert 'data-region="oakland"' in head
        assert 'data-region="broader"' in head
        assert 'data-region=""' in head, "an explicit Everywhere chip"

    def test_a_region_with_no_events_gets_no_chip(self, tmp_path):
        from event_harvester.outputs.html_report import generate_html_report

        p = tmp_path / "r.html"
        generate_html_report(
            [{"title": "A", "date": "2026-09-01", "location": "San Francisco, CA"}],
            output_path=str(p),
        )
        head = p.read_text(encoding="utf-8").split("<main")[0]
        assert 'data-region="oakland"' not in head

    def test_the_two_axes_are_independent(self, tmp_path):
        """Region and source must AND, not replace one another."""
        from event_harvester.outputs.html_report import generate_html_report

        p = tmp_path / "r.html"
        generate_html_report(
            [{"title": "A", "location": "SF", "_platforms": ["web"]}],
            output_path=str(p),
        )
        js = p.read_text(encoding="utf-8")
        body = js.split("function matching()")[1].split("\n}")[0]
        assert "activeSource" in body and "activeRegion" in body
