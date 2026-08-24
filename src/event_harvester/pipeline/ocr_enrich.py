"""Enrich messages that carry images with GLM-OCR text, before extraction.

Event invites are frequently flyers: the date, time and venue are pixels, not
text. A message may declare an ``images`` list (URLs or local file paths — see
`EH-STD-MSG-v0`); this fetches each, OCRs it via `sources.glm_ocr`, and appends
the recovered text to ``content`` so the classifier, reranker and LLM all see
it. One place, every source — a source's only job is to populate ``images``.

Additive and non-fatal by construction: OCR off, an unreachable endpoint, a
dead image URL, a garbled reply — every one of these leaves the message exactly
as it was. The feature can only *add* signal, never remove a harvest.

Cached by image content hash (`data/.ocr_cache.json`), so a re-harvest never
re-OCRs an image it has already read — the same reason the reranker's date work
is worth caching: the cost is the network round-trip, and the input is stable.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from event_harvester.config import OCRConfig
from event_harvester.outputs.thumbnails import data_uri, encode_thumbnail
from event_harvester.sources import glm_ocr
from event_harvester.utils import data_path, load_json, save_json

logger = logging.getLogger("event_harvester.ocr_enrich")

CACHE_FILE = data_path(".ocr_cache.json")

#: A flyer is small; anything larger is a photo/video thumb unlikely to be an
#: invite and expensive to move. Skip it rather than pay for it.
MAX_IMAGE_BYTES = 8_000_000


def _fetch_image(ref: str, timeout: float) -> bytes:
    """Get image bytes from a URL or local path. Empty on any failure."""
    try:
        if ref.startswith(("http://", "https://")):
            r = httpx.get(ref, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            data = r.content
        else:
            from pathlib import Path
            p = Path(ref)
            data = p.read_bytes() if p.exists() else b""
    except Exception as e:  # noqa: BLE001 - additive; a bad image is just no text
        logger.debug("ocr: could not fetch %s: %s", ref, e)
        return b""
    if len(data) > MAX_IMAGE_BYTES:
        logger.debug("ocr: %s is %d bytes (>%d), skipping", ref, len(data), MAX_IMAGE_BYTES)
        return b""
    return data


def enrich_messages(messages: list[dict], cfg: OCRConfig) -> int:
    """OCR every image on every message and fold the text into ``content``.

    Returns the number of messages actually enriched. A no-op (returns 0) when
    OCR is disabled or nothing carries images, so the caller can invoke it
    unconditionally.
    """
    if not cfg.enabled:
        return 0
    targets = [m for m in messages if m.get("images")]
    if not targets:
        return 0

    url = cfg.url or glm_ocr.DEFAULT_OCR_URL
    if not glm_ocr.health(url):
        logger.warning("ocr: endpoint %s not ready; skipping enrichment", url)
        return 0

    cache: dict = load_json(CACHE_FILE, default={}) or {}
    dirty = False
    enriched = 0
    blobs: dict[str, bytes] = {}                 # fetch each image once, reuse

    def _bytes(ref: str) -> bytes:
        if ref not in blobs:
            blobs[ref] = _fetch_image(ref, cfg.timeout)
        return blobs[ref]

    # One image at a time per message, but images across messages can overlap
    # in flight -- the service is remote and the bottleneck is its latency.
    def _ocr_ref(ref: str) -> str:
        data = _bytes(ref)
        if not data:
            return ""
        key = hashlib.sha256(data).hexdigest()
        if key in cache:
            return cache[key]
        text = glm_ocr.ocr_image_bytes(data, url=url, timeout=cfg.timeout)
        cache[key] = text                       # cache even ""; a blank image stays blank
        return text

    with ThreadPoolExecutor(max_workers=4) as pool:
        for msg in targets:
            refs = [str(r) for r in (msg.get("images") or [])][: cfg.max_images]
            texts = [t for t in pool.map(_ocr_ref, refs) if t.strip()]

            # The dyad: the flyer and its text are one unit, combined here. The
            # OCR pass already holds the bytes, so it also embeds a card-sized
            # thumbnail (`_thumb`) that `attach_source_ids` carries onto the
            # event, so presentation shows *this* flyer beside *this* text --
            # not a generic link thumbnail decoupled from the description.
            if "_thumb" not in msg:
                for ref in refs:
                    jpeg = encode_thumbnail(_bytes(ref)) if _bytes(ref) else b""
                    if jpeg:
                        msg["_thumb"] = data_uri(jpeg)
                        break

            if not texts:
                continue
            dirty = True
            block = "\n".join(texts)
            msg["content"] = (msg.get("content") or "").rstrip()
            msg["content"] = (msg["content"] + "\n\n[flyer text]\n" + block).strip()
            msg["_ocr_chars"] = len(block)
            enriched += 1

    if dirty:
        save_json(CACHE_FILE, cache)
    logger.info("ocr: enriched %d of %d message(s) carrying images",
                enriched, len(targets))
    return enriched
