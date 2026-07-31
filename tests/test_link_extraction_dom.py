"""DOM-level tests for `ext/helpers/extract_event_links.js`.

The extractor needs a DOM, so it was previously untestable from pytest and
every bug in it had to be found by pointing a browser at a live site. Three
were, in one afternoon. This closes that gap **without a new dependency**:
Playwright is already required, and a real Chromium over fixture HTML is
offline, deterministic, and more faithful than a JS DOM shim — which returns
zeros from `getBoundingClientRect` and would silently pass the visibility
checks elsewhere in this codebase.

The fixture is served at a fake origin via request interception rather than
`set_content`, which leaves the page on `about:blank`: relative hrefs then fail
to resolve and the extractor's same-host check drops every link. Nothing here
touches the network.

Skipped when no browser is installed, so a checkout without
`playwright install` still runs a green suite.
"""

from __future__ import annotations

import json

import pytest

from event_harvester.sources.web_utils import _EVENT_LINKS_JS, region_key

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def page():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - depends on local install
            pytest.skip(f"chromium unavailable: {exc}")
        pg = browser.new_page()
        yield pg
        browser.close()


FIXTURE_ORIGIN = "https://fixture.test"


def extract(page, html: str) -> list[dict]:
    """Serve `html` at a fake origin and run the extractor against it."""
    page.route(
        f"{FIXTURE_ORIGIN}/**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto(f"{FIXTURE_ORIGIN}/listing", wait_until="load")
    return page.evaluate(_EVENT_LINKS_JS) or []


# A listing page shaped like the ones that motivated this: a promo carousel and
# a results list that are *structurally identical* — repeated siblings, each
# with a date and a link, each card titled with its own heading.
CAROUSEL_AND_RESULTS = """
<body>
  <main>
    <section>
      <h2>Popular near you</h2>
      <ul>
        <li><h3>Promoted Gala</h3><span>Aug 12</span>
            <a href="/e/promoted-gala">Promoted Gala</a></li>
        <li><h3>Sponsored Mixer</h3><span>Aug 12</span>
            <a href="/e/sponsored-mixer">Sponsored Mixer</a></li>
      </ul>
    </section>
    <section>
      <h2>Search results</h2>
      <ul>
        <li><h3>Queer Night</h3><span>Sep 03</span><a href="/e/queer-night">Queer Night</a></li>
        <li><h3>AI Meetup</h3><span>Sep 04</span><a href="/e/ai-meetup">AI Meetup</a></li>
        <li><h3>Drag Brunch</h3><span>Sep 05</span><a href="/e/drag-brunch">Drag Brunch</a></li>
      </ul>
    </section>
  </main>
</body>
"""


class TestRegionSeparatesCarouselFromResults:
    def test_a_cards_own_title_is_not_its_region(self, page):
        """The bug this file exists for.

        `regionOf` used to `querySelector` the card itself, and an event card's
        title is an <h3> — so every link got a region of one and nothing could
        be grouped. Measured on eventbrite.com: 27 links, 27 regions.
        """
        links = extract(page, CAROUSEL_AND_RESULTS)
        keys = [region_key(lnk) for lnk in links]
        assert len(set(keys)) < len(links), (
            f"{len(links)} links produced {len(set(keys))} regions — "
            "each card is being treated as its own region"
        )
        assert "h:queer night" not in keys, "a card title leaked in as a region"

    def test_the_two_sections_land_in_different_regions(self, page):
        links = extract(page, CAROUSEL_AND_RESULTS)
        by_url = {lnk["url"].rsplit("/", 1)[-1]: region_key(lnk) for lnk in links}
        assert by_url["promoted-gala"] != by_url["queer-night"]
        assert by_url["promoted-gala"] == by_url["sponsored-mixer"]

    def test_the_section_heading_is_what_gets_recorded(self, page):
        links = extract(page, CAROUSEL_AND_RESULTS)
        keys = {region_key(lnk) for lnk in links}
        assert "h:popular near you" in keys
        assert "h:search results" in keys

    def test_the_results_region_is_the_bigger_one(self, page):
        """Cold-start heuristic: the region with more distinct dates is content."""
        from collections import defaultdict

        links = extract(page, CAROUSEL_AND_RESULTS)
        dates = defaultdict(set)
        for lnk in links:
            dates[region_key(lnk)].add(lnk.get("date_hint", ""))
        assert len(dates["h:search results"]) > len(dates["h:popular near you"])


class TestProvenanceIsAlwaysPresent:
    def test_every_link_carries_a_strategy_and_a_region(self, page):
        links = extract(page, CAROUSEL_AND_RESULTS)
        assert links, "fixture produced no links at all"
        assert all(lnk.get("strategy") for lnk in links)
        assert all(isinstance(lnk.get("region"), dict) for lnk in links)

    def test_the_strategy_named_matches_the_path_taken(self, page):
        links = extract(page, CAROUSEL_AND_RESULTS)
        assert {lnk["strategy"] for lnk in links} == {"dated-card"}

    def test_a_page_with_no_headings_still_yields_a_usable_key(self, page):
        html = """
        <body><main><div>
          <div><span>Sep 03</span><a href="/e/one">Event One Here</a></div>
          <div><span>Sep 04</span><a href="/e/two">Event Two Here</a></div>
        </div></main></body>
        """
        links = extract(page, html)
        assert links
        assert all(region_key(lnk) for lnk in links), "fell through to no key at all"

    def test_region_is_json_serializable(self, page):
        """It has to survive a trip through JSONL to be worth recording."""
        links = extract(page, CAROUSEL_AND_RESULTS)
        json.dumps(links)
