"""Shared test fixtures."""

import pytest


@pytest.fixture
def sample_messages():
    """A set of sample messages from both platforms."""
    return [
        {
            "platform": "discord",
            "id": "1001",
            "timestamp": "2026-03-15T10:00:00+00:00",
            "author": "alice",
            "channel": "123456",
            "content": "Hey, can someone review the PR for the auth fix?",
        },
        {
            "platform": "discord",
            "id": "1002",
            "timestamp": "2026-03-15T10:05:00+00:00",
            "author": "bob",
            "channel": "123456",
            "content": "Sure, I'll take a look after lunch.",
        },
        {
            "platform": "telegram",
            "id": "2001",
            "timestamp": "2026-03-15T11:00:00+00:00",
            "author": "charlie",
            "channel": "Project Updates",
            "content": "Deploy to staging is done. Need someone to run smoke tests before 5pm.",
        },
        {
            "platform": "telegram",
            "id": "2002",
            "timestamp": "2026-03-15T11:30:00+00:00",
            "author": "alice",
            "channel": "Project Updates",
            "content": "Nice weather today!",
        },
        {
            "platform": "discord",
            "id": "1003",
            "timestamp": "2026-03-16T09:00:00+00:00",
            "author": "dave",
            "channel": "789012",
            "content": "The API rate limiter config needs updating before the launch next Tuesday.",
        },
    ]
