"""Centralized configuration with validation."""

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass
class TelegramConfig:
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    session: str = "harvest_session"

    @property
    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash)


@dataclass
class OpenRouterConfig:
    api_key: str = ""
    model: str = "anthropic/claude-3.5-haiku"

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class TickTickConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8080"
    username: str = ""
    password: str = ""
    project: str = ""

    @property
    def is_configured(self) -> bool:
        return all([self.client_id, self.client_secret, self.username, self.password])


@dataclass
class DiscordConfig:
    cache_path: Optional[str] = None  # env override; auto-detected if None

    @property
    def is_configured(self) -> bool:
        return True  # Discord cache reading has no required credentials


@dataclass
class AppConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    ticktick: TickTickConfig = field(default_factory=TickTickConfig)
    days_back: int = 7

    # Filtering
    telegram_channels: list[str] = field(default_factory=list)
    telegram_exclude: list[str] = field(default_factory=list)


def _parse_csv_env(key: str) -> list[str]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def load_config() -> AppConfig:
    """Load configuration from environment variables."""
    api_id_raw = os.getenv("TELEGRAM_API_ID", "0")
    try:
        api_id = int(api_id_raw)
    except ValueError:
        api_id = 0

    days_raw = os.getenv("DAYS_BACK", "7")
    try:
        days_back = int(days_raw)
    except ValueError:
        days_back = 7

    return AppConfig(
        telegram=TelegramConfig(
            api_id=api_id,
            api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            phone=os.getenv("TELEGRAM_PHONE", ""),
            session=os.getenv("TELEGRAM_SESSION", "harvest_session"),
        ),
        discord=DiscordConfig(
            cache_path=os.getenv("DISCORD_CACHE_PATH"),
        ),
        openrouter=OpenRouterConfig(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku"),
        ),
        ticktick=TickTickConfig(
            client_id=os.getenv("TICKTICK_CLIENT_ID", ""),
            client_secret=os.getenv("TICKTICK_CLIENT_SECRET", ""),
            redirect_uri=os.getenv("TICKTICK_REDIRECT_URI", "http://127.0.0.1:8080"),
            username=os.getenv("TICKTICK_USERNAME", ""),
            password=os.getenv("TICKTICK_PASSWORD", ""),
            project=os.getenv("TICKTICK_PROJECT", ""),
        ),
        days_back=days_back,
        telegram_channels=_parse_csv_env("TELEGRAM_CHANNELS"),
        telegram_exclude=_parse_csv_env("TELEGRAM_EXCLUDE"),
    )


def validate_config(
    cfg: AppConfig,
    *,
    need_telegram: bool = True,
    need_discord: bool = True,
    need_analysis: bool = True,
    need_ticktick: bool = True,
) -> list[str]:
    """Validate config and return list of warnings. Raises ConfigError for fatal issues."""
    warnings: list[str] = []

    if need_telegram and not cfg.telegram.is_configured:
        warnings.append(
            "Telegram: TELEGRAM_API_ID and TELEGRAM_API_HASH not set — Telegram will be skipped. "
            "Get them from https://my.telegram.org"
        )

    if need_analysis and not cfg.openrouter.is_configured:
        warnings.append(
            "OpenRouter: OPENROUTER_API_KEY not set — analysis will be skipped. "
            "Get a key from https://openrouter.ai/settings/keys"
        )

    if need_ticktick and not cfg.ticktick.is_configured:
        warnings.append(
            "TickTick: Missing one or more of TICKTICK_CLIENT_ID, TICKTICK_CLIENT_SECRET, "
            "TICKTICK_USERNAME, TICKTICK_PASSWORD — task creation will be skipped."
        )

    return warnings
