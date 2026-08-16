"""An enabled source that harvests nothing must say so.

A dead source and a quiet one are identical in the totals line, and that is not
academic: Discord's desktop cache stopped holding message JSON, the reader
returned `[]` every run for weeks, and every harvest reported `ok` with the
same shrug it gives a genuinely quiet night. This is the 429-storm lesson one
stage earlier — 0 that means "never reached" wearing the face of 0 that means
"nothing to find".

The trigger is 0 *messages*, not 0 events. A source that hands over raw
messages the model then finds no events in is working; this fires a level
before that, at "the source gave us nothing at all".
"""

from __future__ import annotations

from event_harvester.pipeline.orchestrator import _EXPECTED_NONEMPTY, _source_warnings


def _no_kwargs(**enabled: bool) -> dict[str, bool]:
    """`no_kwargs` is keyed the negative way: no_telegram=True means disabled.
    This helper takes the readable positive and inverts it."""
    plats = ("discord", "telegram", "gmail", "signal", "web")
    return {f"no_{p}": not enabled.get(p, False) for p in plats}


class TestItFiresOnASilentSource:
    def test_a_high_volume_source_at_zero_is_flagged_as_wrong(self):
        counts = {"telegram": 0, "discord": 500, "gmail": 3, "signal": 1, "web": 9}
        w = _source_warnings(counts, _no_kwargs(
            discord=True, telegram=True, gmail=True, signal=True, web=True))
        assert any(x.startswith("telegram:") for x in w)
        assert "missing credential" in " ".join(w)

    def test_the_discord_case_specifically(self):
        """The one this exists for: enabled, returns nothing every run."""
        counts = {"discord": 0, "telegram": 800, "gmail": 5, "signal": 0, "web": 40}
        w = _source_warnings(counts, _no_kwargs(
            discord=True, telegram=True, gmail=True, signal=True, web=True))
        assert any("discord: harvested 0 messages" in x for x in w)

    def test_several_silent_sources_each_get_a_line(self):
        counts = {"discord": 0, "telegram": 0, "gmail": 0, "signal": 5, "web": 5}
        w = _source_warnings(counts, _no_kwargs(
            discord=True, telegram=True, gmail=True, signal=True, web=True))
        assert sum(x.startswith(("discord:", "telegram:", "gmail:")) for x in w) == 3


class TestItStaysQuietWhenItShould:
    def test_a_working_source_produces_no_warning(self):
        counts = {"discord": 12, "telegram": 800, "gmail": 5, "signal": 2, "web": 40}
        assert _source_warnings(counts, _no_kwargs(
            discord=True, telegram=True, gmail=True, signal=True, web=True)) == []

    def test_a_disabled_source_at_zero_is_not_flagged(self):
        """`--only web` means discord was never asked; its 0 is expected, and a
        warning on every scoped run is one the reader learns to skip."""
        counts = {"discord": 0, "telegram": 0, "gmail": 0, "signal": 0, "web": 40}
        w = _source_warnings(counts, _no_kwargs(web=True))
        assert w == []

    def test_web_and_signal_zero_is_stated_but_not_called_a_fault(self):
        """These can be legitimately empty — a quiet Signal week, no web hits —
        so their zero is reported without the "missing credential" language
        that would cry wolf."""
        counts = {"discord": 5, "telegram": 5, "gmail": 5, "signal": 0, "web": 0}
        w = _source_warnings(counts, _no_kwargs(
            discord=True, telegram=True, gmail=True, signal=True, web=True))
        joined = " ".join(w)
        assert "signal:" in joined and "web:" in joined
        assert "missing credential" not in joined
        assert "may simply be quiet" in joined


class TestTheDistinctionIsPrincipled:
    def test_only_high_volume_sources_are_treated_as_faults(self):
        """Telegram, Discord, Gmail produce chatter on any real account; their
        silence is a wiring problem. Signal and web are not on that list."""
        assert set(_EXPECTED_NONEMPTY) == {"telegram", "discord", "gmail"}

    def test_a_missing_count_is_treated_as_zero(self):
        """A platform absent from source_counts is the same as zero — never a
        reason to skip the check."""
        w = _source_warnings({}, _no_kwargs(discord=True))
        assert any("discord:" in x for x in w)
