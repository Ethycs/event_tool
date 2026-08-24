"""What "today" means on a page that outlives the day it was built.

The site is rendered once per refresh and then **served unchanged for
`interval_hours`** — 24 by default. Every build-time answer to "what day is
it" is therefore wrong for most of the window the page is up. Observed
2026-08-19: `data/.site/calendar.html`, written 16:46 the previous afternoon,
still carried `data-today="2026-08-18"`, so the events page filed the 19th
under "Tomorrow" and the calendar outlined the wrong cell all day.

The fix moves the arithmetic to the side that knows the answer — the browser —
and these pin the rules that make that work. There is no JS engine in this
test run, so, in the manner of `test_dismiss.py`, they assert against the
source of `_SCRIPT` rather than against a rendered page. The parity test is
the load-bearing one: it *executes* the thresholds lifted out of the JS and
compares them, day by day, with the Python the JSON is still written from.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from event_harvester.outputs.calendar_html import _SCRIPT as CAL_SCRIPT
from event_harvester.outputs.calendar_html import generate_calendar_html
from event_harvester.outputs.html_report import _BUCKET_ORDER, _bucket, generate_html_report
from event_harvester.outputs.html_report import _SCRIPT as PAGE_SCRIPT


def _code(script: str) -> str:
    """The script with its `//` comments removed.

    "This construct does not appear" is only a claim about code — both files
    explain in prose *why* they avoid `toISOString`, and a naive substring
    search reads that explanation as a violation. `(?<!:)` keeps the `//` of a
    `https://` URL out of it.
    """
    return re.sub(r"(?<!:)//.*", "", script)


def _js_bucket_rules() -> list[tuple[str, int, str]]:
    """The `if (delta …) return '…'` ladder out of `bucketOf`, in order."""
    body = re.search(r"function bucketOf\(ev\)\s*\{(.*?)\n\}", PAGE_SCRIPT, re.S)
    assert body, "bucketOf is gone — the page is back to trusting ev.bucket"
    return [
        (op, int(n), name)
        for op, n, name in re.findall(
            r"if \(delta (<|<=|===) (-?\d+)\) return '([^']+)';", body.group(1)
        )
    ]


def _js_bucket(delta: int) -> str:
    for op, n, name in _js_bucket_rules():
        if (op == "<" and delta < n) or (op == "<=" and delta <= n) or \
           (op == "===" and delta == n):
            return name
    return re.search(r"return '(\w[^']*)';\n\}", PAGE_SCRIPT).group(1)


class TestTheTwoImplementationsAgree:
    """`_bucket` still labels `events.json`; `bucketOf` labels the page. Two
    copies of one rule is a drift risk, and the drift would be silent — the
    page would simply group things wrongly, with no error anywhere."""

    def test_every_day_from_a_week_ago_to_a_year_out(self):
        today = date(2026, 8, 19)
        for delta in range(-7, 366):
            iso = (today + timedelta(days=delta)).isoformat()
            assert _js_bucket(delta) == _bucket(iso, today), f"delta={delta}"

    def test_the_ladder_is_actually_read(self):
        """A regex that matched nothing would make the test above vacuous —
        every delta would fall through to the same trailing return."""
        assert len(_js_bucket_rules()) >= 5

    def test_it_names_only_buckets_the_page_can_render(self):
        """`render()` walks `BUCKETS` and asks for events matching each name.
        A bucket the JS invents by a different spelling renders nothing."""
        for _, _, name in _js_bucket_rules():
            assert name in _BUCKET_ORDER
        assert "Undated" in PAGE_SCRIPT


class TestThePageAsksTheBrowser:
    def test_render_buckets_by_computation_not_by_the_baked_field(self):
        assert "bucketOf(ev) === bucket" in PAGE_SCRIPT
        assert "ev.bucket" not in _code(PAGE_SCRIPT)

    def test_today_is_the_browsers_local_midnight(self):
        """Not `toISOString()`, and not a UTC `Date`. Event dates are local
        wall-clock dates, so a 9pm show tonight is `Past` from 5pm PDT if the
        comparison happens in UTC — the same bug the Python side already
        carries a docstring about."""
        assert "new Date(n.getFullYear(), n.getMonth(), n.getDate())" in PAGE_SCRIPT
        assert "toISOString" not in _code(PAGE_SCRIPT)

    def test_a_date_that_does_not_exist_is_undated(self):
        """`new Date(2026, 1, 31)` is 3 March, not an error. Without the
        round-trip check the JS would file nonsense under "This week" while
        Python calls it "Undated"."""
        body = re.search(r"function bucketOf\(ev\)\s*\{(.*?)\n\}", PAGE_SCRIPT, re.S)
        assert "when.getMonth() !== mo" in body.group(1)

    def test_the_json_still_carries_a_bucket_for_other_readers(self, tmp_path):
        """The page ignores it; `events.json` is read elsewhere, so dropping
        the field would be a different breakage rather than a cleanup."""
        out = tmp_path / "i.html"
        generate_html_report(
            [{"title": "T", "date": date.today().isoformat(), "source": "web"}],
            output_path=str(out),
        )
        assert '"bucket": "Today"' in out.read_text(encoding="utf-8")


class TestTheCalendarAsksTheBrowser:
    def test_today_is_not_pinned_to_the_build_date(self):
        assert "let TODAY = localToday()" in CAL_SCRIPT

    def test_the_build_date_survives_as_the_fallback(self, tmp_path):
        out = tmp_path / "c.html"
        generate_calendar_html([], output_path=str(out), today=date(2026, 8, 18))
        assert 'data-today="2026-08-18"' in out.read_text(encoding="utf-8")
        assert "document.body.dataset.today" in CAL_SCRIPT

    def test_it_names_the_day_without_going_through_utc(self):
        assert "keyOf(n.getFullYear(), n.getMonth(), n.getDate())" in CAL_SCRIPT
        assert "toISOString" not in _code(CAL_SCRIPT)

    def test_keyof_is_defined_before_it_is_called(self):
        """`localToday()` runs at module scope, and `keyOf` is a `const` arrow —
        defined below the call site it would throw a TDZ error and leave the
        page blank."""
        assert CAL_SCRIPT.index("const keyOf =") < CAL_SCRIPT.index("let TODAY =")


@pytest.mark.parametrize("script", [PAGE_SCRIPT, CAL_SCRIPT])
class TestATabLeftOpenOvernight:
    """The common case on a phone: the page someone checks at 9am was loaded
    the evening before. Recomputing on load only is not enough."""

    def test_it_re_checks_on_a_timer(self, script):
        assert re.search(r"setInterval\(checkRollover, \d+\)", script)

    def test_it_re_checks_when_the_tab_comes_back(self, script):
        block = re.search(
            r"visibilitychange', \(\) => \{\s*if \(!document\.hidden\) (\w+)\(\)",
            script,
        )
        assert block and block.group(1) in ("checkRollover", "poll")
        assert "checkRollover()" in script

    def test_it_only_re_renders_when_the_day_actually_turned(self, script):
        """`render()` rebuilds every card — the events page across animation
        frames, the calendar 42 cells at once. Doing that once a minute for
        nothing would fight with a click or a half-typed search."""
        body = re.search(r"function checkRollover\(\) \{(.*?)\n\}", script, re.S)
        assert body and "return;" in body.group(1)
        assert body.group(1).count("render()") == 1
