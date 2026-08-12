"""Tests for `utils.parse_llm_ini` — the LLM reply parser.

**This function had no tests, which is how it silently discarded events for
months.** `ConfigParser` treats `%` as substitution syntax, so any reply
mentioning a `%` — a URL with `%20`, a title saying "50% off" — raised
`InterpolationSyntaxError` and lost the whole batch of ten. Found 2026-08-12 on
`erobay`, which fetched 121 messages every run and produced zero events because
its Calcium detail URLs are full of `%`.

The subtlety worth keeping: interpolation fires on **value access**, not on
parse, so the exception came from the dict comprehension *after* the `try`,
sailing past the `except configparser.Error` written to catch exactly this.
"""

from __future__ import annotations

import pytest

from event_harvester.utils import parse_llm_ini


def _ini(**fields) -> str:
    body = "\n".join(f"{k} = {v}" for k, v in fields.items())
    return f"[Event.1]\n{body}\n"


class TestPercentIsOrdinaryText:
    """The regression. Every case here lost ten events before 2026-08-12."""

    @pytest.mark.parametrize("value", [
        "https://erobay.com/cal?Op=Show%20It&Name=Community",
        "50% off before 9pm",
        "100%",
        "%",
        "a %(name)s literal",
        "%%",
    ])
    def test_a_percent_anywhere_survives(self, value):
        got = parse_llm_ini(_ini(title="T", link=value))
        assert got["Event.1"]["link"] == value

    def test_the_whole_batch_is_not_lost(self):
        """The failure mode was total: one bad value, ten events gone."""
        raw = (
            "[Event.1]\ntitle = Fine\nlink = https://x/a\n\n"
            "[Event.2]\ntitle = Has %20 in it\nlink = https://x/b%20c\n\n"
            "[Event.3]\ntitle = Also fine\n"
        )
        got = parse_llm_ini(raw)
        assert sorted(got) == ["Event.1", "Event.2", "Event.3"]

    def test_interpolation_is_not_performed(self):
        """`%(title)s` must stay literal, not resolve against a sibling key."""
        got = parse_llm_ini(_ini(title="Real", details="see %(title)s"))
        assert got["Event.1"]["details"] == "see %(title)s"


class TestOrdinaryParsing:
    def test_sections_and_keys(self):
        got = parse_llm_ini("[Event.1]\ntitle = Hack Night\ndate = 2026-09-01\n")
        assert got == {"Event.1": {"title": "Hack Night", "date": "2026-09-01"}}

    def test_multiple_sections(self):
        raw = "[Event.1]\ntitle = A\n\n[Event.2]\ntitle = B\n"
        assert [v["title"] for v in parse_llm_ini(raw).values()] == ["A", "B"]

    def test_keys_are_lowercased_as_configparser_does(self):
        assert "title" in parse_llm_ini("[Event.1]\nTitle = A\n")["Event.1"]

    def test_a_colon_in_a_value_is_kept(self):
        got = parse_llm_ini(_ini(title="Doors: 7:30pm"))
        assert got["Event.1"]["title"] == "Doors: 7:30pm"

    def test_a_url_with_a_query_string_is_kept_whole(self):
        url = "https://lu.ma/x?utm=1&ref=2"
        assert parse_llm_ini(_ini(link=url))["Event.1"]["link"] == url


class TestLlmQuirks:
    def test_markdown_fences_are_stripped(self):
        raw = "```ini\n[Event.1]\ntitle = A\n```"
        assert parse_llm_ini(raw)["Event.1"]["title"] == "A"

    def test_a_bare_fence_is_stripped(self):
        assert parse_llm_ini("```\n[Event.1]\ntitle = A\n```")["Event.1"]["title"] == "A"

    def test_leading_whitespace(self):
        assert parse_llm_ini("\n\n  [Event.1]\ntitle = A\n")["Event.1"]["title"] == "A"


class TestMalformedInputIsEmptyNotAnException:
    @pytest.mark.parametrize("raw", [
        "",
        "   ",
        "not ini at all",
        "[unclosed\ntitle = A",
        "title = A",           # a value with no section
        "[Event.1]\n[Event.1]\ntitle = A",   # duplicate section
    ])
    def test_it_returns_a_dict(self, raw):
        assert isinstance(parse_llm_ini(raw), dict)

    def test_a_duplicate_key_does_not_raise(self):
        assert isinstance(parse_llm_ini("[Event.1]\ntitle = A\ntitle = B"), dict)
