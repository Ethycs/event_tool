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


class TestTheServiceRegistry:
    """Three services, and the differences between them are the whole point."""

    def test_every_service_in_the_order_has_a_spec(self):
        assert set(svc.SERVICE_ORDER) == set(svc.SERVICES)

    def test_the_backend_is_installed_before_the_gateway_that_depends_on_it(self):
        order = list(svc.SERVICE_ORDER)
        assert order.index("mcp") < order.index("gateway")

    def test_an_unknown_service_is_refused_by_name(self):
        with pytest.raises(svc.ServiceError, match="unknown service"):
            svc.spec_for("dispatch")

    def test_the_three_services_do_not_share_a_port(self):
        ports = [s.default_port for s in svc.SERVICES.values()]
        assert len(set(ports)) == len(ports)

    def test_the_three_services_do_not_share_a_name(self):
        names = [s.default_name for s in svc.SERVICES.values()]
        assert len(set(names)) == len(names)

    def test_the_mcp_service_runs_streamable_http(self):
        """stdio is the default transport and would exit immediately as a service."""
        args = svc.build_install_commands(service="mcp")[0]
        assert args[4:7] == ["run", "event-harvester", "mcp"]
        assert args[args.index("--transport") + 1] == "streamable-http"

    def test_the_mcp_service_defaults_to_its_own_port(self):
        args = svc.build_install_commands(service="mcp")[0]
        assert args[args.index("--port") + 1] == "8765"

    def test_allow_any_host_is_off_unless_asked_for(self):
        assert "--allow-any-host" not in svc.build_install_commands(service="mcp")[0]
        assert "--allow-any-host" in svc.build_install_commands(
            service="mcp", allow_any_host=True)[0]

    def test_the_gateway_runs_the_pixi_task(self):
        args = svc.build_install_commands(service="gateway")[0]
        assert args[4:] == ["run", "gateway"]

    def test_the_gateway_is_configured_by_environment(self):
        line = next(c for c in svc.build_install_commands(service="gateway")
                    if "AppEnvironmentExtra" in c)
        env = dict(pair.split("=", 1) for pair in line[5:])
        assert env["GATEWAY_PORT"] == "9000"
        assert env["EH_MCP_URL"] == "http://127.0.0.1:8765/mcp"

    def test_the_gateway_backend_is_overridable(self):
        line = next(c for c in svc.build_install_commands(
            service="gateway", backend_url="http://127.0.0.1:7000/mcp")
            if "AppEnvironmentExtra" in c)
        assert "EH_MCP_URL=http://127.0.0.1:7000/mcp" in line

    def test_no_secret_is_written_into_the_service_config(self):
        """`AppEnvironmentExtra` lands in the registry in plaintext.

        Tokens and OAuth client secrets belong in `.env`, which the process
        reads from its working directory instead.
        """
        env = svc.build_service_env(
            svc.spec_for("gateway"), host="127.0.0.1", port=9000)
        assert not any(
            k.endswith(("TOKEN", "SECRET", "PASSWORD", "KEY")) for k in env
        ), env

    def test_the_gateway_starts_after_the_backend(self):
        line = next(c for c in svc.build_install_commands(service="gateway")
                    if "DependOnService" in c)
        assert line[-1] == svc.SERVICES["mcp"].default_name

    def test_services_without_dependencies_do_not_declare_any(self):
        for key in ("site", "mcp"):
            cmds = svc.build_install_commands(service=key)
            assert not any("DependOnService" in c for c in cmds), key

    def test_each_service_logs_to_its_own_file(self, tmp_path):
        stems = set()
        for key in svc.SERVICE_ORDER:
            line = next(c for c in svc.build_install_commands(service=key, log_dir=tmp_path)
                        if "AppStdout" in c)
            stems.add(Path(line[-1]).name)
        assert len(stems) == 3, f"log files collide: {stems}"

    def test_only_the_gateway_is_exempt_from_the_account_warning(self):
        """It proxies; it reads no user-scoped credential of its own."""
        assert svc.spec_for("gateway").needs_user_account is False
        assert svc.spec_for("site").needs_user_account is True
        assert svc.spec_for("mcp").needs_user_account is True

    def test_an_explicit_name_overrides_the_default(self):
        assert svc.service_name("mcp", "Custom") == "Custom"
        assert svc.service_name("mcp") == "EventHarvesterMCP"

    @pytest.mark.parametrize("key", ["site", "mcp", "gateway"])
    def test_every_service_gets_the_shared_operational_wiring(self, key):
        flat = _flat(svc.build_install_commands(service=key))
        for setting in ("AppDirectory", "AppStdout", "AppStderr", "AppRotateFiles",
                        "SERVICE_AUTO_START", "AppExit", "AppThrottle"):
            assert any(setting in c for c in flat), f"{key} missing {setting}"


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


class TestTheCliActuallyRoutes:
    """Through the real parser and the real command, not a hand-built namespace.

    A service command that parses fine and then acts on the wrong service is
    the failure this catches — and the one an object-level test would miss.
    """

    @staticmethod
    def _parse(argv):
        from event_harvester.cli.parser import build_parser

        return build_parser().parse_args(argv)

    def test_the_default_service_is_still_the_site(self):
        assert self._parse(["service", "status"]).service == "site"

    def test_a_service_can_be_named(self):
        assert self._parse(["service", "start", "--service", "gateway"]).service == "gateway"

    def test_all_installs_backend_first(self):
        from event_harvester.cli.commands import service as cmd

        args = self._parse(["service", "install", "--all"])
        assert cmd._targets(args, svc) == ["mcp", "gateway", "site"]

    @pytest.mark.parametrize("action", ["stop", "uninstall"])
    def test_a_dependent_is_taken_down_before_what_it_depends_on(self, action):
        """Stopping the backend under a live gateway leaves it proxying nothing.

        The assertion is the ordering relation, not a position: `site` has no
        dependencies and may sit anywhere in the sequence.
        """
        from event_harvester.cli.commands import service as cmd

        order = cmd._targets(self._parse(["service", action, "--all"]), svc)
        assert order.index("gateway") < order.index("mcp")

    def test_every_service_is_covered_by_all(self):
        from event_harvester.cli.commands import service as cmd

        order = cmd._targets(self._parse(["service", "install", "--all"]), svc)
        assert sorted(order) == sorted(svc.SERVICES)

    async def test_renaming_three_services_at_once_is_refused(self):
        from event_harvester.cli.commands import service as cmd

        args = self._parse(["service", "status", "--all", "--name", "OnlyOne"])
        assert await cmd.service_cmd(args, None) == 1

    async def test_the_command_acts_on_the_service_it_was_given(self):
        from event_harvester.cli.commands import service as cmd

        seen = []
        args = self._parse(["service", "start", "--service", "gateway"])
        with patch.object(svc, "control",
                          side_effect=lambda a, service, name: seen.append((a, service))
                          or {"name": "X", "service": service}):
            assert await cmd.service_cmd(args, None) == 0
        assert seen == [("start", "gateway")]

    async def test_a_service_error_is_reported_not_raised(self):
        from event_harvester.cli.commands import service as cmd

        args = self._parse(["service", "start"])
        with patch.object(svc, "control", side_effect=svc.ServiceError("nope")):
            assert await cmd.service_cmd(args, None) == 1


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
