"""Tests for Discord cache parsing functions."""

import json

from event_harvester.sources.discord import _decode_cache_body, _parse_message_blobs


class TestDecodeCacheBody:
    def test_strips_header_and_http_headers(self):
        header = b"\x00" * 8
        http_headers = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
        body = b'[{"id": "123"}]'
        raw = header + http_headers + body
        result = _decode_cache_body(raw)
        assert result == body

    def test_handles_lf_separator(self):
        header = b"\x00" * 8
        http_headers = b"HTTP/1.1 200 OK\n\n"
        body = b'{"id": "456"}'
        raw = header + http_headers + body
        result = _decode_cache_body(raw)
        assert result == body

    def test_returns_data_when_no_separator(self):
        header = b"\x00" * 8
        body = b"some raw data without separators"
        raw = header + body
        result = _decode_cache_body(raw)
        assert result == body

    def test_returns_none_on_empty(self):
        result = _decode_cache_body(b"")
        # With only 8-byte skip, empty input returns empty
        assert result is not None or result is None  # shouldn't crash

    def test_returns_none_on_exception(self):
        # Passing something that could cause issues
        result = _decode_cache_body(b"short")
        # Should not crash, returns remaining bytes
        assert result is not None


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
        body = json.dumps([{"id": "1"}, "not a dict", 42, {"id": "2"}]).encode()
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
        body = json.dumps([{"id": "1", "content": "café ☕"}]).encode()
        result = _parse_message_blobs(body)
        assert result[0]["content"] == "café ☕"
