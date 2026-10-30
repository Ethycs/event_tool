"""Ask Triton directly about the model behind the proxy, and load it.

The primary model (`qwen3-8b`) is served by Triton on blackbox behind the
litellm proxy on central-dev. Triton runs with explicit model control: after
the container restarts nothing is loaded until someone issues a load, and the
proxy cannot do that -- it only forwards inference. So a Triton restart turns
every primary call into a 500 ("Request for unknown model ... is not found")
and `llm.chat_completion` quietly falls back to the cloud model. That is what
happened from 2026-10-26 to 2026-10-29: three days of harvests on the fallback,
and nothing said so.

This module is the harvester's answer. `status()` says whether Triton is up
and the model is loaded, `ensure_loaded()` loads it when it is not, and
`looks_unloaded()` recognises the proxy's error so the completion path can
heal itself before falling back. Triton is reached over the mesh
(`TRITON_URL`), not through the proxy.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from event_harvester.config import LLMConfig
from event_harvester.observability.metrics import count

logger = logging.getLogger("event_harvester.triton")

# Tests point this at an httpx.MockTransport.
_TRANSPORT: httpx.BaseTransport | None = None

# A load that failed is not retried for this long. The proxy keeps failing in
# the meantime and the fallback carries the harvest, which is the intended
# degraded mode. Without this a harvest of 200 batches would issue 200 loads.
_RETRY_AFTER_S = 60.0
_last_failed_at: float = 0.0

# What litellm's TritonException reads like when the model is not loaded.
_UNLOADED_MARKERS = (
    "is not found",          # Request for unknown model: 'x' is not found
    "unknown model",
    "not ready",             # Model version not ready
    "model is not available",
    "unavailable",
)


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=_TRANSPORT)


def looks_unloaded(error: BaseException) -> bool:
    """True when a proxy error reads like Triton has no such model loaded."""
    text = str(error).lower()
    return any(marker in text for marker in _UNLOADED_MARKERS)


def status(cfg: LLMConfig, *, timeout: float = 5.0) -> dict[str, Any]:
    """Is Triton up, and is the model loaded? Never raises."""
    out: dict[str, Any] = {
        "configured": cfg.triton_is_configured,
        "url": cfg.triton_url,
        "model": cfg.triton_model,
        "server_ready": None,
        "model_ready": None,
        "error": None,
    }
    if not cfg.triton_is_configured:
        return out
    base = cfg.triton_url.rstrip("/")
    try:
        with _client(timeout) as client:
            r = client.get(f"{base}/v2/health/ready")
            out["server_ready"] = r.status_code == 200
            r = client.get(f"{base}/v2/models/{cfg.triton_model}/ready")
            out["model_ready"] = r.status_code == 200
            if r.status_code != 200:
                out["error"] = r.text.strip()[:200] or f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def ensure_loaded(
    cfg: LLMConfig, *, timeout: float = 180.0, poll: float = 3.0
) -> dict[str, Any]:
    """Load the model if Triton does not have it loaded. Never raises.

    Returns ``{"loaded", "action", "seconds", "error"}`` where ``action`` is
    ``already`` (nothing to do), ``loaded``, ``failed`` or ``skipped`` (not
    configured, or a recent failure is still inside its back-off window).
    """
    global _last_failed_at
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "loaded": False, "action": "skipped", "seconds": 0.0, "error": None,
    }
    if not cfg.triton_is_configured:
        result["error"] = "TRITON_URL / TRITON_MODEL not set"
        return result

    st = status(cfg, timeout=min(timeout, 10.0))
    if st["model_ready"]:
        result.update(loaded=True, action="already")
        return _finish(result, t0, cfg)
    if st["server_ready"] is not True:
        result.update(action="failed", error=st["error"] or "Triton not ready")
        _last_failed_at = time.monotonic()
        return _finish(result, t0, cfg)
    since_fail = time.monotonic() - _last_failed_at
    if _last_failed_at and since_fail < _RETRY_AFTER_S:
        result["error"] = (
            f"load failed {since_fail:.0f}s ago; retry after {_RETRY_AFTER_S:.0f}s"
        )
        return _finish(result, t0, cfg)

    base = cfg.triton_url.rstrip("/")
    logger.warning("Triton has %r unloaded; loading it via %s", cfg.triton_model, base)
    try:
        with _client(timeout) as client:
            # The load call blocks until Triton has loaded (or rejected) the
            # model, so the long timeout belongs here; the poll is a formality.
            r = client.post(
                f"{base}/v2/repository/models/{cfg.triton_model}/load", json={}
            )
            if r.status_code != 200:
                result.update(
                    action="failed",
                    error=f"load HTTP {r.status_code}: {r.text.strip()[:200]}",
                )
                _last_failed_at = time.monotonic()
                return _finish(result, t0, cfg)
            deadline = t0 + timeout
            while time.monotonic() < deadline:
                ready = client.get(f"{base}/v2/models/{cfg.triton_model}/ready")
                if ready.status_code == 200:
                    result.update(loaded=True, action="loaded")
                    _last_failed_at = 0.0
                    return _finish(result, t0, cfg)
                time.sleep(poll)
            result.update(action="failed", error=f"not ready after {timeout:.0f}s")
    except httpx.HTTPError as e:
        result.update(action="failed", error=f"{type(e).__name__}: {e}"[:200])
    _last_failed_at = time.monotonic()
    return _finish(result, t0, cfg)


def _finish(result: dict[str, Any], t0: float, cfg: LLMConfig) -> dict[str, Any]:
    result["seconds"] = round(time.monotonic() - t0, 1)
    count("llm.triton_load", model=cfg.triton_model, action=result["action"],
          description="Attempts to load the primary model on Triton")
    if result["action"] == "loaded":
        logger.warning("Triton loaded %r in %.1fs", cfg.triton_model, result["seconds"])
    elif result["action"] == "failed":
        logger.error("Triton load of %r failed: %s", cfg.triton_model, result["error"])
    return result
