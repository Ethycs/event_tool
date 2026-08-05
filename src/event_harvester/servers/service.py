"""Run the harvester's long-lived processes as Windows services, via NSSM.

Three things can run as a service, and they are independent:

===========  ====  ===========================================================
`site`       8800  the events website, re-harvesting on a timer
`mcp`        8765  the MCP server over streamable-http — the warm backend
`gateway`    9000  the single MCP endpoint fronting the backend(s)
===========  ====  ===========================================================

`gateway` is the **dispatch endpoint**: one URL, which is the shape a Claude
custom connector takes. It depends on `mcp` and says so via NSSM's
`DependOnService`, so the service manager starts them in order and a reboot
does not race. `site` depends on nothing.

Only the gateway is meant to be exposed. `mcp` stays on loopback and is
reached through the gateway, which is where authentication lives — see
`gateway.py`, which refuses to bind a public address without it.

NSSM is the right tool here because the things being supervised are plain
console processes. NSSM gives them automatic start, restart-on-crash, and log
rotation without the process needing to know it is a service.

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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ServiceSpec:
    """What distinguishes one installable service from another.

    Everything else — logging, rotation, restart throttling, the working
    directory — is identical across all three and lives in
    `build_install_commands`.
    """

    key: str
    default_name: str
    display: str
    default_port: int
    summary: str
    #: Keys of services that must be running first. Becomes NSSM
    #: `DependOnService`, so the service manager orders starts and reboots.
    depends_on: tuple[str, ...] = ()
    #: True when the process is the harvester CLI (`pixi run event-harvester
    #: <verb>`); False when it is a bare pixi task.
    is_cli_verb: bool = True
    #: True when the process reads user-scoped credentials (Discord's cache,
    #: Signal's DB, `token.json`, the browser profile) and so goes *quiet*
    #: rather than failing under LocalSystem. The gateway is a pure proxy and
    #: touches none of them.
    needs_user_account: bool = True
    env: dict[str, str] = field(default_factory=dict)


SERVICES: dict[str, ServiceSpec] = {
    "site": ServiceSpec(
        key="site",
        default_name="EventHarvesterSite",
        display="Event Harvester — events site",
        default_port=8800,
        summary="Serves the events site and re-harvests on a timer.",
    ),
    "mcp": ServiceSpec(
        key="mcp",
        default_name="EventHarvesterMCP",
        display="Event Harvester — MCP backend",
        default_port=8765,
        summary=(
            "The MCP server over streamable-http. Kept warm so the pipeline "
            "process is shared and there is no per-session cold start."
        ),
    ),
    "gateway": ServiceSpec(
        key="gateway",
        default_name="EventHarvesterGateway",
        display="Event Harvester — MCP dispatch gateway",
        default_port=9000,
        summary=(
            "One MCP endpoint fronting the backend(s) — the URL a remote "
            "custom connector points at."
        ),
        depends_on=("mcp",),
        is_cli_verb=False,
        needs_user_account=False,
    ),
}

#: Install/start in this order so `DependOnService` targets already exist.
#: Stopping and uninstalling walk it backwards.
SERVICE_ORDER: tuple[str, ...] = ("mcp", "gateway", "site")


def spec_for(service: str) -> ServiceSpec:
    try:
        return SERVICES[service]
    except KeyError:
        raise ServiceError(
            f"unknown service {service!r}. Expected one of: {', '.join(SERVICE_ORDER)}"
        ) from None


def service_name(service: str, name: str | None = None) -> str:
    """The Windows service name — explicit `name` wins, else the spec default."""
    return name or spec_for(service).default_name


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


def build_app_args(
    spec: ServiceSpec,
    *,
    host: str,
    port: int,
    interval_hours: float = 24.0,
    path: str = "/mcp",
    allow_any_host: bool = False,
) -> list[str]:
    """The arguments handed to pixi for one service. Pure."""
    if spec.key == "site":
        return [
            "run", "event-harvester", "site",
            "--host", str(host),
            "--port", str(port),
            "--interval-hours", str(interval_hours),
        ]
    if spec.key == "mcp":
        args = [
            "run", "event-harvester", "mcp",
            "--transport", "streamable-http",
            "--host", str(host),
            "--port", str(port),
            "--path", path,
        ]
        if allow_any_host:
            args.append("--allow-any-host")
        return args
    if spec.key == "gateway":
        # A pixi task, not a CLI verb — the gateway is configured by
        # environment, so there is nothing to pass on the command line.
        return ["run", "gateway"]
    raise ServiceError(f"no argument builder for service {spec.key!r}")


def build_service_env(
    spec: ServiceSpec,
    *,
    host: str,
    port: int,
    path: str = "/mcp",
    backend_url: str | None = None,
) -> dict[str, str]:
    """Non-secret wiring passed to the service as environment.

    Deliberately excludes anything secret. `AppEnvironmentExtra` is stored in
    the registry in plaintext, so tokens and OAuth client secrets belong in
    `.env` — which the process reads from `AppDirectory` — not here.
    """
    if spec.key != "gateway":
        return dict(spec.env)
    return {
        **spec.env,
        "GATEWAY_HOST": str(host),
        "GATEWAY_PORT": str(port),
        "EH_GATEWAY_PATH": path,
        "EH_MCP_URL": backend_url or f"http://127.0.0.1:{SERVICES['mcp'].default_port}/mcp",
    }


def build_install_commands(
    *,
    service: str = "site",
    name: str | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
    interval_hours: float = 24.0,
    path: str = "/mcp",
    allow_any_host: bool = False,
    backend_url: str | None = None,
    nssm: str = "nssm",
    pixi: str = "pixi",
    repo_root: Path | None = None,
    log_dir: Path | None = None,
    extra_args: list[str] | None = None,
) -> list[list[str]]:
    """Return the ordered nssm invocations that install and configure a service.

    Pure: builds the commands without running them, so the wiring is testable
    without touching the Windows service manager.
    """
    spec = spec_for(service)
    svc_name = service_name(service, name)
    root = Path(repo_root or REPO_ROOT)
    logs = Path(log_dir or LOG_DIR)
    bind_port = spec.default_port if port is None else port

    app_args = build_app_args(
        spec, host=host, port=bind_port, interval_hours=interval_hours,
        path=path, allow_any_host=allow_any_host,
    )
    app_args.extend(extra_args or [])

    if spec.key == "site":
        description = (
            f"Serves the events site on {host}:{bind_port} "
            f"and re-harvests every {interval_hours}h."
        )
    else:
        description = f"{spec.summary} Listens on {host}:{bind_port}{path}."

    commands = [
        [nssm, "install", svc_name, pixi, *app_args],
        # Working directory must be the repo: .env, data/, token.json and the
        # source list are all resolved relative to it.
        [nssm, "set", svc_name, "AppDirectory", str(root)],
        [nssm, "set", svc_name, "DisplayName", spec.display],
        [nssm, "set", svc_name, "Description", description],
        [nssm, "set", svc_name, "Start", "SERVICE_AUTO_START"],
        # Ctrl-C first so uvicorn's lifespan shutdown runs and the refresh
        # tasks are cancelled; kill only if it will not go.
        [nssm, "set", svc_name, "AppStopMethodConsole", "15000"],
        [nssm, "set", svc_name, "AppStdout", str(logs / f"{spec.key}.out.log")],
        [nssm, "set", svc_name, "AppStderr", str(logs / f"{spec.key}.err.log")],
        [nssm, "set", svc_name, "AppRotateFiles", "1"],
        [nssm, "set", svc_name, "AppRotateOnline", "1"],
        [nssm, "set", svc_name, "AppRotateBytes", str(ROTATE_BYTES)],
        # Restart on exit, but back off — a service that crashes on a bad
        # credential would otherwise spin at full speed.
        [nssm, "set", svc_name, "AppExit", "Default", "Restart"],
        [nssm, "set", svc_name, "AppRestartDelay", "30000"],
        [nssm, "set", svc_name, "AppThrottle", "10000"],
    ]

    env = build_service_env(
        spec, host=host, port=bind_port, path=path, backend_url=backend_url,
    )
    if env:
        commands.append(
            [nssm, "set", svc_name, "AppEnvironmentExtra",
             *[f"{k}={v}" for k, v in sorted(env.items())]]
        )

    # Ordering, so a reboot does not race the gateway ahead of its backend.
    if spec.depends_on:
        commands.append(
            [nssm, "set", svc_name, "DependOnService",
             *[service_name(dep) for dep in spec.depends_on]]
        )

    return commands


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
    service: str = "site",
    name: str | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
    interval_hours: float = 24.0,
    path: str = "/mcp",
    allow_any_host: bool = False,
    backend_url: str | None = None,
    extra_args: list[str] | None = None,
) -> dict:
    """Install and configure one service. Does **not** start it.

    Not starting is deliberate: the first start of `site` or `mcp` can run a
    harvest, which opens browsers, contacts every configured source, and
    spends LLM tokens. That is a standing commitment and should begin when
    you say so.
    """
    if not is_admin():
        raise ServiceError("Installing a Windows service requires an elevated shell.")

    spec = spec_for(service)
    svc_name = service_name(service, name)
    bind_port = spec.default_port if port is None else port
    nssm, pixi = find_nssm(), find_pixi()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    commands = build_install_commands(
        service=service, name=svc_name, host=host, port=bind_port,
        interval_hours=interval_hours, path=path, allow_any_host=allow_any_host,
        backend_url=backend_url, nssm=nssm, pixi=pixi, extra_args=extra_args,
    )
    for cmd in commands:
        _check(cmd)

    account = f"{os.environ.get('USERDOMAIN', '.')}\\{getpass.getuser()}"
    set_span_attrs(**{"service.name": svc_name, "service.port": bind_port,
                      "service.key": spec.key})
    logger.info("service: installed %s", svc_name)
    url_path = "" if spec.key == "site" else path
    return {
        "ok": True,
        "service": spec.key,
        "name": svc_name,
        "url": f"http://{host}:{bind_port}{url_path}",
        "logs": str(LOG_DIR),
        "runs_as": "LocalSystem",
        "needs_user_account": spec.needs_user_account,
        "depends_on": [service_name(dep) for dep in spec.depends_on],
        "account_command": f'nssm set {svc_name} ObjectName "{account}" "<your-password>"',
        "started": False,
    }


@traced("service.uninstall", tracer_name=_TRACER)
def uninstall(*, service: str = "site", name: str | None = None) -> dict:
    if not is_admin():
        raise ServiceError("Removing a Windows service requires an elevated shell.")
    svc_name = service_name(service, name)
    nssm = find_nssm()
    _run([nssm, "stop", svc_name])  # may already be stopped; not an error
    _check([nssm, "remove", svc_name, "confirm"])
    logger.info("service: removed %s", svc_name)
    return {"ok": True, "service": service, "name": svc_name, "removed": True}


@traced("service.control", tracer_name=_TRACER)
def control(action: str, *, service: str = "site", name: str | None = None) -> dict:
    if action not in ("start", "stop", "restart"):
        raise ServiceError(f"unknown action {action!r}")
    if not is_admin():
        raise ServiceError(f"{action} requires an elevated shell.")
    svc_name = service_name(service, name)
    nssm = find_nssm()
    _check([nssm, action, svc_name])
    return {"ok": True, "service": service, "name": svc_name, "action": action}


@traced("service.status", tracer_name=_TRACER)
def status(*, service: str = "site", name: str | None = None) -> dict:
    """Report service state, the account it runs as, and its config."""
    name = service_name(service, name)
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
        "service": service,
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


def tail_logs(*, service: str = "site", name: str | None = None, lines: int = 40) -> dict:
    """Return the last `lines` of the service's stdout/stderr logs."""
    name = service_name(service, name)
    info = status(service=service, name=name)
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
    return {"ok": info.get("ok", False), "service": service, "name": name, **out}
