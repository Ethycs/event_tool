"""Unified LLM interface — dispatches to litellm or local RyzenAI.

``analysis.py`` calls ``chat_completion()`` instead of importing litellm
directly.  The backend is chosen by ``LLMConfig.backend``.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from event_harvester.config import LLMConfig
from event_harvester.observability.metrics import count
from event_harvester.observability.tracing import set_span_attrs, traced

_TRACER = "event_harvester.llm"

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


@traced("llm.chat", tracer_name=_TRACER)
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
    set_span_attrs(**{
        "llm.backend": cfg.backend,
        "llm.model": cfg.model,
        "llm.max_tokens": max_tokens,
        "llm.message_count": len(messages),
        "llm.request_chars": sum(len(m.get("content") or "") for m in messages),
        "llm.structured_output": bool(response_format),
    })
    if cfg.backend == "local":
        out = _complete_local(messages, cfg, max_tokens=max_tokens)
    else:
        out = _complete_litellm(
            messages, cfg, max_tokens=max_tokens, response_format=response_format
        )
    set_span_attrs(**{"llm.response_chars": len(out), "llm.response_empty": not out})
    return out


def _suppress_litellm_noise():
    """Suppress litellm's noisy provider list warnings."""
    import litellm
    litellm.suppress_debug_info = True
    litellm._turn_on_debug = lambda: None  # no-op


_litellm_init = False


@traced("llm.litellm.completion", tracer_name=_TRACER)
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

    set_span_attrs(**{
        "llm.litellm_model": model,
        "llm.reasoning_effort": kwargs.get("reasoning_effort"),
    })

    resp = completion(**kwargs)
    content = resp.choices[0].message.content or ""
    finish_reason = resp.choices[0].finish_reason

    # Token counts are the only reliable cost signal we get back, and they are
    # otherwise invisible -- litellm logs nothing by default and the pipeline
    # discards the response object. Put them on the span.
    usage = getattr(resp, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
    set_span_attrs(**{
        "llm.finish_reason": finish_reason,
        "llm.prompt_tokens": prompt_tokens,
        "llm.completion_tokens": completion_tokens,
        "llm.total_tokens": getattr(usage, "total_tokens", None) if usage else None,
    })
    # ...and as counters, because "what did this run cost" is a span question
    # while "what are we spending" is not. A span tells you about one call; you
    # cannot sum spans without a backend query, and the number that matters is
    # a sum.
    count("llm.requests", model=model, finish_reason=finish_reason,
          description="LLM completion requests")
    if prompt_tokens is not None:
        count("llm.tokens", int(prompt_tokens), unit="{token}", model=model,
              direction="prompt", description="LLM tokens billed")
    if completion_tokens is not None:
        count("llm.tokens", int(completion_tokens), unit="{token}", model=model,
              direction="completion", description="LLM tokens billed")

    # Reasoning models (GPT-5, o1, o3) may exhaust tokens on reasoning
    # with empty content. Log a warning so the caller knows.
    if not content and finish_reason == "length":
        logger.warning(
            "LLM returned empty content (finish_reason=length, used %d tokens on reasoning). "
            "Increase max_tokens.",
            usage.completion_tokens if usage else 0,
        )

    return content


@traced("llm.local.generate", tracer_name=_TRACER)
def _complete_local(
    messages: list[dict[str, str]],
    cfg: LLMConfig,
    *,
    max_tokens: int,
) -> str:
    restarted = False
    try:
        sess = _get_local_session(cfg)
        result = sess.generate(messages, max_tokens=max_tokens)
    except Exception:
        # Session crashed — restart and retry once.
        logger.warning("Local session crashed, restarting...")
        restarted = True
        shutdown_local()
        sess = _get_local_session(cfg)
        result = sess.generate(messages, max_tokens=max_tokens)

    # The restart is silent apart from one WARNING line; on the span it shows
    # up as a first-class attribute, so a run that is quietly thrashing the
    # local worker is visible rather than inferred from log volume.
    set_span_attrs(**{
        "llm.local.restarted": restarted,
        "llm.local.tokens_generated": result.get("tokens_generated"),
        "llm.local.elapsed_s": result.get("elapsed_s"),
    })

    logger.info(
        "Local generation: %d tokens in %.2fs",
        result["tokens_generated"],
        result["elapsed_s"],
    )
    return result["text"]


def _resolve_vision_model(cfg: LLMConfig) -> str:
    """Pick the vision model: explicit cfg.vision_model wins, else auto-pick
    from the api_key prefix. ``cfg.vision_model`` may not exist on
    ``LLMConfig`` yet (added by another agent); ``getattr`` keeps this
    function robust until then.
    """
    explicit = getattr(cfg, "vision_model", "") or ""
    if explicit:
        return explicit
    if cfg.api_key.startswith("sk-ant-"):
        return "claude-sonnet-4-7"
    return "gpt-4o-mini"


@traced("llm.vision", tracer_name=_TRACER)
def vision_completion(
    prompt: str,
    image_paths: list[Path],
    cfg: LLMConfig,
    *,
    max_tokens: int = 1024,
) -> str:
    """Call a vision-capable LLM with one or more PNG images + a text prompt.

    Returns the assistant's text response. Raises on transport/API errors so
    the caller decides retry policy. Missing image files are logged and
    skipped (not fatal). Logs a warning if more than 5 images are sent.
    """
    global _litellm_init
    if not _litellm_init:
        _suppress_litellm_noise()
        _litellm_init = True

    if len(image_paths) > 5:
        logger.warning(
            "vision_completion: sending %d images (>5) — cost may be high",
            len(image_paths),
        )

    content: list[dict[str, Any]] = []
    image_bytes = 0
    missing = 0
    for path in image_paths:
        if not path.exists():
            logger.warning("vision_completion: image %s missing, skipping", path)
            missing += 1
            continue
        raw = path.read_bytes()
        image_bytes += len(raw)
        b64 = base64.b64encode(raw).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    content.append({"type": "text", "text": prompt})

    model = _resolve_vision_model(cfg)
    # Images dominate vision cost, and a silently-skipped one turns a quality
    # verdict into a guess -- both belong on the span, not just in the log.
    set_span_attrs(**{
        "llm.vision_model": model,
        "llm.max_tokens": max_tokens,
        "llm.image_count": len(image_paths),
        "llm.image_sent_count": len(content) - 1,
        "llm.image_missing_count": missing,
        "llm.image_bytes": image_bytes,
        "llm.prompt_chars": len(prompt),
    })
    logger.info(
        "vision_completion: model=%s images=%d",
        model,
        len(image_paths),
    )

    import litellm

    resp = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
    )
    usage = getattr(resp, "usage", None)
    out = resp.choices[0].message.content
    set_span_attrs(**{
        "llm.prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "llm.completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "llm.total_tokens": getattr(usage, "total_tokens", None) if usage else None,
        "llm.response_chars": len(out or ""),
    })
    return out
