"""Triton self-heal: recognise an unloaded model, load it once, report status."""

from __future__ import annotations

import httpx
import pytest

from event_harvester import llm, triton
from event_harvester.config import LLMConfig

MODEL = "qwen3-8b-gguf"
NOT_FOUND = (
    'TritonException - {"error":"Request for unknown model: '
    f"'{MODEL}' is not found\"}}"
)


def _cfg(**kw) -> LLMConfig:
    return LLMConfig(
        model="qwen3-8b", api_key="k", api_base="https://proxy/v1",
        triton_url="http://triton:8000", triton_model=MODEL, **kw,
    )


class FakeTriton:
    """Just enough of Triton's HTTP surface: health, model ready, load."""

    def __init__(self) -> None:
        self.loaded = False
        self.server_up = True
        self.load_ok = True
        self.loads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/health/ready":
            return httpx.Response(200 if self.server_up else 503)
        if path == f"/v2/models/{MODEL}/ready":
            if self.loaded:
                return httpx.Response(200)
            return httpx.Response(400, json={"error": "Model version not ready"})
        if path == f"/v2/repository/models/{MODEL}/load" and request.method == "POST":
            self.loads += 1
            if self.load_ok:
                self.loaded = True
                return httpx.Response(200)
            return httpx.Response(400, json={"error": "failed to load"})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch) -> FakeTriton:
    f = FakeTriton()
    monkeypatch.setattr(triton, "_TRANSPORT", httpx.MockTransport(f.handler))
    monkeypatch.setattr(triton, "_last_failed_at", 0.0)
    return f


def test_looks_unloaded_matches_the_proxy_error():
    assert triton.looks_unloaded(Exception(NOT_FOUND))
    assert triton.looks_unloaded(Exception("Model version not ready"))
    assert not triton.looks_unloaded(Exception("401 Unauthorized"))
    assert not triton.looks_unloaded(Exception("context length exceeded"))


def test_status_reports_server_up_model_unloaded(fake):
    st = triton.status(_cfg())
    assert st["configured"] is True
    assert st["server_ready"] is True
    assert st["model_ready"] is False
    assert "not ready" in st["error"]


def test_status_unconfigured_is_inert():
    st = triton.status(LLMConfig(model="gpt-4o-mini", api_key="k"))
    assert st["configured"] is False
    assert st["server_ready"] is None and st["model_ready"] is None


def test_ensure_loaded_loads_once_then_reports_already(fake):
    first = triton.ensure_loaded(_cfg(), timeout=5, poll=0)
    assert first["loaded"] and first["action"] == "loaded"
    assert fake.loads == 1
    second = triton.ensure_loaded(_cfg(), timeout=5, poll=0)
    assert second["action"] == "already"
    assert fake.loads == 1


def test_ensure_loaded_failure_backs_off(fake):
    fake.load_ok = False
    first = triton.ensure_loaded(_cfg(), timeout=5, poll=0)
    assert not first["loaded"] and first["action"] == "failed"
    assert fake.loads == 1
    second = triton.ensure_loaded(_cfg(), timeout=5, poll=0)
    assert second["action"] == "skipped"
    assert fake.loads == 1  # inside the retry window: no second load


def test_ensure_loaded_with_triton_down_fails_fast(fake):
    fake.server_up = False
    r = triton.ensure_loaded(_cfg(), timeout=5, poll=0)
    assert r["action"] == "failed" and fake.loads == 0


def test_chat_completion_loads_then_retries_primary_not_fallback(fake, monkeypatch):
    calls: list[str] = []

    def fake_litellm(messages, cfg, *, max_tokens, response_format):
        calls.append(cfg.model)
        if not fake.loaded:
            raise RuntimeError(NOT_FOUND)
        return "OK"

    monkeypatch.setattr(llm, "_complete_litellm", fake_litellm)
    cfg = _cfg(fallback_model="cloud/x", fallback_api_key="k2")
    assert llm.chat_completion([{"role": "user", "content": "hi"}], cfg) == "OK"
    assert calls == ["qwen3-8b", "qwen3-8b"]  # primary, load, primary again
    assert fake.loads == 1


def test_chat_completion_other_errors_fall_back_unchanged(fake, monkeypatch):
    def fake_litellm(messages, cfg, *, max_tokens, response_format):
        if cfg.model == "qwen3-8b":
            raise RuntimeError("401 Unauthorized")
        return "from-fallback"

    monkeypatch.setattr(llm, "_complete_litellm", fake_litellm)
    cfg = _cfg(fallback_model="cloud/x", fallback_api_key="k2")
    out = llm.chat_completion([{"role": "user", "content": "hi"}], cfg)
    assert out == "from-fallback"
    assert fake.loads == 0


def test_chat_completion_unconfigured_triton_falls_back_as_before(monkeypatch):
    def fake_litellm(messages, cfg, *, max_tokens, response_format):
        if cfg.model == "qwen3-8b":
            raise RuntimeError(NOT_FOUND)
        return "from-fallback"

    monkeypatch.setattr(llm, "_complete_litellm", fake_litellm)
    cfg = LLMConfig(model="qwen3-8b", api_key="k", api_base="https://proxy/v1",
                    fallback_model="cloud/x", fallback_api_key="k2")
    assert llm.chat_completion([{"role": "user", "content": "hi"}], cfg) == "from-fallback"
