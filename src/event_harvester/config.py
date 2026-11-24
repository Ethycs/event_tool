"""Centralized configuration with validation."""

import json
import logging
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

    # Rolling window: instead of re-scanning the full `days_back` every run,
    # fetch only from just before the previous harvest, plus an overlap buffer.
    # `days_back` stays the *ceiling* (first run, or after a long gap), so this
    # only ever narrows the window, never widens it.
    rolling_window: bool = True
    overlap_hours: float = 6.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash)


@dataclass
class LLMConfig:
    # Default since 2026-08-13: an 8B model on the mesh, measured at 90% of
    # `gpt-5.6-luna`'s events over identical batches, inventing none and
    # filling every field better, at half the latency. See
    # `03 - Architecture/11 - Training & Evaluation.md`.
    model: str = "qwen3-8b"
    api_key: str = ""
    backend: str = "litellm"  # "litellm" or "local"

    # Where the primary model is served. Empty means the provider's own
    # endpoint, which is what a bare `gpt-4o-mini` wants.
    api_base: str = ""
    # The mesh proxy serves a self-signed certificate. Off by default; only a
    # deployment that knows its endpoint should turn verification off.
    verify_ssl: bool = True

    # **The fallback exists because the primary is one box on one network.**
    # A local model is cheap and fast and cannot page anyone at 3am; when it is
    # unreachable, out of context, or refusing, the harvest should degrade to a
    # cloud model rather than produce zero events. Empty disables it.
    fallback_model: str = ""
    fallback_api_key: str = ""
    fallback_api_base: str = ""

    # Messages per extraction call. **5, not 10, and that is load-bearing for
    # the default model**: qwen3-8b is served with a 2048-token context, and at
    # 10 the largest batches overflow it — 6 of 15 were refused outright, and
    # those held 51 of 78 events. At 5 every call fits (1,297 tokens worst
    # case) and the baseline's own yield is unchanged (78 -> 80 events on
    # identical input), so this costs nothing but request count.
    batch_size: int = 5

    # Local backend settings (used when backend="local")
    model_path: str = ""  # path to .onnx model
    tokenizer_path: str = ""  # path to tokenizer directory (HF format)
    device: str = "npu"  # "npu", "gpu", or "cpu"

    # Vision model overrides — used by ``llm.vision_completion`` and
    # ``nav_quality.NavQualityScorer``. Empty ``vision_model`` means
    # auto-pick based on the API key prefix (sk-ant- → Claude Sonnet,
    # otherwise gpt-4o-mini).
    vision_model: str = ""
    # Hard ceiling on how many images a single pipeline run is allowed
    # to send to the VLM. Acts as a cost guard for the nav-quality
    # scorer; once exceeded, subsequent ``score_source`` calls
    # short-circuit to classification="unknown".
    vision_max_images_per_run: int = 10

    # Triton behind the proxy, reached directly over the mesh. The proxy only
    # forwards inference; it cannot load a model Triton has unloaded, and
    # Triton unloads everything on restart (explicit model control). With
    # both set, the harvester loads the model itself when a call fails for
    # want of it (`llm._retry_after_triton_load`) and the `llm_status` tool
    # can say whether it is loaded. Empty disables both.
    triton_url: str = ""         # e.g. http://10.158.218.141:8000
    triton_model: str = ""       # Triton repository name, e.g. qwen3-8b-gguf

    @property
    def triton_is_configured(self) -> bool:
        return bool(self.triton_url and self.triton_model)

    @property
    def is_configured(self) -> bool:
        """Whether this config could actually reach a model.

        **A model name alone is not configuration.** This used to return
        `bool(self.model)`, which — since `model` has always had a non-empty
        default — was `True` for every config ever constructed. `validate_config`
        therefore never warned about a missing LLM setup, and the pipeline went
        on to make calls that could not succeed.

        That was survivable while the default was `gpt-4o-mini`, which at least
        names a real model at a well-known provider. It stopped being
        survivable on 2026-08-13: **the default is now `qwen3-8b`, which exists
        only behind a particular endpoint.** With no `api_base` it is sent to
        OpenAI, which has never heard of it, and the operator gets a 404 in a
        log rather than a warning at startup.

        So: a model, and something to reach it with — a key, or an endpoint
        that needs none (a LAN Ollama or vLLM typically does).
        """
        if self.backend == "local":
            return bool(self.model_path)
        return bool(self.model) and bool(self.api_key or self.api_base)

    @property
    def needs_endpoint(self) -> bool:
        """True when the model name cannot be resolved by a public provider.

        A bare name with no provider prefix and no `api_base` is going to
        OpenAI by default. `gpt-*` belongs there; `qwen3-8b` does not, and the
        failure arrives as an unhelpful 404 mid-harvest.
        """
        if self.backend == "local" or self.api_base:
            return False
        model = (self.model or "").lower()
        if "/" in model:            # an explicit provider — anthropic/, ollama/
            return False
        return not model.startswith(("gpt-", "o1-", "o3-", "chatgpt-",
                                     "text-", "davinci"))

    @property
    def fallback_is_configured(self) -> bool:
        """Whether the fallback could actually answer.

        It inherits the primary's key when it has none of its own, and on this
        deployment those are *different* keys for *different* model groups —
        the local-models key cannot reach `codex_sdk/*`. A fallback that 401s
        is worse than no fallback, because it is only exercised when something
        has already gone wrong.
        """
        if not self.fallback_model:
            return False
        return bool(self.fallback_api_key or self.fallback_api_base
                    or self.api_key)

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
    # Extra Gmail search terms, ANDed with the `after:` filter the harvest
    # derives from --days-back. Empty by default: this used to be
    # "newer_than:7d", which silently won that AND and capped every run at a
    # week no matter what --days-back said.
    query: str = ""
    max_results: int = 200

    # Fetch each message's real body instead of Gmail's 200-char snippet.
    # Measured 2026-08-10 over 85 messages: median body 2505 chars, so the
    # snippet is ~8% of an email. 32 of those 85 reached the reranker with no
    # date and no link and were dropped for it; 4 carried an Eventbrite/Luma
    # link that existed only past the 200-char cut. Costs one extra
    # `messages().get(format="full")` per message.
    fetch_bodies: bool = True
    # Ceiling on hydrated `content`. Matches the web path's detail-page cap.
    # A newsletter runs to 15 KB of footer; the event is at the top.
    body_max_chars: int = 5000
    # Drop mail you have already read. False for event harvesting -- an
    # invite you opened is still an invite, and this was dropping 42% of the
    # mailbox. `True` is the recruiter-triage meaning ("still needs action").
    drop_read: bool = False
    # Follow event-platform links (Luma, Eventbrite, …) found in a body and
    # read their schema.org JSON-LD. Measured 2026-08-10: 12 of 14 links
    # returned exact date, time AND venue to a plain GET -- no browser. That
    # is ground truth, where the body only ever offered prose to infer from.
    expand_links: bool = True
    expand_max: int = 40        # ceiling on outbound fetches per harvest
    expand_timeout_s: int = 12
    expand_workers: int = 8     # plain `requests` calls; IO-bound
    # Gmail label applied to mail that produced an event. Empty disables it.
    # Additive only -- it adds a label, never removes one, and never moves a
    # message out of the inbox. Skipped entirely on --dry-run.
    events_label: str = "events"

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
    # Names of web sub-sources to restrict to (empty = all). Set by a
    # profile, the --web-source CLI flag, or the WEB_SOURCE_NAMES env var.
    selected_names: list[str] = field(default_factory=list)
    # How many parallel UCBrowser instances to run for non-Cloudflare
    # ("standard") web sources. Each costs ~200-300MB; Cloudflare-protected
    # sources (native_chrome=True) stay serial regardless. 1 = single
    # browser (old behavior).
    parallel: int = 3
    # When True, record every web page session (navigation, popups, XHR
    # for feed mode) to data/.web_traces/<source>/<run-id>.jsonl via
    # NavigationTracer. Set via --debug-web-trace CLI flag or
    # WEB_DEBUG_TRACE=1 env var.
    debug_trace: bool = False
    # When True, capture per-step PNG screenshots alongside the JSONL
    # trace (and assemble a scroll-sequence GIF for feed sources). Set
    # via --debug-web-screenshots CLI flag or WEB_DEBUG_SCREENSHOTS=1
    # env var. Implies debug_trace=True — see load_config().
    debug_screenshots: bool = False
    # The Universal Controller service (EH_UC_SERVICE_URL, e.g.
    # http://127.0.0.1:4010). event_tool names it; nothing in the controller
    # library reads the environment to find it. Used by the Grok MCP tools,
    # and by the native web route when a window is named too
    # (WEB_UC_NATIVE_WINDOW, e.g. native): the native route then attaches to
    # that long-lived window. With no window named, the native route launches
    # its own Chrome or attaches to one already open on its port, as before.
    # Each is a different access method and is logged separately.
    uc_service: str = ""
    uc_native_window: str = ""


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


# What an interactive `event-harvester` run falls back to — the ceilings that
# were the global default until 2026-08-11. See `CapConfig.apply_cli_defaults`.
CLI_DEFAULT_CAPS = {
    "discord": 50, "telegram": 50, "gmail": 30,
    "signal": 30, "web": 30, "total": 150,
}


@dataclass
class CapConfig:
    """Per-source caps applied at the analysis stage.

    Messages are scored first, then capped per source, then merged up to
    the global `total` ceiling. Caps prevent any single noisy source from
    crowding out higher-signal events from other sources.
    """
    # **Unlimited by default since 2026-08-11.** -1 means "no ceiling for this
    # bucket"; 0 means "drop this source entirely", which is a different and
    # still-useful thing.
    #
    # The old defaults (50/50/30/30/30, total 150) were the pipeline's binding
    # constraint and were losing whole sources silently. Measured: caps
    # discarded ~98% of what the classifier passed, and because every web
    # sub-source shares one `web` bucket, luma's 60 messages took all 30 web
    # slots on their own — erobay fetched **121 messages every run for weeks
    # and produced no events**, with nothing in any log saying so.
    #
    # The decision (2026-08-11) is that dropping candidates belongs at the
    # *view*, not the pipeline: the site can filter, search and sort, and it
    # cannot show an event that was discarded before extraction.
    #
    # The cost is real and lands on the LLM stage — see `prioritize_with_caps`,
    # which logs the count being sent so an expensive run is visible rather
    # than surprising. Dial a bucket down from `/admin` when that matters.
    discord: int = -1
    telegram: int = -1
    gmail: int = -1
    signal: int = -1
    web: int = -1
    total: int = -1            # global ceiling after per-source caps; -1 = none
    group_by_source: bool = False  # if True, output is bucketed by source
    no_cap: bool = False       # if True, all per-source and total caps are bypassed

    def apply_cli_defaults(self) -> None:
        """Restore the pre-2026-08-11 ceilings for an interactive CLI run.

        **The CLI keeps caps; the long-running surfaces do not.** They are
        different jobs. A scheduled harvest feeding the site should collect
        everything and let the page filter it — the site cannot show an event
        that was discarded before extraction. Somebody typing
        `event-harvester --days 7` wants a bounded run with a bounded bill.

        Only fields still at the unlimited default are touched, so an explicit
        `--cap`, a profile, a `/admin` overlay or a `CAP_*` env var all keep
        their value.
        """
        for key, value in CLI_DEFAULT_CAPS.items():
            if getattr(self, key, None) == -1:
                setattr(self, key, value)

    def get(self, platform: str) -> int:
        """Look up cap for a platform name (case-insensitive).

        Unknown platforms fall back to the web cap rather than the global
        total, since web_fetch tags messages with sub-source names
        (luma, instagram, eventbrite, ...) that aren't core attributes
        but should still respect a per-source ceiling.
        """
        return getattr(self, platform.lower(), self.web)


@dataclass
class OCRConfig:
    """Flyer OCR via the infra's GLM-OCR Triton service.

    Off by default: it depends on an endpoint that is not always reachable
    (blackbox, ZeroTier/LAN-gated -- see memory `glm-ocr-endpoint`), and OCR is
    additive, so absence must cost nothing. Enable with OCR_ENABLED=1.
    """
    enabled: bool = False
    url: str = ""              # empty -> glm_ocr.DEFAULT_OCR_URL
    timeout: float = 120.0
    max_images: int = 4       # per message; a gallery post must not blow the budget


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
    ocr: OCRConfig = field(default_factory=OCRConfig)
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


logger = logging.getLogger("event_harvester.config")


def _env_flag(key: str, *, default: bool = False) -> bool:
    """A boolean from the environment, with an explicit default.

    Unset means the default — which matters for `LLM_VERIFY_SSL`, where the
    safe value is on and the existing `in ("1","true","yes")` idiom would
    silently make it off.
    """
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
    """A bounded integer. A junk or out-of-range value is the default, logged."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %d", key, raw, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning("%s=%d is outside %d-%d; using %d",
                       key, value, minimum, maximum, default)
        return default
    return value


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

    # API key resolution: prefer OPENAI_API_KEY for backward compat, but
    # fall back to ANTHROPIC_API_KEY so Anthropic-only users get the
    # sk-ant- auto-detection inside vision_completion. Either env var is
    # also accepted by litellm directly when called.
    llm_api_key = os.getenv("OPENAI_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")

    vlm_max_raw = os.getenv("VLM_MAX_IMAGES_PER_RUN", "").strip()
    if vlm_max_raw:
        try:
            vlm_max_images = int(vlm_max_raw)
        except ValueError:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "VLM_MAX_IMAGES_PER_RUN=%r is not an int; keeping default 10",
                vlm_max_raw,
            )
            vlm_max_images = 10
    else:
        vlm_max_images = 10

    cfg = AppConfig(
        telegram=TelegramConfig(
            api_id=api_id,
            api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            phone=os.getenv("TELEGRAM_PHONE", ""),
            session=os.getenv("TELEGRAM_SESSION", "harvest_session"),
            rolling_window=_env_flag("TELEGRAM_ROLLING_WINDOW", default=True),
            overlap_hours=float(os.getenv("TELEGRAM_OVERLAP_HOURS", "6") or 6),
        ),
        discord=DiscordConfig(
            cache_path=os.getenv("DISCORD_CACHE_PATH"),
        ),
        gmail=GmailConfig(
            credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
            token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
            query=os.getenv("GMAIL_QUERY", ""),
            max_results=int(os.getenv("GMAIL_MAX_RESULTS", "200")),
            # Both default ON/eligible; the env vars exist to turn them back
            # off, so absence must read as the default, not as False.
            fetch_bodies=os.getenv("GMAIL_FETCH_BODIES", "1").lower()
            in ("1", "true", "yes"),
            body_max_chars=int(os.getenv("GMAIL_BODY_MAX_CHARS", "5000")),
            drop_read=os.getenv("GMAIL_DROP_READ", "").lower() in ("1", "true", "yes"),
            expand_links=os.getenv("GMAIL_EXPAND_LINKS", "1").lower()
            in ("1", "true", "yes"),
            expand_max=int(os.getenv("GMAIL_EXPAND_MAX", "40")),
            events_label=os.getenv("GMAIL_EVENTS_LABEL", "events"),
        ),
        signal=SignalConfig(
            db_path=os.getenv("SIGNAL_DB_PATH"),
        ),
        llm=LLMConfig(
            model=os.getenv("LLM_MODEL", "qwen3-8b"),
            # The primary is served by the proxy, which has its own virtual
            # key — not the provider key `llm_api_key` holds. Falls back to it
            # so a plain `LLM_MODEL=gpt-4o-mini` setup still works unchanged.
            api_key=os.getenv("LLM_API_KEY") or llm_api_key,
            backend=os.getenv("LLM_BACKEND", "litellm"),
            api_base=os.getenv("LLM_API_BASE", ""),
            verify_ssl=_env_flag("LLM_VERIFY_SSL", default=True),
            fallback_model=os.getenv("LLM_FALLBACK_MODEL", ""),
            fallback_api_key=os.getenv("LLM_FALLBACK_API_KEY") or os.getenv(
                "LLM_ZT_KEY", ""),
            fallback_api_base=os.getenv("LLM_FALLBACK_API_BASE", "")
            or os.getenv("LLM_API_BASE", ""),
            batch_size=_env_int("LLM_BATCH_SIZE", 5, minimum=1, maximum=50),
            model_path=os.getenv("LLM_MODEL_PATH", ""),
            tokenizer_path=os.getenv("LLM_TOKENIZER_PATH", ""),
            device=os.getenv("LLM_DEVICE", "npu"),
            vision_model=os.getenv("VLM_MODEL", ""),
            vision_max_images_per_run=vlm_max_images,
            triton_url=os.getenv("TRITON_URL", "").strip(),
            triton_model=os.getenv("TRITON_MODEL", "").strip(),
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
            selected_names=_parse_csv_env("WEB_SOURCE_NAMES"),
            parallel=max(1, int(os.getenv("WEB_PARALLEL", "3"))),
            debug_trace=os.getenv("WEB_DEBUG_TRACE", "").lower() in ("1", "true", "yes"),
            debug_screenshots=os.getenv("WEB_DEBUG_SCREENSHOTS", "").lower() in ("1", "true", "yes"),
            uc_service=os.getenv("EH_UC_SERVICE_URL", "").strip(),
            uc_native_window=os.getenv("WEB_UC_NATIVE_WINDOW", "").strip(),
        ),
        caps=CapConfig(
            # -1 = unlimited. Set a CAP_* env var to put a ceiling back
            # everywhere, including the scheduled site harvest.
            discord=int(os.getenv("CAP_DISCORD", "-1")),
            telegram=int(os.getenv("CAP_TELEGRAM", "-1")),
            gmail=int(os.getenv("CAP_GMAIL", "-1")),
            signal=int(os.getenv("CAP_SIGNAL", "-1")),
            web=int(os.getenv("CAP_WEB", "-1")),
            total=int(os.getenv("CAP_TOTAL", "-1")),
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
        ocr=OCRConfig(
            enabled=_env_flag("OCR_ENABLED"),
            url=os.getenv("GLM_OCR_URL", "").strip(),
            timeout=float(os.getenv("OCR_TIMEOUT", "120")),
            max_images=_env_int("OCR_MAX_IMAGES", 4, minimum=1, maximum=20),
        ),
        days_back=days_back,
        telegram_channels=_parse_csv_env("TELEGRAM_CHANNELS"),
        telegram_exclude=_parse_csv_env("TELEGRAM_EXCLUDE"),
    )
    # Screenshots imply trace — the JSONL is where the image refs live,
    # so capturing PNGs without recording the trace would lose them.
    if cfg.web.debug_screenshots:
        cfg.web.debug_trace = True
    apply_caps_overlay(cfg)
    return cfg


# ── Caps overlay (UI-writable) ───────────────────────────────────────
#
# Caps are the pipeline's binding constraint — measured 2026-08-07 they
# discarded ~98% of what the classifier passed — so they are the one setting
# worth tuning without editing `.env` and restarting. `/admin` writes this
# file; env vars remain the source of truth for anything it does not set, so
# deleting the file restores env/default behaviour exactly.

CAPS_FILE = Path("data/settings.json")
_CAP_FIELDS = ("discord", "telegram", "gmail", "signal", "web", "total")
CAP_MAX = 5000  # a typo here costs real money at the LLM stage
CAP_UNLIMITED = -1


def _clamp_cap(value) -> int:
    """Bound a cap, preserving -1 as "unlimited".

    Any negative value normalises to exactly -1 rather than clamping to 0:
    `prioritize_with_caps` tests `cap < 0`, so -5 already meant unlimited, and
    silently turning it into "drop everything from this source" would be the
    worst possible misreading of an operator's typo.
    """
    n = int(value)
    if n < 0:
        return CAP_UNLIMITED
    return min(CAP_MAX, n)


def load_caps_overlay(path: Path | None = None) -> dict[str, int]:
    """Read UI-set caps. Unreadable or absent means "no overlay", never an error."""
    p = Path(path or CAPS_FILE)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    caps = raw.get("caps") if isinstance(raw, dict) else None
    if not isinstance(caps, dict):
        return {}
    out: dict[str, int] = {}
    for k in _CAP_FIELDS:
        if k in caps:
            try:
                out[k] = _clamp_cap(caps[k])
            except (TypeError, ValueError):
                continue
    return out


def save_caps_overlay(caps: dict, path: Path | None = None) -> dict[str, int]:
    """Validate and write atomically. Returns what was actually stored."""
    clean: dict[str, int] = {}
    for k in _CAP_FIELDS:
        if k in caps:
            clean[k] = _clamp_cap(caps[k])
    p = Path(path or CAPS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    try:
        existing = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["caps"] = clean
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return clean


def apply_caps_overlay(cfg, path: Path | None = None) -> None:
    for k, v in load_caps_overlay(path).items():
        setattr(cfg.caps, k, v)


def _llm_warnings(llm: LLMConfig) -> list[str]:
    """What is wrong with the LLM setup, in the order it will bite.

    Separated from `validate_config` because the LLM stage now has four ways to
    be misconfigured and only one of them used to be checked. Each warning
    names the variable to set — a warning that says "configure the LLM" makes
    the reader go and read this file.
    """
    out: list[str] = []
    if llm.backend == "local":
        if not llm.model_path:
            out.append(
                "LLM: LLM_BACKEND=local but LLM_MODEL_PATH is unset - analysis "
                "will be skipped. Point it at an .onnx model, or set "
                "LLM_BACKEND=litellm."
            )
        return out

    if not llm.model:
        out.append("LLM: LLM_MODEL is unset - analysis will be skipped.")
        return out

    if not (llm.api_key or llm.api_base):
        out.append(
            f"LLM: {llm.model!r} has neither a key nor an endpoint - analysis "
            "will be skipped. Set LLM_API_KEY (or OPENAI_API_KEY), or "
            "LLM_API_BASE for a local server."
        )
    elif llm.needs_endpoint:
        # The failure this prevents is a 404 from OpenAI for a model only a
        # local server has ever heard of, arriving mid-harvest.
        out.append(
            f"LLM: {llm.model!r} is not a name any public provider serves and "
            "LLM_API_BASE is unset, so it will be sent to OpenAI and rejected. "
            "Set LLM_API_BASE to the server hosting it."
        )

    if llm.fallback_model and not llm.fallback_is_configured:
        out.append(
            f"LLM: fallback {llm.fallback_model!r} has no credentials of its "
            "own and none to inherit. It will fail exactly when it is needed. "
            "Set LLM_FALLBACK_API_KEY."
        )
    # Deliberately no warning for an *absent* fallback. One model is a valid
    # configuration, and a warning that fires on every run is one the reader
    # learns to skip past — which costs more than it saves the first time
    # something is actually wrong.
    if llm.api_base and not llm.verify_ssl:
        out.append(
            f"LLM: TLS verification is off for {llm.api_base} (LLM_VERIFY_SSL). "
            "Intended for a self-signed endpoint you control."
        )
    return out


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

    if need_analysis:
        warnings.extend(_llm_warnings(cfg.llm))

    if need_ticktick and not cfg.ticktick.is_configured:
        warnings.append(
            "TickTick: Missing one or more of TICKTICK_CLIENT_ID, TICKTICK_CLIENT_SECRET, "
            "TICKTICK_USERNAME, TICKTICK_PASSWORD - task creation will be skipped."
        )

    return warnings
