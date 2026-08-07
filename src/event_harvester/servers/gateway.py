"""The dispatch endpoint — one MCP URL fronting every backend.

`mcp_server.py` is the harvester's own MCP server. This module is the thing in
front of it: a FastMCP proxy that fronts one or more backends behind a single
URL, so a client registers one endpoint instead of one per backend. Locally
that saves editors some configuration. Remotely it is the whole point — a
Claude *custom connector* takes exactly one URL, so whatever is reachable at
that URL is the entire surface Claude gets.

Which makes the exposure decision the important part of this file.

**The tools behind this endpoint are not read-only.** `harvest_events` opens
browsers and spends LLM tokens, `approve_event` writes to TickTick, `chat` and
the `grok_*` family drive a logged-in browser session. On loopback that is
fine — the only caller is you. On a public URL, an unauthenticated endpoint
hands all of it to anyone who finds the hostname. So `check_exposure` refuses
to bind a non-loopback address with `auth=none`, and the refusal is the
default rather than a warning, because a warning scrolls past.

Auth modes (`EH_GATEWAY_AUTH`)
------------------------------
``none``    No auth. Loopback only.
``token``   A static bearer token (`EH_GATEWAY_TOKEN`). Good for your own
            clients over a tunnel; **Claude's connector UI cannot use it**,
            because that flow is OAuth.
``jwt``     Verify JWTs you mint elsewhere (HMAC secret or a JWKS URI).
``github`` / ``google`` / any provider module under
            ``fastmcp.server.auth.providers`` that takes the standard
            client_id/client_secret/base_url triple. **This is the mode a
            Claude custom connector wants** — FastMCP acts as an OAuth proxy
            to the identity provider, and Claude does the normal OAuth dance.

Reachability is not this file's job. Claude connects *inbound* from
Anthropic's IP ranges, so the endpoint needs a public HTTPS URL — a tunnel
(`cloudflared`, `ngrok`, Tailscale Funnel) or a real reverse proxy. See
`docs/02 - Implementation/02 - Deploy Playbook.md` §5c.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any

logger = logging.getLogger("event_harvester.gateway")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000
DEFAULT_PATH = "/mcp"
DEFAULT_BACKEND = "http://127.0.0.1:8765/mcp"

#: Hosts where "no authentication" is a defensible default because nothing
#: off-box can reach the socket. ``0.0.0.0`` is deliberately **not** here.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "127.0.0.1:0"})

#: Modes that leave the endpoint open to anyone who can reach the port.
OPEN_MODES = frozenset({"none"})

#: Providers wired by name. Each takes the same client_id/client_secret/
#: base_url triple; anything else in `fastmcp.server.auth.providers` that
#: follows that shape works too, it just has not been exercised here.
KNOWN_OAUTH_PROVIDERS = {
    "github": ("github", "GitHubProvider"),
    "google": ("google", "GoogleProvider"),
    "auth0": ("auth0", "Auth0Provider"),
    "workos": ("workos", "WorkOSProvider"),
    "descope": ("descope", "DescopeProvider"),
    "clerk": ("clerk", "ClerkProvider"),
    "azure": ("azure", "AzureProvider"),
    "discord": ("discord", "DiscordProvider"),
}


class GatewayError(RuntimeError):
    """Configuration is wrong in a way that must stop the process."""


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ) if env is None else env


def is_loopback(host: str) -> bool:
    """True when only this machine can reach a socket bound to `host`."""
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def backends_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Build the MCPConfig `mcpServers` mapping the proxy is constructed from.

    One backend today. The shape is the standard MCPConfig one, so adding a
    second is a dict entry and its tools surface behind the same endpoint.

    `verified` — with a single backend FastMCP 3.4.5 proxies tool names
    through **unprefixed** (`harvest_events`, not
    `event-harvester_harvest_events`). Do not rely on a prefix when adding a
    second backend without re-checking; that is where a name collision would
    show up.
    """
    e = _env(env)
    return {
        "mcpServers": {
            "event-harvester": {"url": e.get("EH_MCP_URL", DEFAULT_BACKEND)},
        }
    }


def auth_mode(env: dict[str, str] | None = None) -> str:
    return (_env(env).get("EH_GATEWAY_AUTH") or "none").strip().lower()


def check_exposure(
    host: str,
    mode: str,
    env: dict[str, str] | None = None,
) -> None:
    """Refuse to serve an unauthenticated endpoint off-box.

    Raises rather than warns. The tools behind this gateway harvest, spend
    tokens, write to TickTick and drive a logged-in browser; a warning in a
    service log nobody reads is not a control.
    """
    if mode not in OPEN_MODES or is_loopback(host):
        return
    if (_env(env).get("EH_GATEWAY_ALLOW_INSECURE") or "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "gateway: binding %s with no authentication because "
            "EH_GATEWAY_ALLOW_INSECURE is set. Every caller gets harvest, "
            "approve, chat and the grok tools.",
            host,
        )
        return
    raise GatewayError(
        f"refusing to bind {host} with EH_GATEWAY_AUTH=none.\n"
        "  This endpoint exposes tools that harvest, spend LLM tokens, write to\n"
        "  TickTick and drive a logged-in browser session.\n"
        "  Set EH_GATEWAY_AUTH to one of: token, jwt, "
        f"{', '.join(sorted(KNOWN_OAUTH_PROVIDERS))}\n"
        "  (a Claude custom connector needs an OAuth provider, not token/jwt),\n"
        "  or bind 127.0.0.1 and put a tunnel in front of it."
    )


def _require(e: dict[str, str], key: str, why: str) -> str:
    value = (e.get(key) or "").strip()
    if not value:
        raise GatewayError(f"{key} is required {why}.")
    return value


def build_auth(env: dict[str, str] | None = None) -> Any | None:
    """Construct the FastMCP auth provider named by `EH_GATEWAY_AUTH`.

    Returns None for ``none``. Import of `fastmcp` is deferred so the module
    (and its tests) load without the optional dependency present.
    """
    e = _env(env)
    mode = auth_mode(e)

    if mode in OPEN_MODES:
        return None

    if mode == "token":
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        token = _require(e, "EH_GATEWAY_TOKEN", "when EH_GATEWAY_AUTH=token")
        return StaticTokenVerifier(
            tokens={token: {"client_id": "event-harvester", "scopes": ["mcp"]}},
        )

    if mode == "jwt":
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        secret = (e.get("EH_GATEWAY_JWT_SECRET") or "").strip()
        jwks = (e.get("EH_GATEWAY_JWKS_URI") or "").strip()
        if not secret and not jwks:
            raise GatewayError(
                "EH_GATEWAY_AUTH=jwt needs either EH_GATEWAY_JWT_SECRET "
                "(HMAC) or EH_GATEWAY_JWKS_URI (asymmetric)."
            )
        return JWTVerifier(
            public_key=secret or None,
            jwks_uri=jwks or None,
            algorithm="HS256" if secret else None,
            issuer=(e.get("EH_GATEWAY_JWT_ISSUER") or "").strip() or None,
            audience=(e.get("EH_GATEWAY_JWT_AUDIENCE") or "").strip() or None,
        )

    if mode in KNOWN_OAUTH_PROVIDERS:
        module_name, class_name = KNOWN_OAUTH_PROVIDERS[mode]
        module = importlib.import_module(f"fastmcp.server.auth.providers.{module_name}")
        provider = getattr(module, class_name)
        kwargs: dict[str, Any] = {
            "client_id": _require(e, "EH_GATEWAY_CLIENT_ID", f"when EH_GATEWAY_AUTH={mode}"),
            "client_secret": _require(
                e, "EH_GATEWAY_CLIENT_SECRET", f"when EH_GATEWAY_AUTH={mode}"
            ),
            # The provider mints redirect URIs from this, so it must be the
            # URL Claude actually reaches — the tunnel hostname, not the bind
            # address.
            "base_url": _require(
                e,
                "EH_GATEWAY_PUBLIC_URL",
                f"when EH_GATEWAY_AUTH={mode} — it is the public https:// URL "
                "Claude connects to, and OAuth redirect URIs are built from it",
            ),
        }
        scopes = (e.get("EH_GATEWAY_SCOPES") or "").strip()
        if scopes:
            kwargs["required_scopes"] = [s.strip() for s in scopes.split(",") if s.strip()]
        return provider(**kwargs)

    raise GatewayError(
        f"unknown EH_GATEWAY_AUTH={mode!r}. "
        f"Expected none, token, jwt, or one of: {', '.join(sorted(KNOWN_OAUTH_PROVIDERS))}"
    )


def build_gateway(env: dict[str, str] | None = None, *, name: str = "gateway") -> Any:
    """Create the proxy server. Does not bind a socket."""
    from fastmcp.server import create_proxy

    e = _env(env)
    auth = build_auth(e)
    gateway = create_proxy(backends_from_env(e), name=name, auth=auth)

    # A liveness probe the service manager and the tunnel can both use without
    # speaking MCP. Deliberately says nothing about backends or auth — it is
    # reachable by anyone who can reach the port, including before auth.
    @gateway.custom_route("/healthz", methods=["GET"])
    async def _healthz(_request):  # pragma: no cover - exercised by run
        from starlette.responses import JSONResponse

        from event_harvester.buildinfo import build_info

        # Says nothing about backends or auth — it is reachable by anyone who
        # can reach the port, including before auth. Build identity is not
        # sensitive and is the whole reason this endpoint is useful.
        return JSONResponse({
            "ok": True,
            "service": "event-harvester-gateway",
            "build": build_info(),
        })

    return gateway


def resolve_bind(env: dict[str, str] | None = None) -> tuple[str, int, str]:
    e = _env(env)
    host = e.get("GATEWAY_HOST", DEFAULT_HOST)
    port = int(e.get("GATEWAY_PORT", str(DEFAULT_PORT)))
    path = e.get("EH_GATEWAY_PATH", DEFAULT_PATH)
    return host, port, path


def load_env_file() -> None:
    """Read `.env` from the working directory, without clobbering the real env.

    The service manager supplies the non-secret wiring through
    `AppEnvironmentExtra`, and `.env` supplies the secrets. `override=False`
    is what makes that split work: what the service manager set wins.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a hard dep in practice
        return
    load_dotenv(override=False)


def make_app(env: dict[str, str] | None = None):
    """ASGI factory — the entry point uvicorn's reloader imports per restart.

    The exposure guard runs here, not only in `run_gateway`, because under
    `--reload` the worker is what actually binds and `run_gateway` is never
    called in the child.
    """
    if env is None:
        load_env_file()
    e = _env(env)
    host, _, path = resolve_bind(e)
    check_exposure(host, auth_mode(e), e)
    return build_gateway(e).http_app(path=path)


def run_gateway_reload(env: dict[str, str] | None = None) -> int:
    """Serve with uvicorn's auto-reloader, in a subprocess.

    Same reasoning as the site's reload path: uvicorn implements reload by
    supervising *child* processes, so handing the whole thing to `python -m
    uvicorn` keeps the real reloader rather than reimplementing it.

    The guard still applies — it runs in `make_app`, which each worker calls.
    """
    import subprocess
    import sys
    from pathlib import Path

    if env is None:
        load_env_file()
    e = _env(env)
    host, port, _ = resolve_bind(e)
    # Fail in the parent too, so a bad config is one clear message rather than
    # a reload loop of tracebacks from restarting children.
    check_exposure(host, auth_mode(e), e)

    src = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable, "-m", "uvicorn",
        "event_harvester.servers.gateway:make_app", "--factory",
        "--reload", "--reload-dir", str(src),
        "--host", host, "--port", str(port),
        "--log-level", "warning",
    ]
    logger.info("gateway: reload mode — http://%s:%d", host, port)
    try:
        return subprocess.call(cmd, env=dict(e))
    except KeyboardInterrupt:
        return 130


def run_gateway(env: dict[str, str] | None = None, *, reload: bool = False) -> int:
    """Serve the gateway. Blocks."""
    if env is None:
        load_env_file()
        env = dict(os.environ)
    if reload:
        return run_gateway_reload(env)

    e = _env(env)
    host, port, path = resolve_bind(e)
    mode = auth_mode(e)

    check_exposure(host, mode, e)

    gateway = build_gateway(e)
    public = (e.get("EH_GATEWAY_PUBLIC_URL") or "").strip()
    logger.info(
        "gateway: serving http://%s:%s%s (auth=%s)%s",
        host, port, path, mode,
        f" public={public}" if public else "",
    )
    gateway.run(transport="http", host=host, port=port, path=path)
    return 0
