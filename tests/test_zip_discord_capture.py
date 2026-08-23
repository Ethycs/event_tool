"""The consolidation half of passive Discord capture: dedup and bundle.

The BetterDiscord capture plugin appends every message you browse to per-channel
JSONL without deduping (simple, crash-safe). `scripts/zip_discord_capture.py`
consolidates: dedup by id, edit-wins, sorted by time. The plugin can't be tested
here (it runs in Discord), but this half is pure and must be.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "zip_discord_capture",
    Path(__file__).resolve().parent.parent / "scripts" / "zip_discord_capture.py",
)
zdc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(zdc)


def _write(p: Path, rows: list[dict]) -> None:
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class TestDedup:
    def test_a_duplicate_id_collapses_to_one(self, tmp_path):
        f = tmp_path / "g_c.jsonl"
        _write(f, [
            {"id": "1", "timestamp": "2026-08-01T10:00:00Z", "content": "a"},
            {"id": "1", "timestamp": "2026-08-01T10:00:00Z", "content": "a"},
            {"id": "2", "timestamp": "2026-08-02T10:00:00Z", "content": "b"},
        ])
        out = zdc._dedup(f)
        assert [m["id"] for m in out] == ["1", "2"]

    def test_last_write_wins_so_edits_are_kept(self, tmp_path):
        f = tmp_path / "g_c.jsonl"
        _write(f, [
            {"id": "1", "timestamp": "2026-08-01T10:00:00Z", "content": "original"},
            {"id": "1", "timestamp": "2026-08-01T10:00:00Z", "content": "edited"},
        ])
        assert zdc._dedup(f)[0]["content"] == "edited"

    def test_output_is_time_sorted(self, tmp_path):
        f = tmp_path / "g_c.jsonl"
        _write(f, [
            {"id": "2", "timestamp": "2026-08-05T10:00:00Z"},
            {"id": "1", "timestamp": "2026-08-01T10:00:00Z"},
        ])
        assert [m["id"] for m in zdc._dedup(f)] == ["1", "2"]

    def test_malformed_lines_are_skipped(self, tmp_path):
        f = tmp_path / "g_c.jsonl"
        f.write_text('{"id":"1","timestamp":"t"}\n{bad json\n\n', encoding="utf-8")
        assert [m["id"] for m in zdc._dedup(f)] == ["1"]

    def test_rows_without_an_id_are_ignored(self, tmp_path):
        f = tmp_path / "g_c.jsonl"
        _write(f, [{"timestamp": "t", "content": "no id"}, {"id": "1", "timestamp": "t"}])
        assert [m["id"] for m in zdc._dedup(f)] == ["1"]


class TestCaptureDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DISCORD_CAPTURE_DIR", str(tmp_path))
        assert zdc.capture_dir() == tmp_path

    def test_default_is_under_the_client_reachable_dir(self, monkeypatch):
        monkeypatch.delenv("DISCORD_CAPTURE_DIR", raising=False)
        monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
        p = zdc.capture_dir()
        assert p.name == "messages" and "discord-harvester" in str(p)
