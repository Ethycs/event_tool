"""The LLM stage has four ways to be misconfigured; only one used to be checked.

`LLMConfig.is_configured` returned `bool(self.model)`, and `model` has always
had a non-empty default — so it was `True` for every config ever constructed.
`validate_config` therefore never warned, and the pipeline went on to make
calls that could not succeed.

Survivable while the default was `gpt-4o-mini`, which at least names a real
model at a well-known provider. Not survivable from 2026-08-13, when the
default became **`qwen3-8b`, which exists only behind a particular endpoint**:
with no `api_base` it goes to OpenAI, which has never heard of it, and the
operator gets a 404 mid-harvest instead of a warning at startup.
"""

from __future__ import annotations

import pytest

from event_harvester.config import AppConfig, LLMConfig, _llm_warnings, validate_config


def _warn_text(llm: LLMConfig) -> str:
    return " ".join(_llm_warnings(llm))


class TestIsConfigured:
    def test_a_model_name_alone_is_not_configuration(self):
        assert not LLMConfig(model="qwen3-8b").is_configured

    def test_a_key_is_enough(self):
        assert LLMConfig(model="gpt-4o-mini", api_key="sk-x").is_configured

    def test_an_endpoint_is_enough(self):
        """A LAN Ollama or vLLM typically wants no key at all."""
        assert LLMConfig(model="qwen3-8b",
                         api_base="http://localhost:11434/v1").is_configured

    def test_no_model_is_never_configured(self):
        assert not LLMConfig(model="", api_key="sk-x").is_configured

    def test_the_local_backend_only_needs_its_model_path(self):
        assert LLMConfig(backend="local", model_path="/m.onnx").is_configured
        assert not LLMConfig(backend="local", model_path="").is_configured


class TestNeedsEndpoint:
    def test_a_local_only_model_sent_to_openai_is_caught(self):
        """The exact shape of the new default with LLM_API_BASE forgotten."""
        cfg = LLMConfig(model="qwen3-8b", api_key="sk-x")
        assert cfg.needs_endpoint
        assert "not a name any public provider serves" in _warn_text(cfg)

    def test_an_openai_model_is_fine_without_one(self):
        assert not LLMConfig(model="gpt-4o-mini", api_key="sk-x").needs_endpoint

    def test_an_explicit_provider_is_fine_without_one(self):
        assert not LLMConfig(model="anthropic/claude-haiku-4-5",
                             api_key="sk-x").needs_endpoint

    def test_an_endpoint_settles_it(self):
        assert not LLMConfig(model="qwen3-8b", api_key="k",
                             api_base="https://h:4000/v1").needs_endpoint


class TestFallbackCredentials:
    def test_a_fallback_that_cannot_authenticate_is_flagged(self):
        """It is only exercised when something has already gone wrong, so a
        401 there is worse than having no fallback at all."""
        cfg = LLMConfig(model="qwen3-8b", api_base="https://h:4000/v1",
                        fallback_model="codex_sdk/gpt-5.6-luna")
        assert not cfg.fallback_is_configured
        assert "no credentials of its own" in _warn_text(cfg)

    def test_its_own_key_satisfies_it(self):
        cfg = LLMConfig(model="qwen3-8b", api_base="https://h:4000/v1",
                        fallback_model="x", fallback_api_key="sk-fb")
        assert cfg.fallback_is_configured

    def test_an_inherited_key_satisfies_it(self):
        cfg = LLMConfig(model="qwen3-8b", api_key="sk-primary",
                        api_base="https://h:4000/v1", fallback_model="x")
        assert cfg.fallback_is_configured

    def test_no_fallback_is_not_an_error(self):
        """One model is a valid configuration. A warning that fires on every
        run is one the reader learns to skip past."""
        cfg = LLMConfig(model="gpt-4o-mini", api_key="sk-x")
        assert not cfg.fallback_is_configured
        assert _llm_warnings(cfg) == []


class TestWarnings:
    def test_nothing_reachable_says_so(self):
        assert "neither a key nor an endpoint" in _warn_text(LLMConfig(model="x"))

    def test_a_missing_model_says_so(self):
        assert "LLM_MODEL is unset" in _warn_text(LLMConfig(model=""))

    def test_the_local_backend_gets_its_own_message(self):
        text = _warn_text(LLMConfig(backend="local", model_path=""))
        assert "LLM_MODEL_PATH" in text

    def test_disabled_tls_is_reported(self):
        cfg = LLMConfig(model="qwen3-8b", api_key="k",
                        api_base="https://central-dev.lan:4000/v1",
                        verify_ssl=False)
        assert "TLS verification is off" in _warn_text(cfg)

    def test_verified_tls_is_silent(self):
        cfg = LLMConfig(model="qwen3-8b", api_key="k",
                        api_base="https://h:4000/v1", verify_ssl=True)
        assert _llm_warnings(cfg) == []

    def test_every_warning_names_the_variable_to_set(self):
        """A warning that says "configure the LLM" makes the reader go and
        read config.py."""
        for cfg in (LLMConfig(model=""),
                    LLMConfig(model="qwen3-8b"),
                    LLMConfig(model="qwen3-8b", api_key="k"),
                    LLMConfig(backend="local", model_path="")):
            for warning in _llm_warnings(cfg):
                assert "LLM_" in warning, warning

    def test_a_healthy_default_deployment_is_quiet(self):
        """Exactly what `.env` now sets, minus the self-signed note."""
        cfg = LLMConfig(model="qwen3-8b", api_key="sk-local",
                        api_base="https://central-dev.lan:4000/v1",
                        fallback_model="codex_sdk/gpt-5.6-luna",
                        fallback_api_key="sk-cloud")
        assert _llm_warnings(cfg) == []


class TestReachesValidateConfig:
    def test_the_warning_surfaces_through_the_public_entry_point(self):
        cfg = AppConfig(llm=LLMConfig(model="qwen3-8b"))
        warnings = validate_config(cfg, need_telegram=False, need_discord=False,
                                   need_gmail=False, need_ticktick=False)
        assert any("LLM" in w for w in warnings)

    @pytest.mark.parametrize("flag", [True, False])
    def test_need_analysis_gates_it(self, flag):
        cfg = AppConfig(llm=LLMConfig(model="qwen3-8b"))
        warnings = validate_config(cfg, need_telegram=False, need_discord=False,
                                   need_gmail=False, need_ticktick=False,
                                   need_analysis=flag)
        assert bool(warnings) is flag


class TestEnvHelpers:
    def test_a_flag_defaults_when_unset(self, monkeypatch):
        from event_harvester.config import _env_flag

        monkeypatch.delenv("EH_TEST_FLAG", raising=False)
        assert _env_flag("EH_TEST_FLAG", default=True) is True
        assert _env_flag("EH_TEST_FLAG", default=False) is False

    def test_an_empty_string_is_not_false(self, monkeypatch):
        """`LLM_VERIFY_SSL=` should mean "unset", not "off"."""
        from event_harvester.config import _env_flag

        monkeypatch.setenv("EH_TEST_FLAG", "   ")
        assert _env_flag("EH_TEST_FLAG", default=True) is True

    def test_junk_ints_fall_back_without_raising(self, monkeypatch):
        """config.py had no logger, so the warning here raised NameError."""
        from event_harvester.config import _env_int

        monkeypatch.setenv("EH_TEST_INT", "not-a-number")
        assert _env_int("EH_TEST_INT", 5, minimum=1, maximum=50) == 5

    def test_out_of_range_ints_fall_back(self, monkeypatch):
        from event_harvester.config import _env_int

        monkeypatch.setenv("EH_TEST_INT", "9999")
        assert _env_int("EH_TEST_INT", 5, minimum=1, maximum=50) == 5
