"""Tests for LLM analysis - prompt building and response parsing."""

import json
from unittest.mock import MagicMock, patch

from event_harvester.analysis import analyse_and_extract_tasks, build_prompt
from event_harvester.config import LLMConfig


class TestBuildPrompt:
    def test_groups_by_platform_and_channel(self, sample_messages):
        prompt = build_prompt(sample_messages, 7)
        assert "Discord / 123456" in prompt
        assert "Discord / 789012" in prompt
        assert "Telegram / Project Updates" in prompt

    def test_includes_message_content(self, sample_messages):
        prompt = build_prompt(sample_messages, 7)
        assert "review the PR" in prompt
        assert "smoke tests" in prompt

    def test_truncates_long_content(self):
        messages = [
            {
                "platform": "discord",
                "id": "1",
                "timestamp": "2026-03-15T10:00:00+00:00",
                "author": "user",
                "channel": "ch",
                "content": "x" * 500,
            }
        ]
        prompt = build_prompt(messages, 1)
        lines = [line for line in prompt.split("\n") if "user:" in line]
        assert len(lines) == 1
        content_part = lines[0].split("user: ")[1]
        assert len(content_part) <= 400

    def test_limits_messages_per_chat(self):
        messages = [
            {
                "platform": "discord",
                "id": str(i),
                "timestamp": f"2026-03-15T{10 + i // 60:02d}:{i % 60:02d}:00+00:00",
                "author": "user",
                "channel": "ch",
                "content": f"msg {i}",
            }
            for i in range(100)
        ]
        prompt = build_prompt(messages, 1)
        assert "msg 99" in prompt
        assert "msg 40" in prompt
        assert "msg 39" not in prompt

    def test_shows_message_count_header(self, sample_messages):
        prompt = build_prompt(sample_messages, 3)
        assert "5 messages" in prompt
        assert "3 day(s)" in prompt
        assert "3 chat(s)" in prompt

    def test_empty_messages(self):
        prompt = build_prompt([], 7)
        assert "0 messages" in prompt


class TestAnalyseAndExtractTasks:
    def test_returns_empty_when_not_configured(self):
        cfg = LLMConfig(model="")
        summary, tasks = analyse_and_extract_tasks([], 7, cfg)
        assert summary == ""
        assert tasks == []

    def test_parses_valid_response(self, sample_messages):
        response_data = {
            "summary": "Test summary",
            "tasks": [
                {
                    "title": "Review auth PR",
                    "notes": "Discord / 123456, alice asked",
                    "priority": 3,
                    "due_in_days": 1,
                }
            ],
        }

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(response_data)
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        cfg = LLMConfig(model="openrouter/test-model")

        with patch(
            "event_harvester.analysis.completion",
            return_value=mock_resp,
        ):
            summary, tasks = analyse_and_extract_tasks(
                sample_messages, 7, cfg,
            )

        assert summary == "Test summary"
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Review auth PR"

    def test_handles_malformed_json(self, sample_messages):
        mock_choice = MagicMock()
        mock_choice.message.content = "not valid json {"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        cfg = LLMConfig(model="openrouter/test-model")

        with patch(
            "event_harvester.analysis.completion",
            return_value=mock_resp,
        ):
            summary, tasks = analyse_and_extract_tasks(
                sample_messages, 7, cfg,
            )

        assert tasks == []
        assert summary == "not valid json {"
