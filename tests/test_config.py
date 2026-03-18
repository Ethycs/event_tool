"""Tests for config loading and validation."""

import os
from unittest.mock import patch

from event_harvester.config import (
    AppConfig,
    OpenRouterConfig,
    TelegramConfig,
    TickTickConfig,
    load_config,
    validate_config,
)


class TestConfigLoading:
    def test_load_config_defaults(self):
        """Config loads with sensible defaults when env is empty."""
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
            assert cfg.telegram.api_id == 0
            assert cfg.telegram.session == "harvest_session"
            assert cfg.openrouter.model == "anthropic/claude-3.5-haiku"
            assert cfg.days_back == 7
            assert cfg.telegram_channels == []

    def test_load_config_from_env(self):
        env = {
            "TELEGRAM_API_ID": "99999",
            "TELEGRAM_API_HASH": "abc123",
            "TELEGRAM_PHONE": "+1555",
            "OPENROUTER_API_KEY": "sk-test",
            "DAYS_BACK": "14",
            "TELEGRAM_CHANNELS": "chat-a, chat-b",
            "TELEGRAM_EXCLUDE": "spam",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
            assert cfg.telegram.api_id == 99999
            assert cfg.telegram.api_hash == "abc123"
            assert cfg.openrouter.api_key == "sk-test"
            assert cfg.days_back == 14
            assert cfg.telegram_channels == ["chat-a", "chat-b"]
            assert cfg.telegram_exclude == ["spam"]

    def test_load_config_invalid_api_id(self):
        with patch.dict(os.environ, {"TELEGRAM_API_ID": "not_a_number"}, clear=True):
            cfg = load_config()
            assert cfg.telegram.api_id == 0


class TestConfigValidation:
    def test_no_warnings_when_all_configured(self):
        cfg = AppConfig(
            telegram=TelegramConfig(api_id=123, api_hash="abc"),
            openrouter=OpenRouterConfig(api_key="sk-test"),
            ticktick=TickTickConfig(
                client_id="cid", client_secret="cs", username="u", password="p"
            ),
        )
        warnings = validate_config(cfg)
        assert warnings == []

    def test_warnings_when_telegram_missing(self):
        cfg = AppConfig()
        warnings = validate_config(
            cfg, need_discord=False, need_analysis=False, need_ticktick=False,
        )
        assert len(warnings) == 1
        assert "Telegram" in warnings[0]

    def test_no_warning_when_skipped(self):
        cfg = AppConfig()
        warnings = validate_config(
            cfg,
            need_telegram=False, need_discord=False,
            need_analysis=False, need_ticktick=False,
        )
        assert warnings == []

    def test_is_configured_properties(self):
        assert not TelegramConfig().is_configured
        assert TelegramConfig(api_id=1, api_hash="x").is_configured
        assert not OpenRouterConfig().is_configured
        assert OpenRouterConfig(api_key="k").is_configured
        assert not TickTickConfig().is_configured
        tt = TickTickConfig(
            client_id="a", client_secret="b", username="c", password="d",
        )
        assert tt.is_configured
