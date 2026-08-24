"""GLM-OCR client — pull text off event-flyer images so extraction can read it.

Event invites are very often a flyer: the date, time and venue live *in the
image*, not in the message text. This calls the infra's GLM-OCR service so that
text reaches the pipeline.

**GLM-OCR is a Triton service, not an OpenAI-shaped one** (wiki:
`inference/inference-plane.md`), so it cannot go through litellm or
`llm.vision_completion`. It is a KServe v2 `infer` endpoint on blackbox:

    POST http://<blackbox>:8010/v2/models/glm-ocr/infer
    body: {"inputs":[{"name":"image","shape":[1],"datatype":"BYTES","data":["<b64>"]}]}
    out:  outputs[0].data[0] = JSON list of
          {"page":1,"bbox":[x0,y0,x1,y1],"kind":"text","text":"..."}

Reachability is the catch, recorded in memory `glm-ocr-endpoint`: the service is
bound to blackbox's ZeroTier interface and (as of 2026-08-23) the LAN ports
`.136:8010/:8000`. `GLM_OCR_URL` overrides the endpoint. HTTP, no auth, no TLS.

Verified live 2026-08-23: request/response contract holds. Note it can return
only a subset of a page's text on sparse synthetic images — real-flyer quality
is the model's own concern (its `model.py` on blackbox), not this client's; this
returns faithfully whatever regions the service emits.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

logger = logging.getLogger("event_harvester.glm_ocr")

#: Default endpoint. The LAN address blackbox exposes; override with GLM_OCR_URL
#: (e.g. the ZeroTier `http://10.158.218.141:8010/...` once this host is on ZT).
DEFAULT_OCR_URL = "http://192.168.1.136:8010/v2/models/glm-ocr/infer"


def _regions_to_text(regions: list[dict]) -> str:
    """Join region texts in reading order: page, then top-to-bottom, then left."""
    def _key(r: dict) -> tuple:
        bbox = r.get("bbox") or [0, 0, 0, 0]
        return (r.get("page", 1), bbox[1] if len(bbox) > 1 else 0,
                bbox[0] if bbox else 0)

    parts = [str(r.get("text", "")).strip()
             for r in sorted(regions, key=_key)]
    return "\n".join(p for p in parts if p)


def parse_regions(envelope: dict) -> list[dict]:
    """Pull the region list out of a Triton v2 infer response.

    The output tensor ``regions`` is a single BYTES element holding a JSON
    string. Returns ``[]`` for any shape that does not match, rather than
    raising — a malformed OCR reply must not sink a harvest.
    """
    for out in (envelope or {}).get("outputs", []):
        if out.get("name") != "regions":
            continue
        data = out.get("data") or []
        if not data:
            return []
        try:
            regions = json.loads(data[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning("glm_ocr: regions payload was not JSON")
            return []
        return regions if isinstance(regions, list) else []
    return []


def ocr_image_bytes(
    data: bytes,
    *,
    url: str | None = None,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> str:
    """OCR raw image bytes (PNG/JPG/…) and return the text, newline-joined.

    Returns ``""`` on any transport or decode failure — OCR enrichment is
    additive, so a failure means "no extra text", never a broken harvest.
    """
    if not data:
        return ""
    endpoint = url or DEFAULT_OCR_URL
    b64 = base64.b64encode(data).decode("ascii")
    body = {"inputs": [{"name": "image", "shape": [1],
                        "datatype": "BYTES", "data": [b64]}]}
    try:
        if client is not None:
            resp = client.post(endpoint, json=body, timeout=timeout)
        else:
            resp = httpx.post(endpoint, json=body, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 - additive feature, never fatal
        logger.warning("glm_ocr: request to %s failed: %s", endpoint, e)
        return ""
    return _regions_to_text(parse_regions(resp.json()))


def health(url: str | None = None, timeout: float = 5.0) -> bool:
    """Is the OCR service reachable and ready?"""
    endpoint = url or DEFAULT_OCR_URL
    ready = endpoint.split("/v2/")[0] + "/v2/health/ready"
    try:
        return httpx.get(ready, timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001
        return False
