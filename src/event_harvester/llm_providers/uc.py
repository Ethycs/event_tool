"""litellm CustomLLM that drives browser-automated chats (Grok today).

Register once with :func:`register_uc_provider`, then call litellm with
``model="uc/grok"``. The handler holds a session store mapping a
caller-supplied id to a Grok conversation URL so subsequent calls
continue the same chat instead of starting a fresh one.

Session key resolution priority (first hit wins):

1. ``extra_body["session_id"]`` — most explicit.
2. ``extra_body["conversation_url"]`` — bypass the store entirely.
3. ``metadata["session_id"]`` — surfaces in ``litellm_session_id``.

If none of those are passed, every call creates a new conversation.

Limitations:

* No streaming — ``stream=True`` raises ``BadRequestError``.
* Token counts are length-based estimates (browser-driven; no real
  tokenizer output available).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

import litellm
from litellm import CustomLLM
from litellm.types.utils import ModelResponse, Usage

from event_harvester.sites.grok import GrokAuthRequired, get_grok_client

logger = logging.getLogger("event_harvester.llm_providers.uc")

PROVIDER = "uc"
SUPPORTED_MODELS: set[str] = {"grok"}


# ── Helpers ──────────────────────────────────────────────────────────


def _last_user_message(messages: list[dict]) -> str:
    """Return the text of the most recent ``role=user`` message."""
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # OpenAI multimodal — concatenate text parts.
            texts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(t for t in texts if t)
    return ""


def _extra_body(optional_params: dict) -> dict:
    eb = (optional_params or {}).get("extra_body") or {}
    return eb if isinstance(eb, dict) else {}


def _resolve_session_key(
    litellm_params: dict | None,
    optional_params: dict,
) -> Optional[str]:
    eb = _extra_body(optional_params)
    if eb.get("session_id"):
        return str(eb["session_id"])
    sid = (litellm_params or {}).get("litellm_session_id")
    return str(sid) if sid else None


def _resolve_conversation_url(optional_params: dict) -> Optional[str]:
    eb = _extra_body(optional_params)
    url = eb.get("conversation_url")
    return str(url) if url else None


def _populate_response(
    *,
    model_response: ModelResponse,
    model_name: str,
    content: str,
    conversation_id: Optional[str],
    conversation_url: Optional[str],
    prompt_text: str,
) -> ModelResponse:
    """Fill the pre-allocated ModelResponse litellm hands us."""
    model_response.choices[0].message.content = content
    model_response.choices[0].finish_reason = "stop"
    model_response.model = model_name
    model_response.created = int(time.time())
    # Rough estimate — no tokenizer in play for browser-driven chat.
    pt = max(1, len(prompt_text) // 4)
    ct = max(1, len(content) // 4)
    model_response.usage = Usage(
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=pt + ct,
    )
    hp = getattr(model_response, "_hidden_params", None) or {}
    hp["uc_conversation_id"] = conversation_id
    hp["uc_conversation_url"] = conversation_url
    model_response._hidden_params = hp
    return model_response


# ── CustomLLM ────────────────────────────────────────────────────────


class UCBrowserCustomLLM(CustomLLM):
    """litellm CustomLLM that delegates ``uc/<site>`` to a site client."""

    def __init__(self) -> None:
        super().__init__()
        self._sessions: dict[str, str] = {}
        self._sessions_lock = threading.Lock()

    # ── Session store (public for inspection / tests) ────────────────

    def get_session_url(self, session_key: str) -> Optional[str]:
        with self._sessions_lock:
            return self._sessions.get(session_key)

    def set_session_url(self, session_key: str, url: str) -> None:
        with self._sessions_lock:
            self._sessions[session_key] = url

    def forget_session(self, session_key: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(session_key, None)

    def clear_sessions(self) -> None:
        with self._sessions_lock:
            self._sessions.clear()

    # ── litellm CustomLLM API ────────────────────────────────────────

    def completion(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        # litellm strips the ``uc/`` prefix before calling us; ``model``
        # here is just the site name (e.g. "grok").
        site = (model or "").strip().lower()
        if site not in SUPPORTED_MODELS:
            raise litellm.exceptions.BadRequestError(
                message=(
                    f"UCBrowserCustomLLM: unsupported model {model!r}. "
                    f"Supported: {sorted(SUPPORTED_MODELS)}."
                ),
                model=model,
                llm_provider=PROVIDER,
            )

        if optional_params.get("stream"):
            raise litellm.exceptions.BadRequestError(
                message=(
                    "UCBrowserCustomLLM: streaming is not supported. "
                    "Pass stream=False (or omit it)."
                ),
                model=model,
                llm_provider=PROVIDER,
            )

        prompt_text = _last_user_message(messages)
        if not prompt_text:
            raise litellm.exceptions.BadRequestError(
                message="UCBrowserCustomLLM: no user message found in messages.",
                model=model,
                llm_provider=PROVIDER,
            )

        timeout_s = int(timeout) if isinstance(timeout, (int, float)) else 60
        session_key = _resolve_session_key(litellm_params, optional_params)
        explicit_url = _resolve_conversation_url(optional_params)

        conversation_url = explicit_url
        if conversation_url is None and session_key is not None:
            conversation_url = self.get_session_url(session_key)

        try:
            result = get_grok_client().send(
                prompt_text,
                conversation_url=conversation_url,
                timeout_s=timeout_s,
            )
        except GrokAuthRequired as exc:
            raise litellm.exceptions.AuthenticationError(
                message=f"Grok auth required: {exc}",
                model=model,
                llm_provider=PROVIDER,
            ) from exc

        if session_key is not None and result.get("url"):
            self.set_session_url(session_key, result["url"])

        return _populate_response(
            model_response=model_response,
            model_name=f"{PROVIDER}/{site}",
            content=result.get("response") or "",
            conversation_id=result.get("conversation_id"),
            conversation_url=result.get("url"),
            prompt_text=prompt_text,
        )

    async def acompletion(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        # GrokClient uses Playwright's sync API — run it off the event loop.
        return await asyncio.to_thread(
            self.completion,
            model=model,
            messages=messages,
            model_response=model_response,
            optional_params=optional_params,
            litellm_params=litellm_params,
            timeout=timeout,
            **kwargs,
        )


# ── Registration ─────────────────────────────────────────────────────


_registered_handler: UCBrowserCustomLLM | None = None
_register_lock = threading.Lock()


def register_uc_provider() -> UCBrowserCustomLLM:
    """Attach :class:`UCBrowserCustomLLM` to ``litellm.custom_provider_map``.

    Idempotent — re-calling returns the already-registered handler so
    its session store is preserved across module imports.
    """
    global _registered_handler
    with _register_lock:
        if _registered_handler is not None:
            return _registered_handler
        handler = UCBrowserCustomLLM()
        existing = list(litellm.custom_provider_map or [])
        # Drop any prior entry for the same provider key — keeps the
        # map well-formed if some other code wired its own handler.
        existing = [e for e in existing if e.get("provider") != PROVIDER]
        existing.append({"provider": PROVIDER, "custom_handler": handler})
        litellm.custom_provider_map = existing
        _registered_handler = handler
        logger.info("Registered UCBrowserCustomLLM under provider %r.", PROVIDER)
        return handler


def get_uc_handler() -> UCBrowserCustomLLM | None:
    """Return the registered handler, or ``None`` if not yet registered."""
    return _registered_handler


def _reset_for_tests() -> None:
    """Drop the registered handler so the next ``register_uc_provider``
    call constructs a fresh one. Test-only."""
    global _registered_handler
    with _register_lock:
        _registered_handler = None
        litellm.custom_provider_map = [
            e for e in (litellm.custom_provider_map or [])
            if e.get("provider") != PROVIDER
        ]
