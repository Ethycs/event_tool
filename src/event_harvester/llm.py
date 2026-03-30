"""Unified LLM interface — dispatches to litellm or local RyzenAI.

``analysis.py`` calls ``chat_completion()`` instead of importing litellm
directly.  The backend is chosen by ``LLMConfig.backend``.
"""

from __future__ import annotations

import logging
from typing import Any

from event_harvester.config import LLMConfig

logger = logging.getLogger("event_harvester.llm")

# Lazy-loaded session for the local backend (kept alive across calls).
_local_session: Any = None
_local_cfg_key: str | None = None


def _get_local_session(cfg: LLMConfig):
    """Return a long-lived RyzenSession, creating it on first call."""
    global _local_session, _local_cfg_key

    key = f"{cfg.model_path}:{cfg.device}"
    if _local_session is not None and _local_cfg_key == key:
        return _local_session

    from event_harvester.interlock import RyzenSession

    sess = RyzenSession(
        cfg.model_path,
        device=cfg.device,
        tokenizer_path=cfg.tokenizer_path,
    )
    sess.__enter__()
    _local_session = sess
    _local_cfg_key = key
    return sess


def shutdown_local() -> None:
    """Shut down the local RyzenSession if one is active."""
    global _local_session, _local_cfg_key
    if _local_session is not None:
        _local_session.__exit__(None, None, None)
        _local_session = None
        _local_cfg_key = None


def chat_completion(
    messages: list[dict[str, str]],
    cfg: LLMConfig,
    *,
    max_tokens: int = 2048,
    response_format: dict[str, str] | None = None,
) -> str:
    """Send chat messages and return the assistant's reply as a string.

    Routes to litellm (``backend="litellm"``) or local ONNX inference
    (``backend="local"``).
    """
    if cfg.backend == "local":
        return _complete_local(messages, cfg, max_tokens=max_tokens)
    return _complete_litellm(messages, cfg, max_tokens=max_tokens, response_format=response_format)


def _suppress_litellm_noise():
    """Suppress litellm's noisy provider list warnings."""
    import litellm
    litellm.suppress_debug_info = True
    litellm._turn_on_debug = lambda: None  # no-op


_litellm_init = False


def _complete_litellm(
    messages: list[dict[str, str]],
    cfg: LLMConfig,
    *,
    max_tokens: int,
    response_format: dict[str, str] | None,
) -> str:
    global _litellm_init
    if not _litellm_init:
        _suppress_litellm_noise()
        _litellm_init = True

    from litellm import completion

    model = cfg.litellm_model
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    # Minimize reasoning for reasoning models —
    # event extraction is a simple task that doesn't need chain-of-thought.
    # GPT-5.4 supports "none"; GPT-5 supports "minimal"; others may vary.
    if "gpt-5.4" in model:
        kwargs["reasoning_effort"] = "none"
    elif any(tag in model for tag in ("gpt-5", "o1-", "o3-")):
        kwargs["reasoning_effort"] = "minimal"

    resp = completion(**kwargs)
    content = resp.choices[0].message.content or ""

    # Reasoning models (GPT-5, o1, o3) may exhaust tokens on reasoning
    # with empty content. Log a warning so the caller knows.
    if not content and resp.choices[0].finish_reason == "length":
        logger.warning(
            "LLM returned empty content (finish_reason=length, used %d tokens on reasoning). "
            "Increase max_tokens.",
            resp.usage.completion_tokens if resp.usage else 0,
        )

    return content


def _complete_local(
    messages: list[dict[str, str]],
    cfg: LLMConfig,
    *,
    max_tokens: int,
) -> str:
    try:
        sess = _get_local_session(cfg)
        result = sess.generate(messages, max_tokens=max_tokens)
    except Exception:
        # Session crashed — restart and retry once.
        logger.warning("Local session crashed, restarting...")
        shutdown_local()
        sess = _get_local_session(cfg)
        result = sess.generate(messages, max_tokens=max_tokens)

    logger.info(
        "Local generation: %d tokens in %.2fs",
        result["tokens_generated"],
        result["elapsed_s"],
    )
    return result["text"]
