"""Tests for process identity.

The point of this module is answering "is the running service current?"
without archaeology. So the tests that matter are the ones about *staleness*
detection and about not lying when the answer is unknown.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from event_harvester import buildinfo


@pytest.fixture
def fake_repo(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    return tmp_path, git


class TestReadingTheSha:
    def test_a_symbolic_ref_resolves_through_the_loose_ref(self, fake_repo):
        root, git = fake_repo
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
        assert buildinfo._read_head_sha(root) == "a" * 40

    def test_a_detached_head_is_the_sha_itself(self, fake_repo):
        root, git = fake_repo
        (git / "HEAD").write_text("b" * 40 + "\n", encoding="utf-8")
        assert buildinfo._read_head_sha(root) == "b" * 40

    def test_a_packed_ref_is_found(self, fake_repo):
        """The normal state after a clone — no loose ref file exists."""
        root, git = fake_repo
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{'c' * 40} refs/heads/main\n"
            f"{'d' * 40} refs/remotes/origin/main\n",
            encoding="utf-8",
        )
        assert buildinfo._read_head_sha(root) == "c" * 40

    def test_no_git_directory_is_none_not_a_crash(self, tmp_path):
        assert buildinfo._read_head_sha(tmp_path) is None

    def test_a_missing_ref_is_none(self, fake_repo):
        root, git = fake_repo
        (git / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")
        assert buildinfo._read_head_sha(root) is None


class TestStaleness:
    def test_it_detects_a_moved_checkout(self, monkeypatch):
        monkeypatch.setattr(buildinfo, "booted_sha", lambda: "a" * 40)
        monkeypatch.setattr(buildinfo, "current_sha", lambda: "b" * 40)
        assert buildinfo.is_stale() is True

    def test_an_unmoved_checkout_is_not_stale(self, monkeypatch):
        monkeypatch.setattr(buildinfo, "booted_sha", lambda: "a" * 40)
        monkeypatch.setattr(buildinfo, "current_sha", lambda: "a" * 40)
        assert buildinfo.is_stale() is False

    def test_unknown_is_none_and_never_false(self, monkeypatch):
        """Absent information must not read as healthy."""
        monkeypatch.setattr(buildinfo, "booted_sha", lambda: None)
        monkeypatch.setattr(buildinfo, "current_sha", lambda: "a" * 40)
        assert buildinfo.is_stale() is None

        monkeypatch.setattr(buildinfo, "booted_sha", lambda: "a" * 40)
        monkeypatch.setattr(buildinfo, "current_sha", lambda: None)
        assert buildinfo.is_stale() is None

    def test_the_booted_sha_is_cached_so_it_cannot_follow_the_tree(self):
        """The whole point: it must remember what this process started on."""
        first = buildinfo.booted_sha()
        second = buildinfo.booted_sha()
        assert first == second
        assert buildinfo.booted_sha.cache_info().hits >= 1

    def test_each_server_records_its_commit_at_start_not_at_the_first_question(self):
        """Measured 2026-11-24: a server started at 05:47 on one commit, first
        asked at 05:53 after another, reported the later commit, a 05:53 start
        and `stale: false`. The modules imported buildinfo inside the handler."""
        import subprocess
        import sys

        probe = (
            "import sys, importlib\n"
            "importlib.import_module(sys.argv[1])\n"
            "b = sys.modules.get('event_harvester.buildinfo')\n"
            "print(bool(b) and b.booted_sha.cache_info().currsize)\n"
        )
        for module in ("mcp_server", "gateway", "site"):
            out = subprocess.run(
                [sys.executable, "-c", probe, f"event_harvester.servers.{module}"],
                capture_output=True, text=True, timeout=120,
            )
            assert out.stdout.strip().endswith("1"), (module, out.stdout, out.stderr[-300:])


class TestBuildInfo:
    def test_it_carries_what_a_diagnosis_needs(self):
        info = buildinfo.build_info()
        for key in ("version", "sha", "sha_current", "stale", "pid",
                    "started_at", "uptime_seconds"):
            assert key in info, key

    def test_the_pid_is_this_process(self):
        import os

        assert buildinfo.build_info()["pid"] == os.getpid()

    def test_shas_are_shortened_for_display(self):
        info = buildinfo.build_info()
        if info["sha"]:
            assert len(info["sha"]) == 12

    def test_uptime_advances(self):
        later = buildinfo.PROCESS_STARTED_AT + timedelta(seconds=90)
        assert buildinfo.uptime_seconds(later) == pytest.approx(90, abs=1)

    def test_describe_is_one_line(self):
        assert "\n" not in buildinfo.describe()
