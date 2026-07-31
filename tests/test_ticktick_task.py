"""Tests for the TickTick Open API v1 task object.

Two of these pin bugs that were live in the previous inline construction, and
both were silent: a priority scale where the API has an enum, and every due
date claiming UTC regardless of the zone the event was actually in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from event_harvester.outputs.ticktick_task import (
    API_DATETIME,
    ChecklistItem,
    ItemStatus,
    Priority,
    TaskStatus,
    TickTickTask,
    coerce_priority,
    format_api_datetime,
)


class TestPriorityIsAnEnumNotAScale:
    """The API accepts 0, 1, 3, 5. The pipeline emits 0-5."""

    def test_the_legal_values_pass_through(self):
        for value in (0, 1, 3, 5):
            assert int(coerce_priority(value)) == value

    def test_the_illegal_middles_are_coerced_not_forwarded(self):
        """2 and 4 are produced by the LLM and were previously sent as-is."""
        assert coerce_priority(2) is Priority.MEDIUM
        assert coerce_priority(4) is Priority.HIGH

    def test_it_rounds_up(self):
        """Under-prioritising a real event costs more than the reverse."""
        assert int(coerce_priority(4)) > int(coerce_priority(3))

    def test_junk_becomes_none_rather_than_raising(self):
        for value in (None, "", "high", object(), float("nan")):
            assert coerce_priority(value) is Priority.NONE

    def test_out_of_range_is_clamped(self):
        assert coerce_priority(99) is Priority.HIGH
        assert coerce_priority(-5) is Priority.NONE

    def test_the_enums_do_not_share_a_vocabulary(self):
        """Task status 1 is not a thing; item status 1 is. Easy to conflate."""
        assert TaskStatus.COMPLETED == 2
        assert ItemStatus.COMPLETE == 1


class TestDatesCarryARealOffset:
    def test_a_naive_datetime_is_treated_as_local_not_utc(self):
        """The bug: a 9pm SF show was sent as 21:00Z, i.e. 2pm local.

        Tasks landed seven hours early on every sync and nothing noticed.
        """
        pacific = timezone(timedelta(hours=-7))
        naive = datetime(2026, 7, 30, 21, 0, 0)
        out = format_api_datetime(naive, pacific)
        assert out == "2026-07-30T21:00:00-0700"
        assert not out.endswith("+0000")

    def test_an_aware_datetime_keeps_its_own_offset(self):
        aware = datetime(2026, 7, 30, 21, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        assert format_api_datetime(aware) == "2026-07-30T21:00:00+0200"

    def test_the_output_parses_back_with_the_documented_format(self):
        out = format_api_datetime(datetime(2026, 1, 2, 3, 4, 5), timezone.utc)
        assert datetime.strptime(out, API_DATETIME)


class TestFromEvent:
    def test_date_and_time_combine(self):
        task = TickTickTask.from_event(
            {"title": "Queer Night", "date": "2026-07-30", "time": "9pm"},
            default_tz=timezone(timedelta(hours=-7)),
        )
        assert task.dueDate.startswith("2026-07-30T21:00:00")
        assert task.isAllDay is None, "a timed event is not all-day"

    def test_a_dateless_time_less_event_is_all_day(self):
        """Previously sent as a timed task at midnight."""
        task = TickTickTask.from_event({"title": "Street Fair", "date": "2026-08-15"})
        assert task.isAllDay is True
        assert task.dueDate.startswith("2026-08-15T00:00:00")

    def test_an_event_with_no_date_gets_no_dates(self):
        task = TickTickTask.from_event({"title": "Someday"})
        assert task.dueDate is None and task.startDate is None
        assert task.isAllDay is None

    def test_an_unparseable_date_is_dropped_not_fatal(self):
        task = TickTickTask.from_event({"title": "X", "date": "next Blursday"})
        assert task.dueDate is None
        assert task.validate() == []

    def test_content_gathers_the_context_a_task_needs(self):
        task = TickTickTask.from_event({
            "title": "Show", "notes": "Doors at 9", "location": "The Stud",
            "link": "https://example.test/e/1", "source": "instagram in stud",
        })
        for fragment in ("Doors at 9", "The Stud", "example.test", "instagram"):
            assert fragment in task.content

    def test_priority_is_coerced_on_the_way_in(self):
        assert TickTickTask.from_event({"title": "X", "priority": 4}).priority is Priority.HIGH

    def test_title_is_bounded(self):
        task = TickTickTask.from_event({"title": "z" * 900})
        assert len(task.title) <= 255


class TestApiPayload:
    def test_unset_fields_are_omitted_not_nulled(self):
        """An explicit null means 'clear this' on the update endpoint."""
        payload = TickTickTask(title="X").to_api()
        assert "dueDate" not in payload
        assert "content" not in payload
        assert None not in payload.values()

    def test_required_fields_are_present(self):
        payload = TickTickTask(title="X", projectId="p1").to_api()
        assert payload["title"] == "X"
        assert payload["projectId"] == "p1"

    def test_enums_serialize_as_integers_not_enum_reprs(self):
        payload = TickTickTask(title="X", priority=Priority.HIGH).to_api()
        assert payload["priority"] == 5
        assert isinstance(payload["priority"], int)
        assert payload["status"] == 0

    def test_the_update_body_always_carries_id_and_projectid(self):
        """A body without them silently targets nothing."""
        payload = TickTickTask(title="X", id="t1", projectId="p1").to_update_api()
        assert payload["id"] == "t1" and payload["projectId"] == "p1"

    def test_checklist_items_serialize(self):
        task = TickTickTask(title="X", items=[
            ChecklistItem(title="Buy ticket"),
            ChecklistItem(title="Done", status=ItemStatus.COMPLETE),
        ])
        items = task.to_api()["items"]
        assert [i["title"] for i in items] == ["Buy ticket", "Done"]
        assert items[1]["status"] == 1

    def test_empty_collections_are_omitted(self):
        payload = TickTickTask(title="X").to_api()
        assert "items" not in payload and "reminders" not in payload

    def test_it_is_json_serializable(self):
        import json

        json.dumps(TickTickTask.from_event(
            {"title": "X", "date": "2026-07-30", "priority": 4},
        ).to_api())


class TestValidate:
    def test_a_well_formed_task_has_no_problems(self):
        assert TickTickTask.from_event(
            {"title": "X", "date": "2026-07-30", "time": "9pm"},
        ).validate() == []

    def test_an_empty_title_is_rejected(self):
        assert any("title" in e for e in TickTickTask(title="   ").validate())

    def test_an_illegal_priority_is_caught(self):
        task = TickTickTask(title="X")
        task.priority = 4  # bypassing coerce_priority, as inline code used to
        assert any("priority" in e for e in task.validate())

    def test_a_malformed_date_is_caught(self):
        task = TickTickTask(title="X", dueDate="2026-07-30")  # no time, no offset
        assert any("dueDate" in e for e in task.validate())

    def test_all_day_without_a_date_is_caught(self):
        assert any("isAllDay" in e for e in TickTickTask(title="X", isAllDay=True).validate())


class TestIntegrationWithTheSyncPath:
    def test_create_ticktick_tasks_builds_a_compliant_object(self, monkeypatch):
        """The failure mode this replaces: a 4 reaching the API verbatim."""
        from event_harvester.outputs import ticktick as sync_mod

        captured = {}

        class FakeTaskApi:
            def builder(self, **kwargs):
                captured.update(kwargs)
                return kwargs

            def create(self, obj):
                return {"id": "new-task-id"}

        class FakeClient:
            inbox_id = "inbox"
            task = FakeTaskApi()
            state = {"projects": []}

        monkeypatch.setattr(sync_mod, "sync_tasks", lambda tt, name: [])
        monkeypatch.setattr(sync_mod, "save_fingerprint", lambda *a, **k: None)

        result = sync_mod.create_ticktick_tasks(
            FakeClient(),
            [{"title": "Queer Night", "date": "2026-07-30", "time": "9pm", "priority": 4}],
        )

        assert result["created"] == ["Queer Night"]
        assert captured["priority"] == 5, "4 is not a legal API priority"
        assert "T21:00:00" in captured["dueDate"], "9pm must survive as 21:00"
        assert not captured["dueDate"].endswith("+0000") or captured["timeZone"] is None
        assert captured["title"] == "Queer Night"

    def test_a_noncompliant_task_is_refused_before_the_api_call(self, monkeypatch):
        from event_harvester.outputs import ticktick as sync_mod

        class FakeTaskApi:
            def builder(self, **kwargs):  # pragma: no cover
                raise AssertionError("should not have reached the API")

            def create(self, obj):  # pragma: no cover
                raise AssertionError("should not have reached the API")

        class FakeClient:
            inbox_id = "inbox"
            task = FakeTaskApi()
            state = {"projects": []}

        monkeypatch.setattr(sync_mod, "sync_tasks", lambda tt, name: [])

        result = sync_mod.create_ticktick_tasks(FakeClient(), [{"title": "   "}])
        assert result["failed"] and not result["created"]


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (2, 3), (3, 3), (4, 5), (5, 5),
])
def test_every_pipeline_priority_maps_to_a_legal_api_value(value, expected):
    assert int(coerce_priority(value)) == expected
