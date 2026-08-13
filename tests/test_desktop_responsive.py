"""The catalog has to keep resizing past 1400px, and the type has to stop.

Measured with headless Chromium against the 244-event build on 2026-08-13,
before this pass:

| viewport | columns | `main` width | empty gutter | h1 at x | first card at x |
|---|---|---|---|---|---|
| 1280 | 4 | 1280 | 0 | 16 | 16 |
| 1920 | **4** | 1400 | 520 | 16 | **276** |
| 2560 | **4** | 1400 | 1160 | 16 | **596** |
| 3440 | **4** | 1400 | 2040 | 16 | **1036** |

Two faults from one declaration. `main { max-width: 1400px }` froze the grid at
four columns however wide the window got — the thing that reads as "not
responsive" — and because only `main` was centred while `header` and
`.controls` stayed full-bleed, the search field ran to x=3270 while the cards it
filters began at x=1036. A 1020px skew between the furniture and its content.

After: 3 / 4 / 5 / 6 / 7 / 9 columns at 1024 / 1280 / 1920 / 2240 / 2560 / 3440,
tracks between 305 and 385px at every step, gutter 0, skew 0.

As in `test_mobile.py`, none of that is reachable from pytest — there is no
layout engine here — so these pin the *rules that produced it*: that no cap
exists, that the track and the type are expressed as clamps, and that each size
is declared exactly once. That last one is the same lesson as
`test_mobile.TestSourceOrder` from the other end: a value that appears twice can
drift, and a value that appears once cannot.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from event_harvester.outputs import nav
from event_harvester.outputs.html_report import _STYLE, generate_html_report

#: Every fluid step, and the floor each one must never go below. The floors are
#: the sizes `test_mobile.py` measured at 320-375px; a phone must not lose type
#: to a change aimed at a desktop.
STEPS = {"--fs-xs": 11, "--fs-sm": 13, "--fs-md": 14, "--fs-base": 16, "--fs-lg": 18}


def _decl(name: str) -> str:
    """The value of a custom property as declared in the stylesheet."""
    m = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", _STYLE)
    assert m, f"{name} is not declared"
    return m.group(1).strip()


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    d = tmp_path_factory.mktemp("desktop")
    generate_html_report(
        [{"title": "Queer Night", "date": date.today().isoformat(), "time": "9pm",
          "location": "The Stud", "source": "web", "link": "https://example.test/1"}],
        output_path=str(d / "i.html"), nav=True,
    )
    return (d / "i.html").read_text(encoding="utf-8")


class TestTheGridKeepsResizing:
    def test_main_has_no_width_cap(self):
        """`max-width: 1400px` is the whole bug. At 3440px it left 2040px of
        empty gutter and four columns."""
        main = re.search(r"\bmain\s*\{([^}]*)\}", _STYLE)
        assert main, "no `main` rule"
        assert "max-width" not in main.group(1)

    def test_main_is_not_centred_against_a_different_measure(self):
        """`margin: 0 auto` is what centred `main` while the bands above it
        stayed full-bleed, and that misalignment is the second half of the
        defect. With no cap there is nothing left to centre."""
        main = re.search(r"\bmain\s*\{([^}]*)\}", _STYLE)
        assert "margin" not in main.group(1)

    def test_every_band_shares_one_inset(self):
        """header, .controls and main must agree, or they cannot line up."""
        insets = {}
        for sel in ("header", r"\.controls", "main"):
            body = re.search(rf"^{sel}\s*\{{([^}}]*)\}}", _STYLE, re.M)
            pad = re.search(r"padding\s*:\s*([^;]+)", body.group(1))
            insets[sel] = pad.group(1).split()[1]
        assert len(set(insets.values())) == 1, insets

    def test_the_track_is_fluid_not_fixed(self):
        """`minmax(270px, 1fr)` gains columns but never widens a card, so on a
        wide screen you get more slivers rather than a better layout."""
        shelf = re.search(r"\.shelf\s*\{([^}]*)\}", _STYLE).group(1)
        assert "repeat(auto-fill, minmax(var(--card-min), 1fr))" in shelf
        assert "clamp(" in _decl("--card-min")

    def test_the_track_floor_still_fits_a_narrow_phone(self):
        """A 320px screen has ~292px of content box. A floor above that would
        push the single column into a horizontal scroll."""
        floor = int(re.search(r"clamp\(\s*(\d+)px", _decl("--card-min")).group(1))
        assert floor <= 292

    def test_the_track_stops_growing(self):
        """Unbounded, one card would be half an ultrawide."""
        assert re.search(r",\s*(\d+)px\s*\)$", _decl("--card-min"))

    def test_the_carousel_uses_the_same_card_width(self):
        """Two layouts disagreeing about what a card is was how `300px` and
        `270px` came to sit in the same stylesheet meaning the same thing."""
        rule = re.search(r"body\.carousel \.shelf \.card\s*\{([^}]*)\}", _STYLE).group(1)
        assert "var(--card-min)" in rule


class TestTheTypeScaleIsFluidAndSaturates:
    @pytest.mark.parametrize("step", STEPS)
    def test_each_step_is_a_clamp_that_moves_with_the_viewport(self, step):
        value = _decl(step)
        assert value.startswith("clamp("), value
        assert "vw" in value, f"{step} does not respond to width: {value}"

    @pytest.mark.parametrize("step,floor", STEPS.items())
    def test_each_step_floors_at_the_size_the_phone_pass_measured(self, step, floor):
        assert int(re.search(r"clamp\(\s*(\d+)px", _decl(step)).group(1)) == floor

    @pytest.mark.parametrize("step", STEPS)
    def test_each_step_has_a_ceiling(self, step):
        """"Readable but not overwhelming" is the ceiling. A 3440px monitor is
        further from your eyes than a phone, not larger in your field of view,
        so type that keeps growing with the viewport is the failure mode — the
        upper bound of every clamp is a fixed px, never a vw."""
        top = re.search(r",\s*([^,]+)\)\s*$", _decl(step)).group(1).strip()
        assert re.fullmatch(r"\d+px", top), f"{step} never stops growing: {top}"

    def test_the_base_step_cannot_trigger_ios_zoom(self):
        """Safari zooms the page when a focused input computes under 16px, and
        the search field is on this token."""
        assert STEPS["--fs-base"] == 16
        search = re.search(r"input\[type=search\]\s*\{([^}]*)\}", _STYLE).group(1)
        assert "var(--fs-base)" in search

    def test_no_size_is_declared_outside_the_scale(self):
        """One source of truth per step. `h1 { font-size: 18px }` in the phone
        media query restated `--fs-lg`'s floor in a second place that could
        drift from it; both are now the same declaration."""
        live = re.sub(r"/\*.*?\*/", "", _STYLE, flags=re.S)
        assert not re.findall(r"font-size\s*:\s*\d", live)
        assert not re.findall(r"font\s*:\s*\d", live)

    def test_the_phone_media_query_no_longer_restates_the_heading(self):
        # Comments stripped first: the removed rule is quoted in one, and a
        # test that reads comments is testing prose.
        live = re.sub(r"/\*.*?\*/", "", _STYLE, flags=re.S)
        block = re.search(r"@media \(max-width: 700px\)\s*\{(.*?)\n\}", live, re.S).group(1)
        assert not re.search(r"h1\s*\{[^}]*font-size", block)


class TestTheNavTravelsBetweenPagesThatDisagree:
    def test_it_scales_where_the_scale_exists(self, page):
        assert "var(--fs-md, 14px)" in nav.NAV_STYLE
        assert "var(--fs-md, 14px)" in page

    def test_it_falls_back_where_it_does_not(self):
        """Only the event list defines the tokens. Without the fallback the
        declaration is invalid on `/calendar` and `/admin`, and the tabs
        silently inherit the body size instead of keeping their 14px."""
        m = re.search(r"font-size:\s*var\(--fs-md(?:,\s*(\d+)px)?\)", nav.NAV_STYLE)
        assert m and m.group(1) == "14"
