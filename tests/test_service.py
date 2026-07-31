"""Tests for the NSSM service wiring.

Nothing here touches the Windows service manager. What is tested is the part
that has to be right *before* nssm runs: the argument construction, the guards,
and the account warning — because the account is the difference between a
working harvest and a quiet one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from event_harvester.servers import service as svc


def _flat(commands):
    return [" ".join(c) for c in commands]


class TestInstallCommands:
    def test_the_service_launches_the_site_through_pixi(self):
        cmds = svc.build_install_commands(nssm="nssm", pixi="C:/pixi.exe")
        install = cmds[0]
        assert install[:3] == ["nssm", "install", "EventHarvesterSite"]
        assert install[3] == "C:/pixi.exe"
        assert install[4:7] == ["run", "event-harvester", "site"]

    def test_host_port_and_interval_are_passed_through(self):
        cmds = svc.build_install_commands(host="0.0.0.0", port=9000, interval_hours=6)
        args = cmds[0]
        assert "--host" in args and args[args.index("--host") + 1] == "0.0.0.0"
        assert args[args.index("--port") + 1] == "9000"
        assert args[args.index("--interval-hours") + 1] == "6"

    def test_the_working_directory_is_the_repo(self, tmp_path):
        """`.env`, `data/`, token.json and the source list all resolve from it."""
        cmds = svc.build_install_commands(repo_root=tmp_path)
        line = next(c for c in cmds if "AppDirectory" in c)
        assert Path(line[-1]) == tmp_path

    def test_it_starts_automatically(self):
        assert any("SERVICE_AUTO_START" in c for c in _flat(svc.build_install_commands()))

    def test_shutdown_tries_ctrl_c_before_killing(self):
        """So uvicorn's lifespan runs and the refresh tasks get cancelled."""
        line = next(c for c in svc.build_install_commands() if "AppStopMethodConsole" in c)
        assert int(line[-1]) >= 5000

    def test_logs_are_captured_and_rotated(self, tmp_path):
        flat = _flat(svc.build_install_commands(log_dir=tmp_path))
        assert any("AppStdout" in c for c in flat)
        assert any("AppStderr" in c for c in flat)
        assert any("AppRotateFiles 1" in c for c in flat)
        rotate = next(c for c in svc.build_install_commands() if "AppRotateBytes" in c)
        assert int(rotate[-1]) == svc.ROTATE_BYTES

    def test_restart_is_throttled(self):
        """A service crashing on a bad credential must not spin at full speed."""
        cmds = svc.build_install_commands()
        assert any("AppExit" in c and "Restart" in c for c in cmds)
        delay = next(c for c in cmds if "AppRestartDelay" in c)
        assert int(delay[-1]) >= 10000

    def test_extra_args_are_appended(self):
        cmds = svc.build_install_commands(extra_args=["--no-harvest-on-start"])
        assert cmds[0][-1] == "--no-harvest-on-start"

    def test_building_the_commands_runs_nothing(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("ran a command")):
            svc.build_install_commands()


class TestGuards:
    def test_install_requires_elevation(self):
        with patch.object(svc, "is_admin", return_value=False):
            with pytest.raises(svc.ServiceError, match="elevated"):
                svc.install()

    def test_uninstall_requires_elevation(self):
        with patch.object(svc, "is_admin", return_value=False):
            with pytest.raises(svc.ServiceError, match="elevated"):
                svc.uninstall()

    def test_a_missing_nssm_says_how_to_get_it(self):
        with patch.object(svc.shutil, "which", return_value=None):
            with pytest.raises(svc.ServiceError, match="winget install"):
                svc.find_nssm()

    def test_a_missing_pixi_is_named(self):
        with patch.object(svc.shutil, "which", return_value=None):
            with pytest.raises(svc.ServiceError, match="pixi"):
                svc.find_pixi()

    def test_an_unknown_control_action_is_refused(self):
        with pytest.raises(svc.ServiceError, match="unknown action"):
            svc.control("obliterate")


class TestAccountIsTheImportantPart:
    """LocalSystem cannot see Discord, Signal, Gmail or the browser profile."""

    def test_install_does_not_claim_to_have_set_the_account(self, tmp_path):
        with patch.object(svc, "is_admin", return_value=True), \
             patch.object(svc, "find_nssm", return_value="nssm"), \
             patch.object(svc, "find_pixi", return_value="pixi"), \
             patch.object(svc, "LOG_DIR", tmp_path), \
             patch.object(svc, "_check", return_value=""):
            info = svc.install()
        assert info["runs_as"] == "LocalSystem"
        assert "ObjectName" in info["account_command"]
        assert "<your-password>" in info["account_command"], (
            "the command must be handed over, not run — it takes a password"
        )

    def test_install_does_not_start_the_service(self, tmp_path):
        """The first start harvests every source and spends tokens."""
        ran = []
        with patch.object(svc, "is_admin", return_value=True), \
             patch.object(svc, "find_nssm", return_value="nssm"), \
             patch.object(svc, "find_pixi", return_value="pixi"), \
             patch.object(svc, "LOG_DIR", tmp_path), \
             patch.object(svc, "_check", side_effect=lambda cmd: ran.append(cmd) or ""):
            info = svc.install()
        assert info["started"] is False
        assert not any("start" in c[1:2] for c in ran)

    def test_status_flags_localsystem(self):
        def fake_run(cmd):
            key = cmd[-1]
            out = {"ObjectName": "LocalSystem"}.get(key, "x")
            if cmd[1] == "status":
                out = "SERVICE_RUNNING"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        with patch.object(svc, "find_nssm", return_value="nssm"), \
             patch.object(svc, "_run", side_effect=fake_run):
            info = svc.status()
        assert info["account_is_localsystem"] is True
        assert info["state"] == "SERVICE_RUNNING"

    def test_status_on_a_missing_service_is_not_an_exception(self):
        with patch.object(svc, "find_nssm", return_value="nssm"), \
             patch.object(svc, "_run", return_value=subprocess.CompletedProcess(
                 [], 1, stdout="", stderr="service not found")):
            info = svc.status()
        assert info["installed"] is False
        assert "not found" in info["error"]


class TestErrorReporting:
    def test_nssm_utf16_nulls_are_stripped_from_errors(self):
        """nssm writes UTF-16; text-mode subprocess leaves NULs in the message."""
        with patch.object(svc, "_run", return_value=subprocess.CompletedProcess(
                ["nssm", "install", "X"], 5, stdout="", stderr="A\x00c\x00c\x00e\x00s\x00s\x00")):
            with pytest.raises(svc.ServiceError, match="Access"):
                svc._check(["nssm", "install", "X"])

    def test_tail_logs_survives_a_missing_file(self, tmp_path):
        with patch.object(svc, "status", return_value={
            "ok": True, "stdout_log": str(tmp_path / "nope.log"), "stderr_log": "",
        }):
            out = svc.tail_logs()
        assert out["stdout_log"] == [] and out["stderr_log"] == []

    def test_tail_logs_returns_the_tail(self, tmp_path):
        log = tmp_path / "out.log"
        log.write_text("\n".join(str(i) for i in range(100)), encoding="utf-8")
        with patch.object(svc, "status", return_value={
            "ok": True, "stdout_log": str(log), "stderr_log": "",
        }):
            out = svc.tail_logs(lines=5)
        assert out["stdout_log"] == ["95", "96", "97", "98", "99"]
