"""Tests for TickTick task creation and deduplication."""

from unittest.mock import MagicMock, patch

from event_harvester.ticktick import (
    _hash_task,
    _load_created_hashes,
    _save_created_hashes,
    create_ticktick_tasks,
)


class TestTaskHashing:
    def test_consistent_hash(self):
        assert _hash_task("Review PR") == _hash_task("Review PR")

    def test_case_insensitive(self):
        assert _hash_task("Review PR") == _hash_task("review pr")

    def test_strips_whitespace(self):
        assert _hash_task("  Review PR  ") == _hash_task("Review PR")

    def test_different_titles_different_hashes(self):
        assert _hash_task("Review PR") != _hash_task("Deploy staging")


class TestDedupPersistence:
    def test_roundtrip(self, tmp_path):
        dedup_file = tmp_path / "created_tasks.json"
        hashes = {"abc123", "def456"}

        with patch("event_harvester.ticktick._DEDUP_FILE", dedup_file):
            _save_created_hashes(hashes)
            loaded = _load_created_hashes()

        assert loaded == hashes

    def test_load_missing_file(self, tmp_path):
        dedup_file = tmp_path / "nonexistent.json"
        with patch("event_harvester.ticktick._DEDUP_FILE", dedup_file):
            assert _load_created_hashes() == set()

    def test_load_corrupt_file(self, tmp_path):
        dedup_file = tmp_path / "corrupt.json"
        dedup_file.write_text("not json")
        with patch("event_harvester.ticktick._DEDUP_FILE", dedup_file):
            assert _load_created_hashes() == set()


class TestCreateTickTickTasks:
    def test_dry_run_does_not_call_api(self):
        tt = MagicMock()
        tasks = [
            {"title": "Test task", "notes": "test", "priority": 1, "due_in_days": 2}
        ]

        with patch("event_harvester.ticktick._load_created_hashes", return_value=set()):
            with patch("event_harvester.ticktick._save_created_hashes"):
                result = create_ticktick_tasks(tt, tasks, dry_run=True)

        assert len(result) == 1
        tt.task.builder.assert_not_called()
        tt.task.create.assert_not_called()

    def test_skips_duplicate_tasks(self):
        tt = MagicMock()
        tasks = [
            {"title": "Existing task", "notes": "", "priority": 0, "due_in_days": None}
        ]
        existing_hash = _hash_task("Existing task")

        with patch("event_harvester.ticktick._load_created_hashes", return_value={existing_hash}):
            with patch("event_harvester.ticktick._save_created_hashes"):
                result = create_ticktick_tasks(tt, tasks, dry_run=False)

        assert len(result) == 0
        tt.task.create.assert_not_called()

    def test_empty_tasks_returns_empty(self):
        tt = MagicMock()
        result = create_ticktick_tasks(tt, [], dry_run=False)
        assert result == []
