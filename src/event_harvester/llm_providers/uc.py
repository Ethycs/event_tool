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

Blind / fire-and-forget mode:

Pass ``extra_body={"wait_for_response": False}`` to submit the message
without waiting for Grok to generate a reply. The completion returns as
soon as the URL settles to ``/c/<uuid>``. ``message.content`` will be
empty; the conversation_url is still surfaced on ``_hidden_params`` so
callers can read it later with a separate call. Useful for batching
many prompts when the caller doesn't need to parse each one.

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
from typing import Any, AsyncIterator, Iterator, Optional

import litellm
from litellm import CustomLLM
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage

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


def _from_extras(optional_params: dict, key: str):
    """Pull ``key`` from ``optional_params`` regardless of nesting.

    Direct ``litellm.completion(extra_body={...})`` preserves the dict
    nested under ``optional_params["extra_body"]``. The litellm proxy
    flattens ``extra_body`` entries into top-level ``optional_params``
    keys. Check both shapes so the same handler covers both callers.
    """
    op = optional_params or {}
    eb = op.get("extra_body") or {}
    if isinstance(eb, dict) and key in eb:
        return eb[key]
    if key in op:
        return op[key]
    return None


def _resolve_session_key(
    litellm_params: dict | None,
    optional_params: dict,
) -> Optional[str]:
    sid = _from_extras(optional_params, "session_id")
    if sid:
        return str(sid)
    sid = (litellm_params or {}).get("litellm_session_id")
    return str(sid) if sid else None


def _resolve_conversation_url(optional_params: dict) -> Optional[str]:
    url = _from_extras(optional_params, "conversation_url")
    return str(url) if url else None


def _resolve_wait_for_response(optional_params: dict) -> bool:
    v = _from_extras(optional_params, "wait_for_response")
    if v is None:
        return True
    return bool(v)


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

        # Note: stream=True doesn't reach this method — litellm routes
        # streaming requests to ``streaming``/``astreaming`` below.

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
        wait_for_response = _resolve_wait_for_response(optional_params)

        conversation_url = explicit_url
        if conversation_url is None and session_key is not None:
            conversation_url = self.get_session_url(session_key)

        try:
            result = get_grok_client().send(
                prompt_text,
                conversation_url=conversation_url,
                timeout_s=timeout_s,
                wait_for_response=wait_for_response,
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

    # ── Streaming (fake) ─────────────────────────────────────────────
    #
    # GrokClient.send() is anchor-locked / blocking — it doesn't expose
    # token-level streaming. To stay compatible with clients that require
    # ``stream=True`` (Cline, Open WebUI, some Cursor flows) we emit the
    # full response as a single SSE content chunk followed by a final
    # ``is_finished=True`` chunk. This is the "fake streaming" pattern.

    def _stream_chunks(self, response: ModelResponse) -> Iterator[GenericStreamingChunk]:
        content = ""
        try:
            content = response.choices[0].message.content or ""
        except Exception:
            content = ""
        usage_block = None
        if getattr(response, "usage", None) is not None:
            try:
                usage_block = {
                    "prompt_tokens": int(response.usage.prompt_tokens or 0),
                    "completion_tokens": int(response.usage.completion_tokens or 0),
                    "total_tokens": int(response.usage.total_tokens or 0),
                }
            except Exception:
                usage_block = None
        yield GenericStreamingChunk(
            text=content,
            tool_use=None,
            is_finished=False,
            finish_reason="",
            usage=None,
            index=0,
        )
        yield GenericStreamingChunk(
            text="",
            tool_use=None,
            is_finished=True,
            finish_reason="stop",
            usage=usage_block,
            index=0,
        )

    def streaming(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> Iterator[GenericStreamingChunk]:
        response = self.completion(
            model=model,
            messages=messages,
            model_response=model_response,
            optional_params=optional_params,
            litellm_params=litellm_params,
            timeout=timeout,
            **kwargs,
        )
        yield from self._stream_chunks(response)

    async def astreaming(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[GenericStreamingChunk]:
        response = await self.acompletion(
            model=model,
            messages=messages,
            model_response=model_response,
            optional_params=optional_params,
            litellm_params=litellm_params,
            timeout=timeout,
            **kwargs,
        )
        for chunk in self._stream_chunks(response):
            yield chunk


# ── Registration ─────────────────────────────────────────────────────


# Module-level singleton — eagerly instantiated so the litellm proxy can
# import it by dotted path (``event_harvester.llm_providers.uc.uc_handler``)
# in its ``custom_provider_map`` config. Construction has no side effects;
# the underlying browser doesn't open until the first completion call.
uc_handler: "UCBrowserCustomLLM" = UCBrowserCustomLLM()

_registered_handler: UCBrowserCustomLLM | None = None
_register_lock = threading.Lock()


def register_uc_provider() -> UCBrowserCustomLLM:
    """Attach :data:`uc_handler` to ``litellm.custom_provider_map``.

    Idempotent — re-calling is a no-op. Direct-Python callers use this
    to enable ``litellm.completion(model="uc/grok", ...)``. The litellm
    proxy wires the same handler via its config file's
    ``custom_provider_map`` and doesn't need this call.
    """
    global _registered_handler
    with _register_lock:
        if _registered_handler is not None:
            return _registered_handler
        existing = list(litellm.custom_provider_map or [])
        # Drop any prior entry for the same provider key — keeps the
        # map well-formed if some other code wired its own handler.
        existing = [e for e in existing if e.get("provider") != PROVIDER]
        existing.append({"provider": PROVIDER, "custom_handler": uc_handler})
        litellm.custom_provider_map = existing
        _registered_handler = uc_handler
        logger.info("Registered UCBrowserCustomLLM under provider %r.", PROVIDER)
        return uc_handler


def get_uc_handler() -> UCBrowserCustomLLM | None:
    """Return the registered handler, or ``None`` if not yet registered."""
    return _registered_handler


def _reset_for_tests() -> None:
    """Clear the registration + drop cached sessions on the shared
    ``uc_handler``. Test-only — needed because the handler is a
    module-level singleton."""
    global _registered_handler
    with _register_lock:
        _registered_handler = None
        litellm.custom_provider_map = [
            e for e in (litellm.custom_provider_map or [])
            if e.get("provider") != PROVIDER
        ]
    uc_handler.clear_sessions()
