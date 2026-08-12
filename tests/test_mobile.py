"""The properties a phone needs, pinned so a later edit cannot quietly undo them.

Measured with headless Chromium at 320x568, 360x740 and 390x844 on 2026-08-12,
before this pass:

| | before | after |
|---|---|---|
| `/admin` document width in a 360px viewport | **456-546px** | 360px |
| list page sticky control block | **303px (53% of an iPhone SE)** | 61px |
| list page: first card begins at | **y=546**, below the fold | y=256 |
| calendar: grid begins at | **y=611** | y=369 |

None of that is reachable from pytest — there is no layout engine here — so
these tests pin the *rules that produced it* rather than the pixels: the
declaration that lets a grid item shrink, the media queries that exist, and
the source order they appear in. Source order is the subtle one and it bit
twice in one afternoon; see `TestSourceOrder`.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from event_harvester.outputs.calendar_html import generate_calendar_html
from event_harvester.outputs.html_report import generate_html_report
from event_harvester.servers.admin import render_page


def _events():
    return [{"title": "Queer Night", "date": date.today().isoformat(), "time": "9pm",
             "location": "The Stud", "source": "web", "link": "https://example.test/1"}]


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    d = tmp_path_factory.mktemp("mobile")
    generate_html_report(_events(), output_path=str(d / "i.html"), nav=True)
    generate_calendar_html(_events(), output_path=str(d / "c.html"), nav=True)
    return {"index": (d / "i.html").read_text(encoding="utf-8"),
            "calendar": (d / "c.html").read_text(encoding="utf-8"),
            "admin": render_page()}


def _style(page: str) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))


class TestAdminFitsAPhone:
    """`/admin` scrolled sideways on every phone: 456px inside a 360px screen."""

    def test_sections_may_shrink_below_their_content(self, pages):
        """A grid item's automatic minimum is its *min-content* width, so a
        section holding a 33-row table refuses to shrink — and `overflow-x` on
        the table changes nothing, because the section was never the scroller.
        This one declaration is the whole fix."""
        assert re.search(r"section\s*\{[^}]*min-width\s*:\s*0", _style(pages["admin"]), re.S)

    def test_the_table_scrolls_inside_its_own_box(self, pages):
        assert '<div class="tablewrap">' in pages["admin"]
        assert re.search(r"\.tablewrap\s*\{[^}]*overflow-x\s*:\s*auto", _style(pages["admin"]))

    def test_the_cap_row_stacks_rather_than_overflowing(self, pages):
        """Label above, controls below. At `110px 1fr 84px 44px` the number
        field and the ∞ toggle — the two controls that set a cap — sat off the
        right edge with nothing saying they existed."""
        css = _style(pages["admin"])
        assert "grid-template-areas" in css
        assert '"lab lab lab"' in css

    def test_long_tokens_can_break(self, pages):
        assert "overflow-wrap:anywhere" in _style(pages["admin"]).replace(" ", "")


class TestTheStickyBlockIsSmall:
    """303px of a 568px screen, permanently, is not a control bar."""

    def test_the_chips_collapse_behind_a_button(self, pages):
        for name in ("index", "calendar"):
            assert 'id="filter-toggle"' in pages[name], name

    def test_the_button_names_the_active_filter(self, pages):
        """"Oakland · web", not "Filters (2)" — which filter is on is the part
        you cannot otherwise see."""
        for name in ("index", "calendar"):
            assert "activeChipLabel" in pages[name], name
            assert "parts.join(' · ')" in pages[name], name

    def test_opening_them_is_a_body_class(self, pages):
        for name in ("index", "calendar"):
            assert "filters-open" in pages[name], name

    def test_the_source_tallies_do_not_push_events_off_the_screen(self, pages):
        assert "header .meta + .meta { display: none; }" in _style(pages["index"])


class TestSourceOrder:
    """Same-specificity rules are decided by position, and both times this got
    it wrong the failure was invisible except on a touch device."""

    @pytest.mark.parametrize("name", ["index", "calendar"])
    def test_the_coarse_pointer_block_is_last(self, pages, name):
        css = _style(pages[name])
        after = css[css.rindex("@media (pointer: coarse)"):]
        # Nothing but that block's own rules may follow it.
        assert after.count("{") == after.count("}")
        assert "@media" not in after[len("@media (pointer: coarse)"):]

    def test_admin_coarse_block_is_last(self):
        css = _style(render_page())
        tail = css[css.rindex("@media(pointer:coarse)"):]
        assert ".sitenav" not in tail.split("}", 1)[0]

    def test_the_filter_button_is_hidden_before_it_is_revealed(self, pages):
        """`#filter-toggle{display:none}` after the media query that shows it
        wins on source order — and hid the button on exactly the screens that
        need it. It shipped that way for one build."""
        for name in ("index", "calendar"):
            css = _style(pages[name])
            hide = css.index("#filter-toggle { display: none; }")
            show = css.index("#filter-toggle { display: inline-flex; }")
            assert hide < show, f"{name}: the button is hidden on phones"


class TestTapTargets:
    def test_every_page_asks_for_44px_on_touch(self, pages):
        for name in ("index", "calendar", "admin"):
            assert "pointer: coarse" in pages[name] or "pointer:coarse" in pages[name]
            assert "44px" in _style(pages[name]), name

    def test_the_nav_sizing_travels_with_the_nav(self, tmp_path):
        """In the page stylesheets it would ship to forwarded files, which have
        no nav at all."""
        from event_harvester.outputs.nav import NAV_STYLE

        assert "pointer: coarse" in NAV_STYLE and "min-height: 44px" in NAV_STYLE
        path = tmp_path / "f.html"
        generate_calendar_html(_events(), output_path=str(path))
        assert "sitenav" not in path.read_text(encoding="utf-8")

    def test_the_admin_slider_is_grabbable(self):
        """It was 16px tall — a slider a thumb cannot reliably catch."""
        assert "input[type=range]{height:44px}" in _style(render_page())


class TestHiddenMeansHidden:
    def test_a_hidden_button_does_not_render(self, pages):
        """`display: inline-block` beats the user-agent `[hidden]` rule, so the
        calendar's Export button rendered as a dead label whenever the month
        held nothing to export."""
        css = _style(pages["calendar"])
        assert re.search(r"\[hidden\]\s*(,[^{]*)?\{\s*display:\s*none", css)
        assert re.search(r"a\.btn\[hidden\]", _style(pages["index"]))
