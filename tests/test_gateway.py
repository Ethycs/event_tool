"""Tests for the dispatch gateway — the one endpoint a remote connector sees.

The important tests here are the exposure guard and the HTTP-level auth check.
The guard is the only thing standing between a tunnel and an unauthenticated
`harvest_events`, and an auth provider that is silently dropped on the floor
would look exactly like one that works — so it is checked over real HTTP
through the app the server actually serves, not by asserting on the object we
just constructed.
"""

from __future__ import annotations

import pytest

from event_harvester.servers import gateway as gw


def _test_client():
    """Starlette's TestClient warns about httpx *on import*, before any
    `pytestmark` filter can apply, and `StarletteDeprecationWarning` does not
    subclass `DeprecationWarning` — so the filter has to be unqualified and
    scoped to this one import. Same seam as `tests/test_site_server.py`.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

    return TestClient


class TestBackendConfig:
    def test_the_default_backend_is_the_mcp_service(self):
        cfg = gw.backends_from_env({})
        assert cfg["mcpServers"]["event-harvester"]["url"] == gw.DEFAULT_BACKEND

    def test_the_backend_url_is_overridable(self):
        cfg = gw.backends_from_env({"EH_MCP_URL": "http://10.0.0.5:1234/mcp"})
        assert cfg["mcpServers"]["event-harvester"]["url"] == "http://10.0.0.5:1234/mcp"

    def test_bind_defaults_are_loopback(self):
        host, port, path = gw.resolve_bind({})
        assert host == "127.0.0.1"
        assert port == gw.DEFAULT_PORT
        assert path == "/mcp"

    def test_bind_is_overridable(self):
        host, port, path = gw.resolve_bind(
            {"GATEWAY_HOST": "0.0.0.0", "GATEWAY_PORT": "9999", "EH_GATEWAY_PATH": "/x"}
        )
        assert (host, port, path) == ("0.0.0.0", 9999, "/x")


class TestExposureGuard:
    """The tools behind this endpoint harvest, spend tokens, and write to TickTick."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_without_auth_is_allowed(self, host):
        gw.check_exposure(host, "none", {})

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "smallblackbox.local"])
    def test_a_public_bind_without_auth_is_refused(self, host):
        with pytest.raises(gw.GatewayError, match="refusing to bind"):
            gw.check_exposure(host, "none", {})

    def test_the_refusal_names_the_ways_out(self):
        with pytest.raises(gw.GatewayError) as e:
            gw.check_exposure("0.0.0.0", "none", {})
        message = str(e.value)
        assert "EH_GATEWAY_AUTH" in message
        assert "github" in message, "the OAuth path must be named — it is the Claude one"
        assert "127.0.0.1" in message, "binding loopback behind a tunnel is the other way"

    @pytest.mark.parametrize("mode", ["token", "jwt", "github", "google"])
    def test_a_public_bind_with_auth_is_allowed(self, mode):
        gw.check_exposure("0.0.0.0", mode, {})

    def test_the_escape_hatch_must_be_explicit(self):
        gw.check_exposure("0.0.0.0", "none", {"EH_GATEWAY_ALLOW_INSECURE": "1"})

    def test_the_escape_hatch_is_not_on_by_accident(self):
        """An empty or nonsense value must not read as consent."""
        for value in ("", "0", "false", "no", "maybe"):
            with pytest.raises(gw.GatewayError):
                gw.check_exposure("0.0.0.0", "none", {"EH_GATEWAY_ALLOW_INSECURE": value})

    def test_0_0_0_0_is_not_loopback(self):
        """It is the wildcard bind — every interface, which is the whole problem."""
        assert gw.is_loopback("0.0.0.0") is False


class TestAuthConstruction:
    def test_no_auth_by_default(self):
        assert gw.auth_mode({}) == "none"
        assert gw.build_auth({}) is None

    def test_an_unknown_mode_is_refused_by_name(self):
        with pytest.raises(gw.GatewayError, match="unknown EH_GATEWAY_AUTH"):
            gw.build_auth({"EH_GATEWAY_AUTH": "wishful"})

    def test_token_mode_needs_a_token(self):
        with pytest.raises(gw.GatewayError, match="EH_GATEWAY_TOKEN is required"):
            gw.build_auth({"EH_GATEWAY_AUTH": "token"})

    def test_token_mode_builds_a_verifier(self):
        auth = gw.build_auth({"EH_GATEWAY_AUTH": "token", "EH_GATEWAY_TOKEN": "s3cret"})
        assert auth is not None

    def test_jwt_mode_needs_a_key_of_some_kind(self):
        with pytest.raises(gw.GatewayError, match="EH_GATEWAY_JWT_SECRET"):
            gw.build_auth({"EH_GATEWAY_AUTH": "jwt"})

    def test_jwt_hmac_builds(self):
        auth = gw.build_auth(
            {"EH_GATEWAY_AUTH": "jwt", "EH_GATEWAY_JWT_SECRET": "x" * 32}
        )
        assert auth is not None

    def test_an_oauth_provider_needs_client_credentials(self):
        with pytest.raises(gw.GatewayError, match="EH_GATEWAY_CLIENT_ID"):
            gw.build_auth({"EH_GATEWAY_AUTH": "github"})

    def test_an_oauth_provider_needs_the_public_url(self):
        """Redirect URIs are minted from it, so the bind address will not do."""
        with pytest.raises(gw.GatewayError, match="EH_GATEWAY_PUBLIC_URL"):
            gw.build_auth({
                "EH_GATEWAY_AUTH": "github",
                "EH_GATEWAY_CLIENT_ID": "Ov23li",
                "EH_GATEWAY_CLIENT_SECRET": "abc",
            })

    def test_an_oauth_provider_builds_with_all_three(self):
        auth = gw.build_auth({
            "EH_GATEWAY_AUTH": "github",
            "EH_GATEWAY_CLIENT_ID": "Ov23li",
            "EH_GATEWAY_CLIENT_SECRET": "abc",
            "EH_GATEWAY_PUBLIC_URL": "https://events.example.com",
        })
        assert auth is not None


class TestAuthIsActuallyEnforced:
    """Over HTTP, through the app the server serves.

    `create_proxy` takes `**settings`, so an `auth=` that FastMCP did not
    recognise would be swallowed without complaint and every construction
    test above would still pass.
    """

    @staticmethod
    def _client(env):
        return _test_client()(gw.build_gateway(env).http_app(path="/mcp"))

    def test_an_unauthenticated_request_is_rejected(self):
        env = {"EH_GATEWAY_AUTH": "token", "EH_GATEWAY_TOKEN": "s3cret"}
        with self._client(env) as client:
            r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert r.status_code == 401

    def test_a_wrong_token_is_rejected(self):
        env = {"EH_GATEWAY_AUTH": "token", "EH_GATEWAY_TOKEN": "s3cret"}
        with self._client(env) as client:
            r = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Authorization": "Bearer wrong"},
            )
        assert r.status_code == 401

    def test_the_right_token_gets_past_auth(self):
        """Past auth, not necessarily to a working call — there is no backend here."""
        env = {"EH_GATEWAY_AUTH": "token", "EH_GATEWAY_TOKEN": "s3cret"}
        with self._client(env) as client:
            r = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={
                    "Authorization": "Bearer s3cret",
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert r.status_code != 401

    def test_without_auth_configured_nothing_is_challenged(self):
        with self._client({}) as client:
            r = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert r.status_code != 401


class TestHealthz:
    def test_healthz_answers(self):
        with _test_client()(gw.build_gateway({}).http_app(path="/mcp")) as client:
            r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestRunGateway:
    def test_it_refuses_before_binding_a_socket(self, monkeypatch):
        """The guard must run before anything listens, not after."""
        monkeypatch.setattr(
            gw, "build_gateway",
            lambda *a, **k: pytest.fail("built a server despite the exposure guard"),
        )
        with pytest.raises(gw.GatewayError):
            gw.run_gateway({"GATEWAY_HOST": "0.0.0.0"})

    def test_it_serves_on_the_resolved_bind(self, monkeypatch):
        served = {}

        class FakeGateway:
            def run(self, **kwargs):
                served.update(kwargs)

        monkeypatch.setattr(gw, "build_gateway", lambda *a, **k: FakeGateway())
        rc = gw.run_gateway({"GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": "9100"})
        assert rc == 0
        assert served["host"] == "127.0.0.1"
        assert served["port"] == 9100
        assert served["path"] == "/mcp"
        assert served["transport"] == "http"


class TestHotReload:
    """`--reload` must not become a hole in the exposure guard."""

    def test_the_factory_builds_an_asgi_app(self):
        app = gw.make_app({})
        assert callable(app)

    def test_the_factory_enforces_the_guard(self):
        """Under --reload the worker binds, so the guard has to live here too."""
        with pytest.raises(gw.GatewayError, match="refusing to bind"):
            gw.make_app({"GATEWAY_HOST": "0.0.0.0"})

    def test_the_parent_also_refuses_before_spawning_children(self, monkeypatch):
        """Otherwise a bad config is a reload loop of child tracebacks."""
        import subprocess

        monkeypatch.setattr(
            subprocess, "call",
            lambda *a, **k: pytest.fail("spawned uvicorn despite the guard"),
        )
        with pytest.raises(gw.GatewayError):
            gw.run_gateway_reload({"GATEWAY_HOST": "0.0.0.0"})

    def test_reload_runs_the_real_uvicorn_reloader(self, monkeypatch):
        import subprocess

        captured = {}
        monkeypatch.setattr(
            subprocess, "call",
            lambda cmd, **kw: captured.update(cmd=cmd, kw=kw) or 0,
        )
        rc = gw.run_gateway_reload({"GATEWAY_PORT": "9100"})
        assert rc == 0
        cmd = captured["cmd"]
        assert "uvicorn" in cmd
        assert "event_harvester.servers.gateway:make_app" in cmd
        assert "--factory" in cmd and "--reload" in cmd
        assert cmd[cmd.index("--port") + 1] == "9100"

    def test_run_gateway_delegates_when_asked(self, monkeypatch):
        monkeypatch.setattr(gw, "run_gateway_reload", lambda env: 7)
        assert gw.run_gateway({"GATEWAY_HOST": "127.0.0.1"}, reload=True) == 7
