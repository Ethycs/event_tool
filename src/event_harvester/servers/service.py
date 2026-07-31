"""Run the events site as a Windows service, via NSSM.

NSSM is the right tool here because the thing being supervised is a plain
console process: `pixi run event-harvester site`. NSSM gives it automatic
start, restart-on-crash, and log rotation without the process needing to know
it is a service.

**The account matters more than anything else in this file.** NSSM defaults a
new service to `LocalSystem`, and almost every credential this project uses is
scoped to *your* user:

* Discord's cache under `%APPDATA%\\discord`
* Signal's database under `%APPDATA%\\Signal`
* `token.json` and `harvest_session.session` at the repo root
* the browser profile the web sources log into

Under `LocalSystem`, `%APPDATA%` is a different directory, so those sources go
quiet rather than erroring loudly — a harvest that "succeeded" with four fewer
sources. `install()` therefore refuses to pretend, and prints the exact
`nssm set ... ObjectName` command needed. That command takes a password, which
is why this code does not run it for you.

Session 0 is the other caveat: a service cannot show a window. Headless
Playwright is fine; anything that wanted a visible browser (the native-Chrome
Cloudflare path) is not. `/healthz` and the per-source counts are how you find
out which.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
from pathlib import Path

from event_harvester.observability.tracing import set_span_attrs, traced
from event_harvester.utils import REPO_ROOT, data_path

logger = logging.getLogger("event_harvester.service")
_TRACER = "event_harvester.servers.service"

DEFAULT_SERVICE_NAME = "EventHarvesterSite"
LOG_DIR = data_path(".service")

#: 10 MB, then rotate. A harvest logs per source per run; unbounded is a
#: disk-filler on a box that also holds browser profiles and traces.
ROTATE_BYTES = 10 * 1024 * 1024


class ServiceError(RuntimeError):
    pass


def find_nssm() -> str:
    exe = shutil.which("nssm")
    if not exe:
        raise ServiceError(
            "nssm is not on PATH. Install it with `winget install NSSM.NSSM` "
            "(or scoop/choco), then re-run."
        )
    return exe


def find_pixi() -> str:
    exe = shutil.which("pixi")
    if not exe:
        raise ServiceError("pixi is not on PATH; the service needs it to launch the app.")
    return exe


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - non-Windows or restricted
        return False


def build_install_commands(
    *,
    name: str = DEFAULT_SERVICE_NAME,
    host: str = "127.0.0.1",
    port: int = 8800,
    interval_hours: float = 24.0,
    nssm: str = "nssm",
    pixi: str = "pixi",
    repo_root: Path | None = None,
    log_dir: Path | None = None,
    extra_args: list[str] | None = None,
) -> list[list[str]]:
    """Return the ordered nssm invocations that install and configure the service.

    Pure: builds the commands without running them, so the wiring is testable
    without touching the Windows service manager.
    """
    root = Path(repo_root or REPO_ROOT)
    logs = Path(log_dir or LOG_DIR)

    app_args = [
        "run", "event-harvester", "site",
        "--host", str(host),
        "--port", str(port),
        "--interval-hours", str(interval_hours),
    ]
    app_args.extend(extra_args or [])

    return [
        [nssm, "install", name, pixi, *app_args],
        # Working directory must be the repo: .env, data/, token.json and the
        # source list are all resolved relative to it.
        [nssm, "set", name, "AppDirectory", str(root)],
        [nssm, "set", name, "DisplayName", "Event Harvester — events site"],
        [nssm, "set", name, "Description",
         f"Serves the events site on {host}:{port} and re-harvests every {interval_hours}h."],
        [nssm, "set", name, "Start", "SERVICE_AUTO_START"],
        # Ctrl-C first so uvicorn's lifespan shutdown runs and the refresh
        # tasks are cancelled; kill only if it will not go.
        [nssm, "set", name, "AppStopMethodConsole", "15000"],
        [nssm, "set", name, "AppStdout", str(logs / "site.out.log")],
        [nssm, "set", name, "AppStderr", str(logs / "site.err.log")],
        [nssm, "set", name, "AppRotateFiles", "1"],
        [nssm, "set", name, "AppRotateOnline", "1"],
        [nssm, "set", name, "AppRotateBytes", str(ROTATE_BYTES)],
        # Restart on exit, but back off — a service that crashes on a bad
        # credential would otherwise spin at full speed.
        [nssm, "set", name, "AppExit", "Default", "Restart"],
        [nssm, "set", name, "AppRestartDelay", "30000"],
        [nssm, "set", name, "AppThrottle", "10000"],
    ]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    logger.debug("service: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def _check(cmd: list[str]) -> str:
    proc = _run(cmd)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        # nssm writes UTF-16; subprocess in text mode leaves NULs behind.
        detail = detail.replace("\x00", "")
        raise ServiceError(f"{' '.join(cmd[:3])} failed ({proc.returncode}): {detail}")
    return (proc.stdout or "").replace("\x00", "").strip()


@traced("service.install", tracer_name=_TRACER)
def install(
    *,
    name: str = DEFAULT_SERVICE_NAME,
    host: str = "127.0.0.1",
    port: int = 8800,
    interval_hours: float = 24.0,
    extra_args: list[str] | None = None,
) -> dict:
    """Install and configure the service. Does **not** start it.

    Not starting is deliberate: the first start runs a harvest, which opens
    browsers, contacts every configured source, and spends LLM tokens. That is
    a standing commitment and should begin when you say so.
    """
    if not is_admin():
        raise ServiceError("Installing a Windows service requires an elevated shell.")

    nssm, pixi = find_nssm(), find_pixi()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    commands = build_install_commands(
        name=name, host=host, port=port, interval_hours=interval_hours,
        nssm=nssm, pixi=pixi, extra_args=extra_args,
    )
    for cmd in commands:
        _check(cmd)

    account = f"{os.environ.get('USERDOMAIN', '.')}\\{getpass.getuser()}"
    set_span_attrs(**{"service.name": name, "service.port": port})
    logger.info("service: installed %s", name)
    return {
        "ok": True,
        "name": name,
        "url": f"http://{host}:{port}",
        "logs": str(LOG_DIR),
        "runs_as": "LocalSystem",
        "account_command": f'nssm set {name} ObjectName "{account}" "<your-password>"',
        "started": False,
    }


@traced("service.uninstall", tracer_name=_TRACER)
def uninstall(*, name: str = DEFAULT_SERVICE_NAME) -> dict:
    if not is_admin():
        raise ServiceError("Removing a Windows service requires an elevated shell.")
    nssm = find_nssm()
    _run([nssm, "stop", name])  # may already be stopped; not an error
    _check([nssm, "remove", name, "confirm"])
    logger.info("service: removed %s", name)
    return {"ok": True, "name": name, "removed": True}


@traced("service.control", tracer_name=_TRACER)
def control(action: str, *, name: str = DEFAULT_SERVICE_NAME) -> dict:
    if action not in ("start", "stop", "restart"):
        raise ServiceError(f"unknown action {action!r}")
    if not is_admin():
        raise ServiceError(f"{action} requires an elevated shell.")
    nssm = find_nssm()
    _check([nssm, action, name])
    return {"ok": True, "name": name, "action": action}


@traced("service.status", tracer_name=_TRACER)
def status(*, name: str = DEFAULT_SERVICE_NAME) -> dict:
    """Report service state, the account it runs as, and its config."""
    nssm = find_nssm()
    proc = _run([nssm, "status", name])
    state = (proc.stdout or "").replace("\x00", "").strip()
    if proc.returncode != 0 or not state:
        return {"ok": False, "name": name, "installed": False,
                "error": (proc.stderr or "").replace("\x00", "").strip() or "not installed"}

    def _get(key: str) -> str:
        out = _run([nssm, "get", name, key])
        return (out.stdout or "").replace("\x00", "").strip()

    account = _get("ObjectName")
    return {
        "ok": True,
        "name": name,
        "installed": True,
        "state": state,
        "runs_as": account,
        "account_is_localsystem": account.lower() in ("localsystem", "system"),
        "application": _get("Application"),
        "arguments": _get("AppParameters"),
        "directory": _get("AppDirectory"),
        "start_type": _get("Start"),
        "stdout_log": _get("AppStdout"),
        "stderr_log": _get("AppStderr"),
    }


def tail_logs(*, name: str = DEFAULT_SERVICE_NAME, lines: int = 40) -> dict:
    """Return the last `lines` of the service's stdout/stderr logs."""
    info = status(name=name)
    out: dict[str, list[str]] = {}
    for key in ("stdout_log", "stderr_log"):
        path = Path(info.get(key) or "")
        if not path.name or not path.exists():
            out[key] = []
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            out[key] = [f"<could not read: {e}>"]
            continue
        out[key] = content[-lines:]
    return {"ok": info.get("ok", False), "name": name, **out}
