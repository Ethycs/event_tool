"""The site is reached at `central-dev.zt/harvester`, not only at `:8800/`.

Every root-relative URL in a served page is a bug under that mount: `/calendar`
resolves against the *proxy's* root, so it lands on `central-dev.zt/calendar` —
a different application, or a 404. Reported 2026-08-12 with `/admin` entirely
non-functional behind the proxy, because all five of its `fetch()` calls were
root-relative and every one of them missed.

Two independent mechanisms are pinned here, and they cover different proxies:

* **Relative URLs** (`outputs/nav.py`) — the pages find each other under any
  mount depth with no configuration. This is the one that matters, and it works
  when the proxy strips the prefix before forwarding.
* **`root_path`** (`servers/site.py`) — additionally mounts every route under
  the prefix, for a proxy that forwards `/harvester/calendar` unchanged.

`TestNoAbsolutePaths` is the regression that keeps a future edit from
reintroducing a `/`-prefixed URL, which would break only behind the proxy and
would look fine in every local check.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from starlette.testclient import TestClient

from event_harvester.outputs.calendar_html import generate_calendar_html
from event_harvester.outputs.html_report import generate_html_report
from event_harvester.outputs.nav import base_script, nav_html
from event_harvester.servers.admin import render_page
from event_harvester.servers.site import normalise_prefix

PREFIX = "/harvester"


def _events():
    return [{"title": "Queer Night", "date": date.today().isoformat(),
             "time": "9pm", "location": "The Stud", "source": "web",
             "link": "https://example.test/e/1"}]


@pytest.fixture
def served(tmp_path):
    """The three pages exactly as the site emits them."""
    index = tmp_path / "index.html"
    cal = tmp_path / "calendar.html"
    generate_html_report(_events(), output_path=str(index), nav=True)
    generate_calendar_html(_events(), output_path=str(cal), nav=True)
    return {
        "index": index.read_text(encoding="utf-8"),
        "calendar": cal.read_text(encoding="utf-8"),
        "admin": render_page(),
    }


# Same-origin URLs only: an absolute `https://…` link to a venue is fine, and a
# `data:` URI has no path at all.
_ABSOLUTE = re.compile(r"""(?:href|src)\s*=\s*["']/(?!/)|fetch\(\s*["']/(?!/)""")


class TestNoAbsolutePaths:
    """The rule, stated once per page."""

    @pytest.mark.parametrize("page", ["index", "calendar", "admin"])
    def test_no_root_relative_urls(self, served, page):
        found = _ABSOLUTE.findall(served[page])
        assert not found, (
            f"{page} emits a root-relative URL; it will miss the mount "
            f"under {PREFIX}: {found}")

    @pytest.mark.parametrize("page", ["index", "calendar", "admin"])
    def test_the_nav_links_are_relative(self, served, page):
        assert 'href="calendar"' in served[page]
        assert 'href="admin"' in served[page]
        assert 'href="."' in served[page]

    def test_the_admin_api_is_reached_relatively(self, served):
        """Five fetches, all of which missed behind the proxy."""
        assert served["admin"].count('fetch("admin/api/') >= 4
        assert 'fetch("/admin/api/' not in served["admin"]

    def test_the_list_page_polls_relatively(self, served):
        assert "fetch('events.json'" in served["index"]
        assert "fetch('healthz'" in served["index"]


class TestBaseScript:
    """Relative URLs have exactly one failure case: no trailing slash."""

    def test_each_page_strips_its_own_name(self):
        assert "calendar(?:\\.html)?" in base_script("calendar")
        assert "admin" in base_script("admin")
        assert "index\\.html" in base_script("index")

    def test_it_ensures_a_trailing_slash(self):
        """`/harvester` alone would take `/` as the base."""
        assert "!== '/') p += '/'" in base_script("index")

    def test_it_is_absent_from_a_forwarded_file(self, tmp_path):
        path = tmp_path / "f.html"
        generate_calendar_html(_events(), output_path=str(path))
        assert "<base" not in path.read_text(encoding="utf-8")
        assert base_script("") == ""

    def test_it_runs_before_the_body(self, served):
        """A <base> inserted after parsing would not apply to the markup."""
        for page in ("index", "calendar", "admin"):
            body = served[page]
            assert body.index("createElement('base')") < body.index("<body")


class TestNormalisePrefix:
    @pytest.mark.parametrize("given", ["harvester", "/harvester", "/harvester/",
                                       "//harvester//", "  /harvester  "])
    def test_every_spelling_lands_on_one_form(self, given):
        assert normalise_prefix(given) == PREFIX

    @pytest.mark.parametrize("given", ["", "/", "   ", None])
    def test_nothing_stays_nothing(self, given):
        assert normalise_prefix(given) == ""


class TestPrefixedRoutes:
    """For a proxy that forwards the prefix instead of stripping it."""

    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        from event_harvester.config import load_config
        from event_harvester.servers import site as site_mod

        monkeypatch.setattr(site_mod, "SITE_HTML", tmp_path / "index.html")
        monkeypatch.setattr(site_mod, "SITE_CALENDAR", tmp_path / "calendar.html")
        generate_html_report(_events(), output_path=str(tmp_path / "index.html"),
                             nav=True)
        generate_calendar_html(_events(), output_path=str(tmp_path / "calendar.html"),
                               nav=True)
        return site_mod.build_app(load_config(), harvest_on_start=False,
                                  schedule=False, root_path=PREFIX)

    @pytest.mark.parametrize("path", ["/", "/calendar", "/admin", "/healthz"])
    def test_both_the_prefixed_and_the_bare_path_answer(self, app, path):
        with TestClient(app) as c:
            assert c.get(path).status_code == 200, "direct access broke"
            assert c.get(PREFIX + path).status_code == 200, "the mount broke"

    def test_the_prefix_without_a_trailing_slash_redirects(self, app):
        """The one case relative URLs cannot resolve, handled by Mount."""
        with TestClient(app) as c:
            r = c.get(PREFIX, follow_redirects=False)
            assert r.status_code in (301, 307)
            assert r.headers["location"].endswith(PREFIX + "/")

    def test_the_admin_api_answers_under_the_prefix(self, app):
        with TestClient(app) as c:
            assert c.get(f"{PREFIX}/admin/api/state").status_code == 200

    def test_no_prefix_means_no_extra_routes(self, tmp_path, monkeypatch):
        from event_harvester.config import load_config
        from event_harvester.servers import site as site_mod

        monkeypatch.setattr(site_mod, "SITE_HTML", tmp_path / "index.html")
        app = site_mod.build_app(load_config(), harvest_on_start=False,
                                 schedule=False)
        with TestClient(app) as c:
            assert c.get(f"{PREFIX}/calendar").status_code == 404


class TestNavIsKeyedByPageNotPath:
    """The active marker moved from a path to an id when the paths went
    relative; a stale `/calendar` would silently mark nothing."""

    def test_the_current_page_is_marked_once(self):
        html = nav_html("calendar")
        assert html.count("aria-current") == 1
        assert 'href="calendar" title="The same events on a month grid" '\
               'aria-current="page"' in html

    def test_an_unknown_id_marks_nothing(self):
        assert "aria-current" not in nav_html("/calendar")
