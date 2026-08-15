"""Dismissing an event: the rules behind the red ×.

Driven in headless Chromium on 2026-08-15 against the 244-event build, at
1440px and 390px:

| | |
|---|---|
| button on top of a card **with a thumbnail** | yes, `elementFromPoint` returns it |
| size, desktop / phone | 28×28 / **44×44** |
| dismiss → count | `244 of 244` → `243 of 243 · 1 dismissed` |
| survives a reload | yes |
| Undo, and restore-all via the count | both return the card |

None of that is reachable from pytest — there is no layout engine here — so
these pin the *rules that produced it*, in the manner of `test_mobile.py`.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from event_harvester.outputs.html_report import _SCRIPT, _STYLE, generate_html_report


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    d = tmp_path_factory.mktemp("dismiss")
    generate_html_report(
        [{"title": "Queer Night", "date": date.today().isoformat(), "time": "9pm",
          "location": "The Stud", "source": "web", "link": "https://example.test/1"}],
        output_path=str(d / "i.html"),
    )
    return (d / "i.html").read_text(encoding="utf-8")


def _live_css() -> str:
    return re.sub(r"/\*.*?\*/", "", _STYLE, flags=re.S)


class TestItFloatsOverEverything:
    def test_the_card_is_a_positioning_context(self):
        """Without this the button is positioned against the page, not the
        card, and lands in the top-right of the viewport."""
        assert re.search(r"\.card\s*\{[^}]*position:\s*relative", _live_css(), re.S)

    def test_it_is_absolutely_positioned_top_right(self):
        rule = re.search(r"\.dismiss\s*\{([^}]*)\}", _live_css(), re.S).group(1)
        assert "position: absolute" in rule
        assert "top:" in rule and "right:" in rule

    def test_it_sits_above_the_thumbnail(self):
        """`.thumb` bleeds past the card's padding on a negative margin to meet
        the rounded corners, so without a z-index the button is *under* the
        picture on exactly the cards that have one."""
        rule = re.search(r"\.dismiss\s*\{([^}]*)\}", _live_css(), re.S).group(1)
        z = re.search(r"z-index:\s*(\d+)", rule)
        assert z and int(z.group(1)) >= 2

    def test_a_thumb_cannot_outrank_it(self):
        thumb = re.search(r"\.thumb\s*\{([^}]*)\}", _live_css(), re.S).group(1)
        assert "z-index" not in thumb


class TestTouchTarget:
    def test_a_thumb_gets_a_thumb_sized_target(self):
        """28px is a mouse target. On a phone it is both hard to hit and easy
        to hit by accident while scrolling — and this one removes a card."""
        block = re.search(r"@media \(pointer: coarse\)\s*\{(.*?)\n\}",
                          _live_css(), re.S).group(1)
        rule = re.search(r"\.dismiss\s*\{([^}]*)\}", block, re.S)
        assert rule and "44px" in rule.group(1)

    def test_the_coarse_block_is_still_last(self):
        """Same specificity as the base `.dismiss` rule, so a block placed
        above it loses on source order — silently, and only on a touch device.
        This stylesheet has had that bug twice."""
        css = _live_css()
        assert css.index("@media (pointer: coarse)") > css.index(".dismiss {")


class TestItActuallyDismisses:
    def test_the_card_builder_adds_a_button(self, page):
        assert "class = 'dismiss'" in _SCRIPT or "className = 'dismiss'" in _SCRIPT
        assert "dismiss" in page

    def test_dismissed_events_are_filtered_out(self):
        matching = re.search(r"function matching\(\)\s*\{(.*?)\n\}", _SCRIPT, re.S).group(1)
        assert "dismissed.has(keyOf(ev))" in matching

    def test_it_is_a_real_button_with_a_label(self):
        """A bare × is unreadable to a screen reader, and the page is one
        someone might be reading aloud."""
        assert "aria-label" in _SCRIPT
        assert "x.type = 'button'" in _SCRIPT

    def test_the_click_does_not_bubble(self):
        """The card carries its own click behaviour; dismissing must not also
        trigger it."""
        assert "e.stopPropagation()" in _SCRIPT


class TestPersistenceIsHonestAboutItsScope:
    def test_state_lives_in_localstorage_not_on_a_server(self):
        """Forced, not chosen: this file is meant to be forwarded, and a
        forwarded copy has no origin to POST to."""
        assert "localStorage.setItem(DISMISS_KEY" in _SCRIPT
        assert "fetch('decline" not in _SCRIPT and "fetch('admin" not in _SCRIPT

    def test_storage_access_is_guarded(self):
        """`localStorage` throws outright under `file://` in some browsers and
        in private mode — and this page's whole point is being openable as a
        file."""
        block = re.search(
            r"try \{\s*dismissed = new Set\(JSON\.parse.*?\} catch", _SCRIPT, re.S)
        assert block

    def test_the_key_is_content_not_position(self):
        """The poll swaps `DATA` wholesale on the next harvest, so an index
        would dismiss a stranger."""
        keyof = re.search(r"function keyOf\(ev\)\s*\{(.*?)\n\}", _SCRIPT, re.S).group(1)
        assert "ev.title" in keyof and "ev.date" in keyof
        assert "index" not in keyof.lower()


class TestThereIsAWayBack:
    def test_the_toast_offers_undo(self):
        assert "label: 'Undo'" in _SCRIPT

    def test_an_actionable_toast_can_be_clicked(self):
        """The base rule sets `pointer-events: none` so a toast never eats a
        click. One carrying an Undo has to take them back, or the button is
        decorative."""
        assert re.search(r"#toast\.actionable\s*\{[^}]*pointer-events:\s*auto",
                         _live_css(), re.S)

    def test_the_count_reports_and_restores(self):
        """The toast's Undo is gone in seconds. An event that vanished with no
        trace and no way back is indistinguishable from a bug."""
        render = re.search(r"function render\(\)\s*\{(.*?)\n\}", _SCRIPT, re.S).group(1)
        assert "dismissed'" in render
        assert "restoreAll" in render
        assert "function restoreAll()" in _SCRIPT

    def test_one_toast_timer_not_many(self):
        """Dismissing three cards in a row left three timers racing, and the
        first to fire hid a toast whose Undo still had seconds left."""
        assert "clearTimeout(toastTimer)" in _SCRIPT


class TestItDoesNotBreakTheFileItLivesIn:
    def test_the_page_still_has_no_external_resources(self, page):
        assert "http://" not in page.replace("http://www.w3.org", "")

    def test_the_toast_never_uses_innerhtml(self):
        # Comments stripped first — the rule is stated in one of them, and a
        # test that reads comments is testing prose.
        live = re.sub(r"//[^\n]*", "", _SCRIPT)
        toast = re.search(r"function toast\(message, action\)\s*\{(.*?)\n\}",
                          live, re.S).group(1)
        assert "innerHTML" not in toast
        assert "textContent" in toast
