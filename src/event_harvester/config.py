"""Centralized configuration with validation."""

import os
from dataclasses import dataclass, field
from pathlib import Path
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
class LLMConfig:
    model: str = "gpt-4o-mini"
    api_key: str = ""
    backend: str = "litellm"  # "litellm" or "local"

    # Local backend settings (used when backend="local")
    model_path: str = ""  # path to .onnx model
    tokenizer_path: str = ""  # path to tokenizer directory (HF format)
    device: str = "npu"  # "npu", "gpu", or "cpu"

    @property
    def is_configured(self) -> bool:
        if self.backend == "local":
            return bool(self.model_path)
        return bool(self.model)

    @property
    def display_name(self) -> str:
        """Human-readable model name for logging."""
        if self.backend == "local":
            from pathlib import Path
            return Path(self.model_path).name or self.model_path
        return self.model

    @property
    def litellm_model(self) -> str:
        """Return model string in litellm format.

        litellm uses the model name directly for OpenAI (gpt-4o-mini),
        and provider/model for others (anthropic/claude-3.5-haiku).
        """
        return self.model


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
class GmailConfig:
    credentials_file: str = "credentials.json"
    token_file: str = "token.json"
    query: str = "newer_than:7d"
    max_results: int = 200

    @property
    def is_configured(self) -> bool:
        return (
            Path(self.credentials_file).exists()
            or bool(os.getenv("GMAIL_CREDENTIALS_JSON"))
            or bool(os.getenv("GMAIL_TOKEN_JSON"))
        )


@dataclass
class SignalConfig:
    db_path: Optional[str] = None  # env override; auto-detected if None

    @property
    def is_configured(self) -> bool:
        # Auto-detected — always "configured" on systems with Signal Desktop
        return True


@dataclass
class DiscordConfig:
    cache_path: Optional[str] = None  # env override; auto-detected if None

    @property
    def is_configured(self) -> bool:
        return True  # Discord cache reading has no required credentials


@dataclass
class WebConfig:
    sources_file: str = "data/web_sources.json"
    max_events: int = 30
    timeout_ms: int = 30000
    no_cooldown: bool = False  # if True, ignore per-source cooldowns


@dataclass
class SourceConfig:
    """Per-source enable flags. Used by the UI; CLI still has --only/--skip
    which override these for one run."""
    discord: bool = True
    telegram: bool = True
    gmail: bool = True
    signal: bool = True
    web: bool = True

    def to_no_kwargs(self) -> dict[str, bool]:
        """Translate to the no_<source> kwargs harvest_messages expects."""
        return {
            "no_discord": not self.discord,
            "no_telegram": not self.telegram,
            "no_gmail": not self.gmail,
            "no_signal": not self.signal,
            "no_web": not self.web,
        }


@dataclass
class CapConfig:
    """Per-source caps applied at the analysis stage.

    Messages are scored first, then capped per source, then merged up to
    the global `total` ceiling. Caps prevent any single noisy source from
    crowding out higher-signal events from other sources.
    """
    discord: int = 50
    telegram: int = 50
    gmail: int = 30
    signal: int = 30
    web: int = 30
    total: int = 150           # global ceiling after per-source caps
    group_by_source: bool = False  # if True, output is bucketed by source

    def get(self, platform: str) -> int:
        """Look up cap for a platform name (case-insensitive).

        Unknown platforms fall back to the web cap rather than the global
        total, since web_fetch tags messages with sub-source names
        (luma, instagram, eventbrite, ...) that aren't core attributes
        but should still respect a per-source ceiling.
        """
        return getattr(self, platform.lower(), self.web)


@dataclass
class AppConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    ticktick: TickTickConfig = field(default_factory=TickTickConfig)
    web: WebConfig = field(default_factory=WebConfig)
    caps: CapConfig = field(default_factory=CapConfig)
    sources: SourceConfig = field(default_factory=SourceConfig)
    days_back: int = 7

    # Pipeline behavior toggles (UI-controllable)
    skip_analyze: bool = False  # skip LLM extraction, return raw harvest
    dry_run: bool = False       # never create real TickTick tasks

    # Obsidian output
    obsidian_events_dir: str = ""
    obsidian_recruiters_dir: str = ""

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
        gmail=GmailConfig(
            credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
            token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
        ),
        signal=SignalConfig(
            db_path=os.getenv("SIGNAL_DB_PATH"),
        ),
        llm=LLMConfig(
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY", ""),
            backend=os.getenv("LLM_BACKEND", "litellm"),
            model_path=os.getenv("LLM_MODEL_PATH", ""),
            tokenizer_path=os.getenv("LLM_TOKENIZER_PATH", ""),
            device=os.getenv("LLM_DEVICE", "npu"),
        ),
        ticktick=TickTickConfig(
            client_id=os.getenv("TICKTICK_CLIENT_ID", ""),
            client_secret=os.getenv("TICKTICK_CLIENT_SECRET", ""),
            redirect_uri=os.getenv("TICKTICK_REDIRECT_URI", "http://127.0.0.1:8080"),
            username=os.getenv("TICKTICK_USERNAME", ""),
            password=os.getenv("TICKTICK_PASSWORD", ""),
            project=os.getenv("TICKTICK_PROJECT", ""),
        ),
        web=WebConfig(
            sources_file=os.getenv("WEB_SOURCES_FILE", "data/web_sources.json"),
            max_events=int(os.getenv("WEB_MAX_EVENTS", "30")),
            timeout_ms=int(os.getenv("WEB_TIMEOUT_MS", "30000")),
            no_cooldown=os.getenv("WEB_NO_COOLDOWN", "").lower() in ("1", "true", "yes"),
        ),
        caps=CapConfig(
            discord=int(os.getenv("CAP_DISCORD", "50")),
            telegram=int(os.getenv("CAP_TELEGRAM", "50")),
            gmail=int(os.getenv("CAP_GMAIL", "30")),
            signal=int(os.getenv("CAP_SIGNAL", "30")),
            web=int(os.getenv("CAP_WEB", "30")),
            total=int(os.getenv("CAP_TOTAL", "150")),
            group_by_source=os.getenv("CAP_GROUP_BY_SOURCE", "").lower() in ("1", "true", "yes"),
        ),
        sources=SourceConfig(
            discord=os.getenv("SOURCE_DISCORD", "1").lower() not in ("0", "false", "no"),
            telegram=os.getenv("SOURCE_TELEGRAM", "1").lower() not in ("0", "false", "no"),
            gmail=os.getenv("SOURCE_GMAIL", "1").lower() not in ("0", "false", "no"),
            signal=os.getenv("SOURCE_SIGNAL", "1").lower() not in ("0", "false", "no"),
            web=os.getenv("SOURCE_WEB", "1").lower() not in ("0", "false", "no"),
        ),
        skip_analyze=os.getenv("SKIP_ANALYZE", "").lower() in ("1", "true", "yes"),
        dry_run=os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"),
        obsidian_events_dir=os.getenv("OBSIDIAN_EVENTS_DIR", ""),
        obsidian_recruiters_dir=os.getenv("OBSIDIAN_RECRUITERS_DIR", ""),
        days_back=days_back,
        telegram_channels=_parse_csv_env("TELEGRAM_CHANNELS"),
        telegram_exclude=_parse_csv_env("TELEGRAM_EXCLUDE"),
    )


def validate_config(
    cfg: AppConfig,
    *,
    need_telegram: bool = True,
    need_discord: bool = True,
    need_gmail: bool = True,
    need_analysis: bool = True,
    need_ticktick: bool = True,
) -> list[str]:
    """Validate config and return list of warnings. Raises ConfigError for fatal issues."""
    warnings: list[str] = []

    if need_telegram and not cfg.telegram.is_configured:
        warnings.append(
            "Telegram: TELEGRAM_API_ID and TELEGRAM_API_HASH not set - Telegram will be skipped. "
            "Get them from https://my.telegram.org"
        )

    if need_gmail and not cfg.gmail.is_configured:
        warnings.append(
            "Gmail: credentials.json not found - Gmail will be skipped. "
            "Download it from Google Cloud Console (APIs & Services > Credentials)."
        )

    if need_analysis and not cfg.llm.is_configured:
        warnings.append(
            "LLM: No model configured - analysis will be skipped. "
            "Set LLM_MODEL and the appropriate API key env var."
        )

    if need_ticktick and not cfg.ticktick.is_configured:
        warnings.append(
            "TickTick: Missing one or more of TICKTICK_CLIENT_ID, TICKTICK_CLIENT_SECRET, "
            "TICKTICK_USERNAME, TICKTICK_PASSWORD - task creation will be skipped."
        )

    return warnings
