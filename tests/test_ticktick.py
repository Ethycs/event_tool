"""Tests for TickTick task creation and deduplication."""

from unittest.mock import MagicMock, patch

from event_harvester.ticktick import create_ticktick_tasks


class TestCreateTickTickTasks:
    def test_dry_run_does_not_call_api(self):
        tt = MagicMock()
        tasks = [
            {"title": "Test task", "notes": "test", "priority": 1, "due_in_days": 2}
        ]

        with patch("event_harvester.ticktick.sync_tasks", return_value=[]):
            result = create_ticktick_tasks(tt, tasks, dry_run=True)

        assert len(result["created"]) == 1
        tt.task.builder.assert_not_called()
        tt.task.create.assert_not_called()

    def test_empty_tasks_returns_empty(self):
        tt = MagicMock()
        result = create_ticktick_tasks(tt, [], dry_run=False)
        assert result == {"created": [], "updated": [], "skipped": []}
