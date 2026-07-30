"""Tests for TickTick task creation and deduplication."""

from unittest.mock import MagicMock, patch

import pytest

from event_harvester.outputs.ticktick import create_ticktick_tasks


class TestFailureReporting:
    """A failed sync must be reportable to the caller.

    `create_ticktick_tasks` deliberately catches per-task exceptions so one
    bad event cannot abort a batch. That left callers unable to distinguish
    success from failure, and the Flet UI treated "the function returned" as
    "the task exists" — writing an `approved` fingerprint for an event that
    was never created, which permanently removed it from the review queue.
    """

    def _one_task(self):
        return [{"title": "Boom", "notes": "n", "priority": 1, "due_in_days": 1}]

    def test_creation_failure_is_reported_not_swallowed(self):
        tt = MagicMock()
        tt.task.create.side_effect = RuntimeError("TickTick 500")

        with patch("event_harvester.outputs.ticktick.sync_tasks", return_value=[]):
            result = create_ticktick_tasks(tt, self._one_task(), dry_run=False)

        assert result["created"] == []
        assert result["failed"] == ["Boom"]

    def test_no_fingerprint_saved_when_creation_fails(self):
        """A failed task must not leave an 'approved' fingerprint behind."""
        tt = MagicMock()
        tt.task.create.side_effect = RuntimeError("TickTick 500")

        with patch("event_harvester.outputs.ticktick.sync_tasks", return_value=[]), \
             patch("event_harvester.outputs.ticktick.save_fingerprint") as save_fp:
            create_ticktick_tasks(tt, self._one_task(), dry_run=False)

        save_fp.assert_not_called()

    def test_successful_creation_saves_an_approved_fingerprint(self):
        """Not "auto" — see the `_ACTED_STATUSES` filter in event_match.

        With status="auto", `find_acted_fingerprint` ignored the entry, so a
        CLI-synced event came back tagged `new` on the next MCP/Flet run and
        was offered for approval all over again.
        """
        tt = MagicMock()
        tt.task.create.return_value = {"id": "tt-123"}

        with patch("event_harvester.outputs.ticktick.sync_tasks", return_value=[]), \
             patch("event_harvester.outputs.ticktick.save_fingerprint") as save_fp:
            result = create_ticktick_tasks(tt, self._one_task(), dry_run=False)

        assert result["created"] == ["Boom"]
        assert result["failed"] == []
        save_fp.assert_called_once()
        assert save_fp.call_args.kwargs["status"] == "approved"

    @pytest.mark.parametrize("bucket", ["created", "updated", "skipped", "failed"])
    def test_result_always_has_every_bucket(self, bucket):
        tt = MagicMock()
        with patch("event_harvester.outputs.ticktick.sync_tasks", return_value=[]):
            result = create_ticktick_tasks(tt, [], dry_run=False)
        assert bucket in result


class TestCreateTickTickTasks:
    def test_dry_run_does_not_call_api(self):
        tt = MagicMock()
        tasks = [
            {"title": "Test task", "notes": "test", "priority": 1, "due_in_days": 2}
        ]

        with patch("event_harvester.outputs.ticktick.sync_tasks", return_value=[]):
            result = create_ticktick_tasks(tt, tasks, dry_run=True)

        assert len(result["created"]) == 1
        tt.task.builder.assert_not_called()
        tt.task.create.assert_not_called()

    def test_empty_tasks_returns_empty(self):
        tt = MagicMock()
        result = create_ticktick_tasks(tt, [], dry_run=False)
        assert result == {"created": [], "updated": [], "skipped": [], "failed": []}
