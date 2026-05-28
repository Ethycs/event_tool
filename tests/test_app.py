"""Tests for app-level helpers in serve.py."""

from event_harvester.cli.commands.serve import _latest_runs_by_source


def test_latest_runs_by_source_empty_root(tmp_path):
    """Missing traces dir returns an empty list."""
    assert _latest_runs_by_source(tmp_path / "does_not_exist") == []


def test_latest_runs_by_source_picks_most_recent(tmp_path):
    """For each source dir, return the lexicographically-latest jsonl stem.

    Synthetic layout:
      tmp/luma/20260514T120000_aaa.jsonl
      tmp/luma/20260514T130000_bbb.jsonl   <- newer, expected pick
      tmp/erobay/20260513T120000_ccc.jsonl
      tmp/empty/                            <- skipped (no jsonl)
      tmp/notes.txt                         <- skipped (not a dir)
    """
    (tmp_path / "luma").mkdir()
    (tmp_path / "luma" / "20260514T120000_aaa.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "luma" / "20260514T130000_bbb.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "erobay").mkdir()
    (tmp_path / "erobay" / "20260513T120000_ccc.jsonl").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "empty").mkdir()
    (tmp_path / "notes.txt").write_text("not a source dir", encoding="utf-8")

    result = _latest_runs_by_source(tmp_path)

    assert ("luma", "20260514T130000_bbb") in result
    assert ("erobay", "20260513T120000_ccc") in result
    assert all(name != "empty" for name, _ in result)
    assert len(result) == 2
