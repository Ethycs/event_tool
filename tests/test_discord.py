"""Tests for Discord cache parsing functions."""

import json

from event_harvester.sources.discord import _parse_message_blobs


class TestParseMessageBlobs:
    def test_parses_array_of_messages(self):
        messages = [
            {"id": "1", "content": "hello", "timestamp": "2026-03-15T10:00:00+00:00"},
            {"id": "2", "content": "world", "timestamp": "2026-03-15T10:01:00+00:00"},
        ]
        body = json.dumps(messages).encode()
        result = _parse_message_blobs(body)
        assert len(result) == 2
        assert result[0]["id"] == "1"

    def test_parses_single_message(self):
        msg = {"id": "42", "content": "test"}
        body = json.dumps(msg).encode()
        result = _parse_message_blobs(body)
        assert len(result) == 1
        assert result[0]["id"] == "42"

    def test_skips_non_dict_items_in_array(self):
        body = json.dumps(
            [{"id": "1", "content": "a"}, "not a dict", 42, {"id": "2", "content": "b"}]
        ).encode()
        result = _parse_message_blobs(body)
        assert len(result) == 2

    def test_returns_empty_for_empty_body(self):
        assert _parse_message_blobs(b"") == []
        assert _parse_message_blobs(None) == []

    def test_returns_empty_for_invalid_json(self):
        assert _parse_message_blobs(b"not json at all") == []

    def test_returns_empty_for_dict_without_id(self):
        body = json.dumps({"content": "no id here"}).encode()
        result = _parse_message_blobs(body)
        assert result == []

    def test_handles_leading_whitespace(self):
        body = b'   [{"id": "1", "content": "padded"}]'
        result = _parse_message_blobs(body)
        assert len(result) == 1

    def test_handles_utf8_content(self):
        body = json.dumps([{"id": "1", "content": "cafe"}]).encode()
        result = _parse_message_blobs(body)
        assert result[0]["content"] == "cafe"


class TestSimpleFileCache:
    def test_reads_simple_file_cache(self, tmp_path, monkeypatch):
        """A SimpleFile-format cache (Linux default) must be detected and read."""
        from datetime import datetime, timezone

        from ccl_chromium_reader import ccl_chromium_cache as ccc
        from event_harvester.sources.discord import _read_cache_messages

        cache_dir = tmp_path / "Cache_Data"
        cache_dir.mkdir()
        (cache_dir / "0123456789abcdef_0").write_bytes(b"")  # simple-cache entry name

        body = json.dumps(
            [{"id": "7", "content": "hi", "timestamp": "2026-03-15T10:00:00+00:00",
              "author": {"username": "u"}, "channel_id": "9"}]
        ).encode()

        class FakeSimple:
            def __init__(self, path):
                pass

            def cache_keys(self):
                return ["https://discord.com/api/v9/channels/9/messages?limit=50"]

            def get_cachefile(self, key):
                return [body]

            def close(self):
                pass

        monkeypatch.setattr(ccc, "ChromiumSimpleFileCache", FakeSimple)
        out = _read_cache_messages(
            datetime(2026, 1, 1, tzinfo=timezone.utc), cache_path=cache_dir
        )
        assert [m["id"] for m in out] == ["7"]
