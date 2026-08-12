"""Event thumbnails, embedded rather than linked.

**Why embedded.** `html_report.py` writes one self-contained file — no CDN, no
fonts, nothing external — and its own footer says *"This file contains the
event data — forwarding it forwards them."* A remote `<img src="https://...">`
would break both halves of that: `tests/test_html_report.py` fails the build on
any external `src`, and more importantly a forwarded copy would report back to
`images.lumacdn.com` every time anyone opened it, from wherever they opened it.
So images are downscaled and inlined as `data:` URIs, and the page keeps
working on a plane.

**Where the images come from.** The event's own `link`, read the way a chat app
reads one: `og:image`, then `twitter:image`, then schema.org `Event.image`.
Deliberately generic rather than per-platform — Luma, Eventbrite, Partiful and
a venue's own site all publish one, and a per-platform path would need a new
branch per source forever.

**Cost, and how it is bounded.** One HTTP fetch per *unique* link, plus one per
image, on the machine writing the report. Three limits keep that honest: a disk
cache keyed by URL (so a rebuild costs nothing), `max_links` (so a big harvest
cannot fan out unboundedly), and a total byte budget (so the page cannot
silently become a 40 MB attachment). Everything fails soft — no image is a
missing thumbnail, never a failed report.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import logging
import re
from pathlib import Path

from event_harvester.utils import data_path

logger = logging.getLogger("event_harvester.outputs.thumbnails")

CACHE_DIR = data_path(".thumbnails")

# 2x the ~150px CSS slot, so it stays sharp on a retina phone and no sharper.
THUMB_W, THUMB_H = 300, 168
JPEG_QUALITY = 72
# One absurd image must not eat the budget before anything else is tried.
MAX_IMAGE_BYTES = 8_000_000
# Total base64 across the page: ~200 thumbnails at a typical 10 KB.
DEFAULT_BUDGET_BYTES = 2_000_000
DEFAULT_MAX_LINKS = 120
TIMEOUT_S = 12
WORKERS = 8
UA = "event-harvester/1.0 (+thumbnails)"

# `property` and `name` both appear in the wild, in either attribute order.
_META_PATTERNS = (
    r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url|:url)?["\']'
    r'[^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    r'<meta[^>]+(?:property|name)=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
)


def _unescape(raw: str) -> str:
    """Undo HTML entity encoding in an attribute value.

    **This is the whole reason Instagram thumbnails appeared impossible.** A
    `content="…"` attribute is HTML, so a multi-parameter URL is written with
    `&amp;` between parameters. Taken literally, every parameter after the
    first is renamed: `oh` becomes `amp;oh`, `oe` becomes `amp;oe`. Those two
    are the CDN's signature and expiry, so the request arrives *unsigned* and
    `scontent-*.cdninstagram.com` answers 403 — correctly.

    Diagnosed 2026-08-12 after two wrong conclusions: first that a `Referer`
    was missing, then that the URLs were session-signed and unreachable.
    Neither was true. Printing the parsed query parameters showed
    `amp;_nc_cat` and the cause was immediate — the evidence was one `print`
    away the whole time, behind an assumption about the remote host.
    """
    from html import unescape

    return unescape(raw or "").strip()


def find_image_url(html: str, page_url: str = "") -> str:
    """The image a link preview would use: og:image, twitter:image, JSON-LD."""
    if not html:
        return ""
    for pattern in _META_PATTERNS:
        m = re.search(pattern, html, re.I)
        if m:
            return _absolutise(unwrap_proxy(_unescape(m.group(1))), page_url)

    from event_harvester.sources.web_utils import _extract_structured_data

    data = _extract_structured_data(html) or {}
    image = data.get("image") or ""
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or ""
    return _absolutise(str(image).strip(), page_url)


def unwrap_proxy(url: str) -> str:
    """Unwrap a Next.js image proxy to the real CDN URL it points at.

    Eventbrite's `og:image` is `/_next/image?url=<encoded>&w=…&q=…`, and that
    endpoint answers **400** to anything but its own front-end. The genuine
    image is right there in the `url` parameter, so unwrap rather than give up
    — measured 2026-08-12, this is the difference between Eventbrite events
    having thumbnails and not.

    **Decode exactly once.** The value looks doubly-encoded
    (`https%253A%252F%252F`) and it is tempting to unquote until it stops
    changing — that is wrong and produced a 400. The inner URL is
    `img.evbuc.com/<cdn-url-as-an-encoded-path-segment>`; `parse_qs` performs
    the outer decode, leaving `%3A%2F%2F`, which is exactly what that host
    expects to receive. Decoding again yields a bare `https://` inside the
    path and the CDN rejects it.
    """
    from urllib.parse import parse_qs, urlparse

    if "/_next/image" not in url and "/cdn-cgi/image/" not in url:
        return url
    inner = parse_qs(urlparse(url).query).get("url", [""])[0]
    return inner if inner.startswith(("http://", "https://")) else url


def _absolutise(url: str, page_url: str) -> str:
    """Resolve a relative image path. A `data:` image is dropped, not passed
    through — it would already be inline, and re-encoding it is pointless."""
    if not url or url.startswith("data:"):
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url
    from urllib.parse import urljoin

    return urljoin(page_url, url) if page_url else ""


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg")


def encode_thumbnail(raw: bytes) -> bytes:
    """Downscale to a cover-cropped JPEG. Returns b"" if it is not an image.

    Cover-crop rather than fit: the cards are a grid, and letterboxed images of
    varying aspect ratios make a grid look broken in a way a uniform crop does
    not. Flattened onto white because JPEG has no alpha and a transparent PNG
    would otherwise composite onto black.
    """
    import io

    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img = ImageOps.fit(
            img.convert("RGB"), (THUMB_W, THUMB_H), method=Image.LANCZOS,
        )
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()
    except Exception as e:  # noqa: BLE001 -- arbitrary bytes off the internet
        logger.debug("thumbnail: not a usable image (%s)", e)
        return b""


def _fetch(url: str, accept: str, referer: str = "") -> bytes:
    """GET with a ceiling, sending the page as `Referer` for image requests.

    **The Referer is not load-bearing for any case measured here.** It was
    added on a wrong diagnosis of the Instagram 403 — the real cause was an
    un-unescaped `&amp;` in the URL (see `_unescape`), and with that fixed a
    plain bot User-Agent gets a 200 with or without it. It is kept only
    because sending the page an image was found on is what a browser does, and
    hotlink protection is real on other hosts. Do not treat its presence as
    evidence that some host required it.
    """
    import requests

    headers = {"User-Agent": UA, "Accept": accept}
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, timeout=TIMEOUT_S, stream=True, headers=headers)
    r.raise_for_status()
    # Read with a ceiling rather than trusting Content-Length, which is
    # advisory and absent on chunked responses.
    buf = bytearray()
    for chunk in r.iter_content(64 * 1024):
        buf.extend(chunk)
        if len(buf) > MAX_IMAGE_BYTES:
            raise ValueError(f"image over {MAX_IMAGE_BYTES} bytes")
    return bytes(buf)


def thumbnail_for(link: str) -> bytes:
    """JPEG bytes for one event link, via the on-disk cache. b"" if none."""
    if not link:
        return b""
    cached = _cache_path(link)
    try:
        if cached.exists():
            return cached.read_bytes()
    except OSError:
        pass

    try:
        html = _fetch(link, "text/html,*/*").decode("utf-8", errors="replace")
        image_url = find_image_url(html, link)
        if not image_url:
            return b""
        jpeg = encode_thumbnail(_fetch(image_url, "image/*", referer=link))
    except Exception as e:  # noqa: BLE001 -- a third-party page, anything goes
        logger.debug("thumbnail: %s -> %s", link, e)
        return b""

    if jpeg:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(jpeg)
        except OSError as e:
            logger.debug("thumbnail: could not cache %s: %s", link, e)
    return jpeg


def data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def thumbnails_for(
    links: list[str],
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    max_links: int = DEFAULT_MAX_LINKS,
) -> dict[str, str]:
    """`{link: data-uri}` for the links that had a usable image.

    Links are de-duplicated before fetching — several events routinely share a
    venue page — and the budget is applied to the encoded size, because that is
    what actually lands in the file.
    """
    unique = [u for u in dict.fromkeys(links) if u][:max_links]
    if not unique:
        return {}

    out: dict[str, str] = {}
    spent = 0
    skipped_budget = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for link, jpeg in zip(unique, pool.map(thumbnail_for, unique)):
            if not jpeg:
                continue
            uri = data_uri(jpeg)
            if spent + len(uri) > budget_bytes:
                skipped_budget += 1
                continue
            out[link] = uri
            spent += len(uri)

    logger.info(
        "Thumbnails: %d/%d link(s) illustrated, %d KB embedded%s",
        len(out), len(unique), spent // 1024,
        f", {skipped_budget} dropped over budget" if skipped_budget else "",
    )
    return out
