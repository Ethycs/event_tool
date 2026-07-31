"""Tests for link extraction provenance (`strategy` / `region`).

The JS that produces these fields cannot be exercised from pytest — it needs a
DOM. What *is* testable here is `region_key`, the canonicalisation every
downstream consumer will key on, and the tolerance of links that predate the
fields.
"""

from __future__ import annotations

from event_harvester.sources.web_utils import LINK_STRATEGIES, region_key


class TestRegionKey:
    def test_heading_wins_because_class_names_are_build_hashed(self):
        """Meetup/Eventbrite ship `css-1x2y3z`; a key built on that rots per deploy."""
        link = {
            "region": {
                "heading": "Popular near you",
                "landmark": "main",
                "path": "div>section:2>ul>li:4",
                "depth": 4,
            }
        }
        assert region_key(link) == "h:popular near you"

    def test_two_regions_under_different_headings_do_not_collide(self):
        results = {"region": {"heading": "Search results", "path": "div>section>ul"}}
        carousel = {"region": {"heading": "Popular near you", "path": "div>section>ul"}}
        assert region_key(results) != region_key(carousel), (
            "identical structure, different region — the heading is the only signal"
        )

    def test_the_same_region_keys_identically_across_page_loads(self):
        """Depth and sibling indices shift; the heading does not."""
        first = {"region": {"heading": "Search results", "path": "div>section:2>ul", "depth": 3}}
        later = {"region": {"heading": "Search Results", "path": "div>section:5>ul", "depth": 6}}
        assert region_key(first) == region_key(later)

    def test_falls_back_to_landmark_and_path_when_a_page_has_no_headings(self):
        link = {"region": {"heading": "", "landmark": "nav", "path": "div>nav>ul"}}
        assert region_key(link) == "l:nav|div>nav>ul"

    def test_falls_back_to_path_alone(self):
        assert region_key({"region": {"path": "div>ul>li"}}) == "p:div>ul>li"

    def test_a_link_with_no_region_yields_an_empty_key_not_a_crash(self):
        """Links produced before 2026-07-30, and any future path that forgets."""
        assert region_key({"url": "https://e/1"}) == ""
        assert region_key({"region": {}}) == ""
        assert region_key({"region": None}) == ""

    def test_keys_are_bounded(self):
        """A key is a metric/store dimension; unbounded strings are a cardinality bomb."""
        link = {"region": {"heading": "x" * 500}}
        assert len(region_key(link)) <= 63


class TestStrategies:
    def test_the_five_strategies_are_ordered_by_trustworthiness(self):
        """The order is the documented contract, not incidental."""
        assert LINK_STRATEGIES[0] == "js-embedded", "reads date+id from the href itself"
        assert LINK_STRATEGIES[-1] == "near-date-fallback", "accepts a date within 5 ancestors"
        assert len(LINK_STRATEGIES) == len(set(LINK_STRATEGIES)) == 5

    def test_every_strategy_named_in_the_js_is_declared_here(self):
        """A push site added to the JS without a name here would be invisible."""
        from pathlib import Path

        from event_harvester.utils import REPO_ROOT

        js = Path(REPO_ROOT, "ext", "helpers", "extract_event_links.js").read_text(encoding="utf-8")
        found = set()
        for line in js.splitlines():
            stripped = line.strip()
            if stripped.startswith("strategy:"):
                found.add(stripped.split("'")[1])
        declared = set(LINK_STRATEGIES)
        assert found == declared, f"JS has {found}, python declares {declared}"

    def test_every_results_push_carries_a_strategy(self):
        """Five push sites, five tags. A silent untagged one is the bug being fixed."""
        from pathlib import Path

        from event_harvester.utils import REPO_ROOT

        js = Path(REPO_ROOT, "ext", "helpers", "extract_event_links.js").read_text(encoding="utf-8")
        pushes = js.count("results.push({")
        tags = sum(1 for line in js.splitlines() if line.strip().startswith("strategy:"))
        assert pushes == tags == 5, f"{pushes} push sites, {tags} strategy tags"
