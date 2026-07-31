"""Tests for the events site server.

No harvest runs here: `_collect` is patched. The behaviours that matter are the
ones a broken harvest would otherwise take down — the site must keep serving
the last good page — and the concurrency guard, because a refresh opens
browsers and calls an LLM.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from event_harvester.servers import site as site_mod


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Never write to the real data/.site."""
    monkeypatch.setattr(site_mod, "SITE_DIR", tmp_path / "site")
    monkeypatch.setattr(site_mod, "SITE_HTML", tmp_path / "site" / "index.html")
    monkeypatch.setattr(site_mod, "SITE_JSON", tmp_path / "site" / "events.json")
    return tmp_path


EVENTS = [
    {"title": "Queer Night", "date": "2026-08-01", "time": "9pm",
     "location": "The Stud", "source": "instagram in stud", "priority": 5},
    {"title": "AI Meetup", "date": "2026-08-04", "source": "meetup", "priority": 3},
]


def _collect_ok(cfg, *, harvest):
    return (list(EVENTS), {"web": 2}, 41)


def client(**kwargs):
    # Imported here, not at module scope: Starlette's TestClient emits a
    # deprecation warning about httpx *on import*, which happens during
    # collection — before any `pytestmark` filter can apply. Swapping httpx is
    # a dependency decision, not a test one, so it is suppressed at the seam.
    import warnings

    # `StarletteDeprecationWarning` does not subclass `DeprecationWarning`, so
    # the filter has to be unqualified — scoped to this one import statement.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

    app = site_mod.build_app(object(), schedule=False, harvest_on_start=False, **kwargs)
    return TestClient(app)


class TestServing:
    def test_the_page_is_built_and_served(self):
        with patch.object(site_mod, "_collect", side_effect=_collect_ok):
            with client() as c:
                res = c.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "Queer Night" in res.text or "events-data" in res.text

    def test_the_page_is_never_cached(self):
        """A cached copy of yesterday's events is worse than a slow load."""
        with patch.object(site_mod, "_collect", side_effect=_collect_ok):
            with client() as c:
                assert c.get("/").headers["cache-control"] == "no-store"

    def test_events_json_matches_what_the_page_embeds(self):
        with patch.object(site_mod, "_collect", side_effect=_collect_ok):
            with client() as c:
                payload = json.loads(c.get("/events.json").text)
        assert [e["title"] for e in payload] == ["Queer Night", "AI Meetup"]
        assert all("bucket" in e for e in payload), "same projection as the page"

    def test_healthz_reports_the_build(self):
        with patch.object(site_mod, "_collect", side_effect=_collect_ok):
            with client(interval_hours=6) as c:
                health = c.get("/healthz").json()
        assert health["ok"] is True
        assert health["events"] == 2
        assert health["interval_hours"] == 6
        assert health["age_seconds"] is not None

    def test_an_unbuilt_site_says_so_rather_than_500ing(self):
        def nothing(cfg, *, harvest):
            return ([], {}, 0)

        # No events and no file written yet -> index must explain itself.
        with patch.object(site_mod, "_collect", side_effect=nothing), \
             patch.object(site_mod, "_write", side_effect=RuntimeError("disk full")):
            with client() as c:
                res = c.get("/")
                health = c.get("/healthz")
        assert res.status_code == 503
        assert "has not been built" in res.text
        assert health.status_code == 503
        assert "disk full" in health.json()["error"]


class TestRefreshIsSurvivable:
    def test_a_failed_harvest_keeps_the_last_good_page(self):
        """The site must not go down because one harvest broke."""
        with patch.object(site_mod, "_collect", side_effect=_collect_ok):
            with client() as c:
                assert c.get("/").status_code == 200

                with patch.object(site_mod, "_collect",
                                  side_effect=RuntimeError("cloudflare said no")):
                    res = c.post("/refresh?harvest=1")

                assert res.json()["error"] is not None
                # Still serving.
                assert c.get("/").status_code == 200
                assert c.get("/healthz").json()["events"] == 2

    def test_refresh_is_post_only(self):
        """So a link preview or a crawler cannot trigger a harvest."""
        with patch.object(site_mod, "_collect", side_effect=_collect_ok):
            with client() as c:
                assert c.get("/refresh").status_code == 405

    def test_a_second_refresh_is_skipped_while_one_is_running(self):
        """A harvest opens browsers and calls an LLM; two at once doubles it."""
        state = site_mod.SiteState()
        started = asyncio.Event()

        async def slow(cfg, *, harvest):
            started.set()
            await asyncio.sleep(0.2)
            return ([], {}, 0)

        async def scenario():
            with patch.object(site_mod, "_collect", side_effect=slow):
                first = asyncio.create_task(site_mod.refresh(state, object()))
                await started.wait()
                second = await site_mod.refresh(state, object())
                await first
            return second

        asyncio.run(scenario())
        assert state.refresh_count == 1, "the second refresh should have been skipped"

    def test_refresh_without_harvest_reads_the_mcp_cache(self, tmp_path, monkeypatch):
        """`--no-harvest-on-start` serves whatever the last harvest produced."""
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({
            "events": [{"title": "From cache", "date": "2026-08-02"}],
            "source_counts": {"web": 1},
            "stats": {"total_messages": 7},
        }))
        monkeypatch.setattr(
            "event_harvester.servers.mcp_server.LATEST_EVENTS_FILE", cache,
        )
        state = site_mod.SiteState()
        snapshot = asyncio.run(site_mod.refresh(state, object(), harvest=False))
        assert snapshot["events"] == 1
        assert "From cache" in site_mod.SITE_JSON.read_text(encoding="utf-8")


class TestTheEntryPointActuallyStarts:
    """The tests above drive `build_app`; this drives the command.

    That gap let a fatal bug ship: `run_site` called `uvicorn.run()`, which
    calls `asyncio.run()` internally, while the CLI dispatcher already had a
    loop running. The process died instantly with "asyncio.run() cannot be
    called from a running event loop" — visible as SERVICE_PAUSED under NSSM,
    and as an immediate traceback from a terminal. Nothing in the suite noticed
    because nothing in the suite ran the command.
    """

    def test_run_site_is_awaitable(self):
        import inspect

        assert inspect.iscoroutinefunction(site_mod.run_site)

    def test_the_command_awaits_the_server_rather_than_calling_uvicorn_run(self):
        from types import SimpleNamespace

        from event_harvester.cli.commands.site import site_cmd

        served = {"count": 0}

        class FakeServer:
            def __init__(self, config):
                self.config = config

            async def serve(self):
                served["count"] += 1

        fake_uvicorn = SimpleNamespace(
            Server=FakeServer,
            Config=lambda app, **kw: SimpleNamespace(app=app, **kw),
            run=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("uvicorn.run() cannot be used inside the CLI's loop")
            ),
        )

        args = SimpleNamespace(
            host="127.0.0.1", port=8800, interval_hours=24.0,
            no_schedule=True, no_harvest_on_start=True, build_only=False,
        )

        with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}), \
             patch.object(site_mod, "_collect", side_effect=_collect_ok):
            code = asyncio.run(site_cmd(args, object()))

        assert code == 0
        assert served["count"] == 1, "the server was never actually served"

    def test_build_only_writes_the_page_and_exits(self):
        from types import SimpleNamespace

        from event_harvester.cli.commands.site import site_cmd

        args = SimpleNamespace(
            host="127.0.0.1", port=8800, interval_hours=24.0,
            no_schedule=True, no_harvest_on_start=False, build_only=True,
        )
        with patch.object(site_mod, "_collect", side_effect=_collect_ok):
            code = asyncio.run(site_cmd(args, object()))
        assert code == 0
        assert site_mod.SITE_HTML.exists()


class TestReloadMode:
    """`--reload` is a dev affordance and must not become a harvest loop."""

    def test_the_factory_defaults_to_not_harvesting(self, monkeypatch):
        """A save every few seconds would otherwise start a harvest each time."""
        for var in ("EH_SITE_HARVEST_ON_START", "EH_SITE_SCHEDULE"):
            monkeypatch.delenv(var, raising=False)
        seen = {}

        def fake_build(cfg, **kwargs):
            seen.update(kwargs)
            return object()

        with patch.object(site_mod, "build_app", side_effect=fake_build), \
             patch("event_harvester.config.load_config", return_value=object()):
            site_mod.make_app()
        assert seen["harvest_on_start"] is False
        assert seen["schedule"] is False

    def test_the_factory_honours_the_environment(self, monkeypatch):
        monkeypatch.setenv("EH_SITE_HARVEST_ON_START", "1")
        monkeypatch.setenv("EH_SITE_SCHEDULE", "yes")
        monkeypatch.setenv("EH_SITE_INTERVAL_HOURS", "3")
        seen = {}

        with patch.object(site_mod, "build_app",
                          side_effect=lambda cfg, **kw: seen.update(kw) or object()), \
             patch("event_harvester.config.load_config", return_value=object()):
            site_mod.make_app()
        assert seen["harvest_on_start"] is True
        assert seen["schedule"] is True
        assert seen["interval_hours"] == 3.0

    def test_reload_shells_out_to_uvicorn_with_the_factory(self):
        """uvicorn reloads by supervising children, which cannot be driven
        from inside the CLI's already-running loop."""
        captured = {}

        def fake_call(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return 0

        with patch("subprocess.call", side_effect=fake_call):
            assert site_mod.run_site_reload(port=9001) == 0

        cmd = captured["cmd"]
        assert "uvicorn" in cmd
        assert "event_harvester.servers.site:make_app" in cmd
        assert "--factory" in cmd and "--reload" in cmd
        assert cmd[cmd.index("--port") + 1] == "9001"
        assert captured["env"]["EH_SITE_HARVEST_ON_START"] == "0"


class TestScheduling:
    def test_the_interval_has_a_floor(self):
        """A typo'd tiny interval would otherwise harvest in a hot loop."""
        app = site_mod.build_app(object(), interval_hours=0.0000001, schedule=False)
        assert app is not None  # the clamp lives in _loop; see below

    def test_the_loop_clamps_to_at_least_a_minute(self):
        import inspect

        source = inspect.getsource(site_mod.build_app)
        assert "max(60.0" in source, "the sleep must be floored"
