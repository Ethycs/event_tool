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
import sys
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
    #: True when the process launches Playwright. Those browsers live under
    #: the installing user's `%LOCALAPPDATA%`, which LocalSystem does not
    #: resolve to — see `default_browsers_path`.
    runs_browser: bool = True
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
        runs_browser=False,
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


def default_browsers_path() -> str | None:
    """Where Playwright's browsers actually live, for the installing user.

    **This is the fix for a silent, total failure of the web path.** Playwright
    installs its browsers under `%LOCALAPPDATA%\\ms-playwright`. Under
    LocalSystem that resolves to
    `C:\\WINDOWS\\system32\\config\\systemprofile\\AppData\\Local\\`, where
    nothing is installed — so **every** standard web source died at
    `launch_persistent_context` with "Executable doesn't exist", logged once
    as a single `web source failed` line.

    Only `erobay` survived, because `native_chrome: true` launches the
    machine-wide Google Chrome instead. Measured 2026-08-06: 1 of 31 sources
    producing, for five consecutive nightly runs, with no error anyone noticed.

    Pointing `PLAYWRIGHT_BROWSERS_PATH` at the user's install fixes it without
    a second 650 MB download and without needing the account password —
    `NT AUTHORITY\\SYSTEM` already has FullControl there.
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    candidate = Path(base) / "ms-playwright"
    return str(candidate) if candidate.is_dir() else None


def build_service_env(
    spec: ServiceSpec,
    *,
    host: str,
    port: int,
    path: str = "/mcp",
    backend_url: str | None = None,
    browsers_path: str | None = None,
) -> dict[str, str]:
    """Non-secret wiring passed to the service as environment.

    Deliberately excludes anything secret. `AppEnvironmentExtra` is stored in
    the registry in plaintext, so tokens and OAuth client secrets belong in
    `.env` — which the process reads from `AppDirectory` — not here. A
    filesystem path is not a secret.
    """
    env = dict(spec.env)

    if spec.runs_browser:
        resolved = browsers_path or default_browsers_path()
        if resolved:
            env["PLAYWRIGHT_BROWSERS_PATH"] = resolved
        # The conda-forge playwright finds its Node driver through
        # CONDA_PREFIX, which only exists once the env is activated. A service
        # that launches `event-harvester.exe` directly instead of through
        # `pixi run` never activates, and every web source then dies with
        # "Playwright driver executable not found" — a different failure from
        # the missing-browsers one above, and it appears only after you fix
        # that. Setting it explicitly makes the service work either way.
        env.setdefault("CONDA_PREFIX", sys.prefix)

    if spec.key == "gateway":
        env.update({
            "GATEWAY_HOST": str(host),
            "GATEWAY_PORT": str(port),
            "EH_GATEWAY_PATH": path,
            "EH_MCP_URL": backend_url
            or f"http://127.0.0.1:{SERVICES['mcp'].default_port}/mcp",
        })
    return env


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


# ── unmanaged processes ───────────────────────────────────────────────────
#
# The failure this exists for: a harvester started by hand on 2026-07-28 was
# still running on 2026-08-05, on `0.0.0.0:8765` with no authentication and no
# job runner, owned by nothing. `service stop --all` would never have touched
# it, because NSSM does not know it exists. Teardown that only stops what it
# installed is not teardown.

#: Command-line fragments that identify one of our own long-lived processes.
_HARVESTER_MARKERS = ("event-harvester", "event_harvester", "gateway.py")

#: Sub-commands worth reporting. A one-shot `harvest` exits on its own.
_LONG_LIVED = ("mcp", "site", "serve", "gateway")

#: Verbs that bind a port and keep running — the things a service should own.
_SERVER_VERBS = ("site", "serve", "gateway")


def _process_table() -> dict[int, dict]:
    """Every process, keyed by PID, with its parent and command line.

    One CIM call rather than psutil (not a dependency) and rather than one
    call per service. Returns {} rather than raising — this is diagnostics and
    must never be the reason a teardown aborts.
    """
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    proc = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        logger.debug("service: could not enumerate processes")
        return {}

    import json

    try:
        raw = json.loads(proc.stdout)
    except ValueError:
        return {}
    if isinstance(raw, dict):
        raw = [raw]

    table: dict[int, dict] = {}
    for entry in raw:
        try:
            pid = int(entry.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        table[pid] = {
            "pid": pid,
            "ppid": int(entry.get("ParentProcessId") or 0),
            "name": entry.get("Name") or "",
            "command": (entry.get("CommandLine") or "").strip(),
        }
    return table


def _looks_like_a_harvester(command: str) -> bool:
    """A long-lived harvester process, not a one-shot and not our own query.

    Matches verbs as whole tokens. Substring matching put `site` inside every
    path containing the word and reported a dozen false positives.
    """
    low = command.lower()
    if not any(marker in low for marker in _HARVESTER_MARKERS):
        return False
    if "get-ciminstance" in low or "converto-json" in low:
        return False
    tokens = low.replace('"', " ").replace("'", " ").split()
    return any(verb in tokens for verb in _LONG_LIVED)


def _ancestors(pid: int, table: dict[int, dict], limit: int = 12) -> list[int]:
    """Walk up the parent chain, defensively — PID reuse can make cycles."""
    chain, seen, current = [], {pid}, table.get(pid, {}).get("ppid", 0)
    while current and current not in seen and len(chain) < limit:
        chain.append(current)
        seen.add(current)
        current = table.get(current, {}).get("ppid", 0)
    return chain


def is_stdio_mcp(command: str) -> bool:
    """A client-spawned stdio MCP server, not a service.

    An editor or desktop client starts one of these per session and it dies
    with the client. It binds no port and owns no queue, so it is not a stray
    service — but it *does* bypass the job queue (`harvest_events` runs inline
    over stdio), so it is worth counting rather than hiding.
    """
    low = command.lower()
    tokens = low.replace('"', " ").split()
    return "mcp" in tokens and "streamable-http" not in low


def _is_server(command: str) -> bool:
    """Binds a port and stays up — the shape a service should own.

    `gateway.py` is matched as well as `gateway`, because the gateway runs as
    `python gateway.py` and `pixi run gateway`. Matching only the bare token
    made an unmanaged gateway invisible to this check.
    """
    low = command.lower()
    tokens = low.replace('"', " ").replace("\\", "/").split()
    tokens = {t.rsplit("/", 1)[-1] for t in tokens}
    if any(verb in tokens for verb in _SERVER_VERBS):
        return True
    if "gateway.py" in tokens:
        return True
    return "mcp" in tokens and "streamable-http" in low


def _leaves_only(procs: list[dict]) -> list[dict]:
    """Collapse `pixi -> .exe -> python` chains to the process doing the work.

    Without this one server is reported three times, which is how a first run
    produced twelve findings for three actual processes.
    """
    parents = {p["ppid"] for p in procs}
    return [p for p in procs if p["pid"] not in parents]


def find_harvester_processes(table: dict[int, dict] | None = None) -> list[dict]:
    """Every running long-lived harvester process, one entry per chain."""
    table = table if table is not None else _process_table()
    matched = [p for p in table.values() if _looks_like_a_harvester(p["command"])]
    return _leaves_only(matched)


def _service_pids(names: list[str]) -> dict[str, int]:
    """The PID NSSM reports for each service — the `nssm.exe` supervisor."""
    script = "; ".join(
        f"(Get-CimInstance Win32_Service -Filter \"Name='{n}'\").ProcessId" for n in names
    )
    proc = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    values = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    out: dict[str, int] = {}
    for name, value in zip(names, values):
        if value.isdigit() and int(value):
            out[name] = int(value)
    return out


def survey_processes(*, names: list[str] | None = None) -> dict[str, list[dict]]:
    """Split running harvester processes into unmanaged servers and stdio clients.

    Returns both, rather than returning one and stashing the other on a
    function attribute — that side-channel pattern is hazard #1 in
    `03 - Architecture/00 - System Overview.md` and was deliberately removed
    from this codebase once already.

    **Ancestry, not PID equality.** NSSM's service PID is `nssm.exe`; the
    actual worker is a grandchild (`nssm` → `pixi` → `python`). Comparing PIDs
    directly reported every managed worker as unmanaged — a dozen false
    positives on the first real run, which is worse than no check at all,
    because a diagnostic that always cries wolf gets ignored.

    Our own process and its ancestors are excluded too: running the check must
    not report the check.
    """
    names = names or [spec.default_name for spec in SERVICES.values()]
    table = _process_table()
    if not table:
        return {"unmanaged": [], "stdio": []}

    service_pids = set(_service_pids(names).values())
    self_chain = {os.getpid(), *_ancestors(os.getpid(), table)}

    unmanaged, stdio = [], []
    for proc in find_harvester_processes(table):
        if proc["pid"] in self_chain:
            continue
        if service_pids & set(_ancestors(proc["pid"], table)):
            continue          # descends from a service supervisor
        entry = {"pid": proc["pid"], "command": proc["command"]}
        if _is_server(proc["command"]):
            unmanaged.append(entry)
        elif is_stdio_mcp(proc["command"]):
            stdio.append(entry)
    return {"unmanaged": unmanaged, "stdio": stdio}


def find_unmanaged(*, names: list[str] | None = None) -> list[dict]:
    """Unmanaged long-lived *servers* only. See `survey_processes`."""
    return survey_processes(names=names)["unmanaged"]


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


# ── verified transitions ──────────────────────────────────────────────────
#
# `start`/`stop` ask the service manager to do something. `up`/`down` check
# that it happened. The difference matters because every lifecycle failure
# this project has hit was a thing that *looked* done: a service running
# four-day-old code, a hand-started process nothing owned, a queued job
# nobody knew was armed.

#: Health endpoint per service, relative to its bind address.
HEALTH_PATHS = {"site": "/healthz", "gateway": "/healthz", "mcp": None}


def _http_get_json(url: str, timeout: float = 3.0) -> dict | None:
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def health(service: str, *, host: str = "127.0.0.1", port: int | None = None) -> dict:
    """Ask a running service what it is. Never raises."""
    spec = spec_for(service)
    path = HEALTH_PATHS.get(service)
    if not path:
        return {"service": service, "reachable": None,
                "detail": "no health endpoint (stdio/JSON-RPC only)"}

    payload = _http_get_json(f"http://{host}:{port or spec.default_port}{path}")
    if payload is None:
        return {"service": service, "reachable": False}
    build = payload.get("build") or {}
    return {
        "service": service,
        "reachable": True,
        "ok": payload.get("ok"),
        "version": build.get("version"),
        "sha": build.get("sha"),
        "stale": build.get("stale"),
        "pid": build.get("pid"),
        "started_at": build.get("started_at"),
        "queue": payload.get("queue"),
    }


@traced("service.down", tracer_name=_TRACER)
def down(*, names: list[str] | None = None) -> dict:
    """Stop every service in reverse dependency order, then verify.

    Verification is the point. It reports processes that survived — the ones
    NSSM never owned and `stop --all` would silently leave running.
    """
    if not is_admin():
        raise ServiceError("Stopping services requires an elevated shell.")

    stopped, failed = [], []
    for key in reversed(SERVICE_ORDER):
        name = service_name(key)
        info = status(service=key, name=name)
        if not info.get("installed"):
            continue
        try:
            control("stop", service=key, name=name)
            stopped.append(name)
        except ServiceError as e:
            failed.append({"name": name, "error": str(e)})

    survivors = find_unmanaged(names=names)
    return {
        "ok": not failed,
        "stopped": stopped,
        "failed": failed,
        "unmanaged_survivors": survivors,
    }


@traced("service.up", tracer_name=_TRACER)
def up(*, host: str = "127.0.0.1") -> dict:
    """Start every installed service in dependency order, then verify.

    Reports the queue depth **before** anything drains it. An orphaned job can
    sit queued for days and then run a full unattended harvest the moment a
    runner appears; that should be something you are told, not something you
    discover from the token bill.
    """
    if not is_admin():
        raise ServiceError("Starting services requires an elevated shell.")

    from event_harvester import jobs

    queue_before = jobs.queue_summary()

    started, failed = [], []
    for key in SERVICE_ORDER:
        name = service_name(key)
        info = status(service=key, name=name)
        if not info.get("installed"):
            continue
        if "RUNNING" in (info.get("state") or "").upper():
            started.append(name)
            continue
        try:
            control("start", service=key, name=name)
            started.append(name)
        except ServiceError as e:
            failed.append({"name": name, "error": str(e)})

    checks = [health(key, host=host) for key in SERVICE_ORDER]
    return {
        "ok": not failed,
        "started": started,
        "failed": failed,
        "health": checks,
        "queue_before": queue_before,
    }


@traced("service.doctor", tracer_name=_TRACER)
def doctor(*, host: str = "127.0.0.1") -> dict:
    """One command for "what is actually running, and is it current?".

    Replaces the process enumeration, start-time comparison and log reading
    that establishing this used to take.
    """
    from event_harvester import jobs
    from event_harvester.buildinfo import current_sha

    services = []
    for key in SERVICE_ORDER:
        info = status(service=key)
        entry = {
            "service": key,
            "name": info.get("name"),
            "installed": info.get("installed", False),
            "state": info.get("state"),
            "runs_as": info.get("runs_as"),
            "needs_user_account": spec_for(key).needs_user_account,
        }
        if entry["installed"]:
            entry["health"] = health(key, host=host)
        services.append(entry)

    findings = []
    for entry in services:
        health_info = entry.get("health") or {}
        if health_info.get("stale"):
            findings.append(
                f"{entry['name']} is running {health_info.get('sha')} but "
                f"{current_sha() or '?'} is checked out — restart it"
            )
        elif health_info.get("reachable") and not health_info.get("sha"):
            # Absence of identity *is* the staleness signal: build info was
            # added 2026-08-06, so a process that cannot report it booted
            # before that and is by definition behind.
            findings.append(
                f"{entry['name']} reports no build identity — it predates "
                f"buildinfo (2026-08-06), so it is running stale code"
            )
        if entry["installed"] and "RUNNING" in (entry.get("state") or "").upper() \
                and health_info.get("reachable") is False:
            findings.append(f"{entry['name']} is RUNNING but not answering health")
        if entry["needs_user_account"] and (entry.get("runs_as") or "").lower() in (
            "localsystem", "system",
        ):
            findings.append(
                f"{entry['name']} runs as LocalSystem — Discord and Signal "
                f"will be invisible"
            )

    # A service that cannot see Playwright's browsers loses every standard web
    # source at launch, and says so exactly once per run in a line nobody
    # reads. Five nightly runs went by with 1 of 31 sources producing.
    for entry in services:
        if not entry["installed"] or not spec_for(entry["service"]).runs_browser:
            continue
        configured = _run(
            [find_nssm(), "get", entry["name"], "AppEnvironmentExtra"]
        ).stdout or ""
        if "PLAYWRIGHT_BROWSERS_PATH" in configured.replace("\x00", ""):
            continue
        if (entry.get("runs_as") or "").lower() in ("localsystem", "system"):
            findings.append(
                f"{entry['name']} has no PLAYWRIGHT_BROWSERS_PATH and runs as "
                f"LocalSystem — every non-native-chrome web source will fail "
                f"at browser launch"
            )

    survey = survey_processes()
    unmanaged = survey["unmanaged"]
    for proc in unmanaged:
        findings.append(
            f"pid {proc['pid']} is an unmanaged server no service owns: "
            f"{proc['command'][:70]}"
        )

    queue = jobs.queue_summary()
    if queue.get("queued"):
        findings.append(
            f"{queue['queued']} job(s) queued; they run as soon as a runner starts"
        )
    if queue.get("queued") and not any(
        e["service"] == "mcp" and "RUNNING" in (e.get("state") or "").upper()
        for e in services
    ):
        findings.append("nothing is draining the queue — EventHarvesterMCP is not running")

    return {
        "ok": not findings,
        "checked_out_sha": (current_sha() or "")[:12] or None,
        "services": services,
        "unmanaged": unmanaged,
        # Informational, not a finding: a client spawns one per session and it
        # dies with the client. Counted because stdio bypasses the job queue.
        "stdio_clients": survey["stdio"],
        "queue": queue,
        "findings": findings,
    }
