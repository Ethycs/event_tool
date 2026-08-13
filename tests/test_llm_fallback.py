"""The primary/fallback chain, and the routing it needs to reach a proxy.

Default since 2026-08-13: `qwen3-8b` on the mesh, with a cloud model behind it.
The local model is cheap, fast and private; it is also one box on one network,
and it can be down, out of context, or mid-restart. None of that should turn a
harvest into zero events — on 2026-08-12 an upstream outage did exactly that
and the empty result overwrote a cache holding 251.

The fallback is therefore about availability, not quality, and these tests pin
the three things that make it trustworthy: it triggers, it is *loud*, and it
cannot loop.
"""

from __future__ import annotations

import logging

import pytest

from event_harvester.config import LLMConfig
from event_harvester.llm import _routed_model, chat_completion

MSGS = [{"role": "user", "content": "hi"}]


def _cfg(**kw):
    base = {"model": "qwen3-8b", "api_key": "primary-key",
            "api_base": "https://central-dev.lan:4000/v1",
            "fallback_model": "codex_sdk/gpt-5.6-luna",
            "fallback_api_key": "fallback-key",
            "fallback_api_base": "https://central-dev.lan:4000/v1"}
    base.update(kw)
    return LLMConfig(**base)


class TestRouting:
    """litellm reads the first path segment of a model name as a provider."""

    def test_a_proxy_model_gets_the_openai_prefix(self):
        assert _routed_model(_cfg()) == "openai/qwen3-8b"

    def test_a_nested_name_is_not_read_as_a_provider(self):
        """`codex_sdk/gpt-5.6-luna` without the prefix becomes "provider
        codex_sdk", which does not exist — every call then fails with a
        provider-list error that reads like a network fault."""
        assert _routed_model(_cfg(model="codex_sdk/gpt-5.6-luna")) == \
            "openai/codex_sdk/gpt-5.6-luna"

    def test_an_explicit_provider_is_left_alone(self):
        assert _routed_model(_cfg(model="anthropic/claude-haiku-4-5")) == \
            "anthropic/claude-haiku-4-5"

    def test_without_an_endpoint_the_name_is_the_provider_s_own(self):
        assert _routed_model(_cfg(model="gpt-4o-mini", api_base="")) == "gpt-4o-mini"


class TestFallback:
    def test_it_takes_over_when_the_primary_fails(self, monkeypatch):
        seen = []

        def fake(messages, cfg, *, max_tokens, response_format):
            seen.append(cfg.model)
            if cfg.model == "qwen3-8b":
                raise RuntimeError("context window exceeded")
            return "[Event.1]\ntitle = Saved\n"

        monkeypatch.setattr("event_harvester.llm._complete_litellm", fake)
        out = chat_completion(MSGS, _cfg())
        assert "Saved" in out
        assert seen == ["qwen3-8b", "codex_sdk/gpt-5.6-luna"]

    def test_it_uses_the_fallback_credentials(self, monkeypatch):
        """A proxy virtual key and a provider key are different things."""
        captured = {}

        def fake(messages, cfg, *, max_tokens, response_format):
            if cfg.model == "qwen3-8b":
                raise RuntimeError("down")
            captured["key"] = cfg.api_key
            captured["base"] = cfg.api_base
            return "ok"

        monkeypatch.setattr("event_harvester.llm._complete_litellm", fake)
        chat_completion(MSGS, _cfg())
        assert captured["key"] == "fallback-key"
        assert captured["base"] == "https://central-dev.lan:4000/v1"

    def test_it_cannot_loop(self, monkeypatch):
        """The fallback is the escape hatch; it must not have one of its own."""
        depth = {"n": 0}

        def fake(messages, cfg, *, max_tokens, response_format):
            depth["n"] += 1
            assert depth["n"] < 5, "the fallback recursed"
            raise RuntimeError("everything is down")

        monkeypatch.setattr("event_harvester.llm._complete_litellm", fake)
        with pytest.raises(RuntimeError, match="everything is down"):
            chat_completion(MSGS, _cfg())
        assert depth["n"] == 2, "exactly one retry, against the fallback"

    def test_the_original_error_surfaces_when_no_fallback_is_set(self, monkeypatch):
        def fake(messages, cfg, *, max_tokens, response_format):
            raise RuntimeError("primary is down")

        monkeypatch.setattr("event_harvester.llm._complete_litellm", fake)
        with pytest.raises(RuntimeError, match="primary is down"):
            chat_completion(MSGS, _cfg(fallback_model=""))

    def test_it_is_loud(self, monkeypatch, caplog):
        """A fallback that silently papers over a dead primary is how you find
        out months later that nothing local has run and the bill is cloud."""
        def fake(messages, cfg, *, max_tokens, response_format):
            if cfg.model == "qwen3-8b":
                raise RuntimeError("down")
            return "ok"

        monkeypatch.setattr("event_harvester.llm._complete_litellm", fake)
        with caplog.at_level(logging.ERROR, logger="event_harvester.llm"):
            chat_completion(MSGS, _cfg())
        assert any("falling back" in r.message for r in caplog.records)

    def test_a_healthy_primary_never_reaches_the_fallback(self, monkeypatch):
        calls = []

        def fake(messages, cfg, *, max_tokens, response_format):
            calls.append(cfg.model)
            return "[Event.1]\ntitle = Fine\n"

        monkeypatch.setattr("event_harvester.llm._complete_litellm", fake)
        chat_completion(MSGS, _cfg())
        assert calls == ["qwen3-8b"]


class TestConfigWiring:
    def test_the_batch_size_reaches_the_extractor(self, monkeypatch):
        """It is a property of the served model's context window, so it must
        come from config rather than a function default."""
        from event_harvester.pipeline import analysis

        captured = {}

        def fake(filtered, days_back, cfg, batch_size=10):
            captured["batch_size"] = batch_size
            return "", [], analysis.empty_rejects()

        monkeypatch.setattr(analysis, "_extract_events_cloud", fake)
        cfg = _cfg(batch_size=5)
        analysis.extract_events_llm(
            [{"content": "party friday 9pm", "platform": "telegram",
              "channel": "x", "author": "@a", "timestamp": "2026-08-12T10:00:00+00:00",
              "id": "1"}],
            days_back=7, cfg=cfg,
        )
        assert captured["batch_size"] == 5

    def test_ssl_verification_is_on_unless_turned_off(self):
        assert LLMConfig().verify_ssl is True
