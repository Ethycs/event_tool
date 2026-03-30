"""Generic Chromium cache reader for extracting API responses from any site.

Reads Chromium's BlockFile cache format to find cached HTTP responses
matching a URL pattern. Works with Chrome, Edge, Brave, Discord, and
any Chromium-based app.

Usage:
    entries = read_chromium_cache(
        cache_path="/path/to/Cache_Data",
        url_pattern=r"instagram\\.com/api/v1/feed",
    )
    for url, data in entries:
        parsed = json.loads(data)
        ...
"""

import gzip
import json
import logging
import os
import platform
import re
import shutil
import tempfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from ccl_chromium_reader.ccl_chromium_cache import ChromiumBlockFileCache

logger = logging.getLogger("event_harvester.chromium_cache")


# ── Cache directory discovery ──────────────────────────────────────────────

def find_chrome_cache(profile: str = "Default") -> Optional[Path]:
    """Find Chrome's HTTP cache directory."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            candidate = Path(base) / "Google" / "Chrome" / "User Data" / profile / "Cache" / "Cache_Data"
            if candidate.exists():
                return candidate
    elif system == "Darwin":
        candidate = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / profile / "Cache" / "Cache_Data"
        if candidate.exists():
            return candidate
    else:
        candidate = Path.home() / ".config" / "google-chrome" / profile / "Cache" / "Cache_Data"
        if candidate.exists():
            return candidate
    return None


def find_edge_cache(profile: str = "Default") -> Optional[Path]:
    """Find Edge's HTTP cache directory."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "User Data" / profile / "Cache" / "Cache_Data"
            if candidate.exists():
                return candidate
    return None


def find_browser_cache(browser: str = "chrome", profile: str = "Default") -> Optional[Path]:
    """Find a browser's cache directory by name."""
    if browser == "chrome":
        return find_chrome_cache(profile)
    elif browser == "edge":
        return find_edge_cache(profile)
    return None


# ── Decompression ──────────────────────────────────────────────────────────

def decompress(data: bytes) -> bytes:
    """Decompress gzip, zlib, or return raw bytes."""
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    try:
        return zlib.decompress(data, -zlib.MAX_WBITS)
    except zlib.error:
        pass
    return data


# ── JSON extraction ────────────────────────────────────────────────────────

def extract_json(body: bytes) -> list[dict]:
    """Extract JSON objects or arrays from raw cache body.

    Handles leading garbage, HTTP headers mixed in, etc.
    Returns a list of dicts found.
    """
    if not body:
        return []
    try:
        text = body.decode("utf-8", errors="replace").strip()
        start = min(
            (text.find(c) for c in ("[", "{") if text.find(c) != -1),
            default=-1,
        )
        if start == -1:
            return []
        parsed = json.loads(text[start:])
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return []


# ── Core cache reader ──────────────────────────────────────────────────────

# ── Trigram MinHash text classifier ────────────────────────────────────────

import hashlib
import struct

_NUM_HASHES = 64  # Number of hash functions for MinHash
_HASH_SEEDS = list(range(_NUM_HASHES))

# Reference natural language samples — diverse enough to cover event text,
# casual chat, email, announcements
_TEXT_REFERENCES = [
    "Join us for a community meetup this Saturday at the park. There will be food "
    "and drinks available. Please let us know if you can make it.",
    "Hey everyone, just a reminder that the hackathon registration closes tomorrow. "
    "Make sure to sign up before midnight. The event starts at nine in the morning.",
    "We are pleased to announce our annual conference taking place on April third "
    "through the fifth at the downtown convention center. Tickets are now on sale.",
    "Good morning! The weather looks great for this weekend. Anyone interested in "
    "going to the farmers market on Sunday? They have amazing fresh produce.",
    "Hi team, the project deadline has been moved to next Friday. Please update "
    "your task boards accordingly. Let me know if you have any questions.",
    "Looking for volunteers for the charity event next month. We need people to "
    "help with setup, registration, and cleanup. Free food for all volunteers.",
]

_ref_signatures: list[list[int]] = []


def _trigrams(text: str) -> set[str]:
    """Extract character trigrams from text."""
    text = text.lower()
    return {text[i:i+3] for i in range(len(text) - 2)}


def _minhash_signature(trigram_set: set[str]) -> list[int]:
    """Compute MinHash signature for a set of trigrams."""
    if not trigram_set:
        return [0xFFFFFFFF] * _NUM_HASHES

    sig = []
    for seed in _HASH_SEEDS:
        min_hash = 0xFFFFFFFF
        for trigram in trigram_set:
            h = struct.unpack("<I", hashlib.md5(
                f"{seed}:{trigram}".encode()
            ).digest()[:4])[0]
            if h < min_hash:
                min_hash = h
        sig.append(min_hash)
    return sig


def _jaccard_from_signatures(sig_a: list[int], sig_b: list[int]) -> float:
    """Estimate Jaccard similarity from two MinHash signatures."""
    if not sig_a or not sig_b:
        return 0.0
    matches = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
    return matches / len(sig_a)


def _get_ref_signatures() -> list[list[int]]:
    """Lazily compute MinHash signatures for reference texts."""
    global _ref_signatures
    if not _ref_signatures:
        for ref in _TEXT_REFERENCES:
            _ref_signatures.append(_minhash_signature(_trigrams(ref)))
    return _ref_signatures


def is_readable_text(data: bytes, threshold: float = 0.08) -> bool:
    """Fast check: is this natural language text or binary/JS/CSS/minified junk?

    Uses trigram MinHash to compare the character trigram profile against
    known natural language. Natural text shares common trigrams ("the", " is ",
    "ing", "and") that code and binary data don't have.

    Args:
        data: raw bytes to check
        threshold: minimum Jaccard similarity to any reference (0-1)

    Returns:
        True if the data looks like natural language text.
    """
    if len(data) < 20:
        return False

    # Quick binary check — skip obvious non-text
    non_printable = sum(1 for b in data[:200] if b < 32 and b not in (9, 10, 13))
    if non_printable / min(len(data), 200) > 0.1:
        return False

    try:
        text = data[:500].decode("utf-8", errors="replace")
    except Exception:
        return False

    sample_sig = _minhash_signature(_trigrams(text))
    ref_sigs = _get_ref_signatures()

    # Check similarity to any reference text
    best = max(_jaccard_from_signatures(sample_sig, ref) for ref in ref_sigs)
    return best >= threshold


def extract_text_from_html(html: str) -> str:
    """Strip HTML tags and return readable text content."""
    # Remove script and style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def read_cache_entries(
    cache_path: Path,
    url_pattern: str | re.Pattern,
) -> list[tuple[str, list[dict]]]:
    """Read all cache entries matching a URL pattern.

    Args:
        cache_path: path to the Cache_Data directory
        url_pattern: regex pattern to match against cached URLs

    Returns:
        List of (url, json_objects) tuples.
    """
    if isinstance(url_pattern, str):
        url_pattern = re.compile(url_pattern)

    # Copy to temp dir to avoid lock from running browser
    tmp = Path(tempfile.mkdtemp(prefix="chromium_cache_"))
    dst = tmp / "Cache_Data"
    try:
        shutil.copytree(str(cache_path), str(dst))
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        logger.error("Failed to copy cache: %s", e)
        return []

    results: list[tuple[str, list[dict]]] = []

    try:
        cache = ChromiumBlockFileCache(dst)
        for key in cache.cache_keys():
            url = key.url if hasattr(key, "url") else str(key)
            if not url_pattern.search(url):
                continue

            try:
                buffers = cache.get_cachefile(key)
            except Exception:
                continue

            for buf in buffers:
                if buf is None or len(buf) == 0:
                    continue
                data = decompress(buf)
                objects = extract_json(data)
                if objects:
                    results.append((url, objects))

        cache.close()
    except Exception as e:
        logger.error("Failed to read cache: %s", e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    logger.info("Cache: found %d entries matching pattern.", len(results))
    return results


def read_cache_messages(
    cache_path: Path,
    url_pattern: str | re.Pattern,
    *,
    platform_name: str,
    cutoff: datetime,
    id_field: str = "id",
    timestamp_field: str = "timestamp",
    author_field: str | tuple = "author",
    channel_field: str = "channel_id",
    content_field: str = "content",
) -> list[dict]:
    """Read cached API responses and convert to standard message format.

    This is a higher-level wrapper around read_cache_entries that maps
    API response fields to the event_harvester message dict format.

    Args:
        cache_path: path to Cache_Data directory
        url_pattern: regex for matching cached URLs
        platform_name: e.g., "instagram", "twitter"
        cutoff: only return messages newer than this
        id_field: JSON key for message ID
        timestamp_field: JSON key for ISO timestamp
        author_field: JSON key for author (string or tuple for nested access)
        channel_field: JSON key for channel/group name
        content_field: JSON key for message text

    Returns:
        List of message dicts in standard format.
    """
    entries = read_cache_entries(cache_path, url_pattern)

    messages: list[dict] = []
    seen_ids: set[str] = set()

    for url, objects in entries:
        for obj in objects:
            try:
                msg_id = str(_get_nested(obj, id_field, ""))
                if not msg_id or msg_id in seen_ids:
                    continue

                content = str(_get_nested(obj, content_field, "")).strip()
                ts_str = str(_get_nested(obj, timestamp_field, ""))
                if not ts_str:
                    continue

                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue

                author = str(_get_nested(obj, author_field, "?"))
                channel = str(_get_nested(obj, channel_field, "?"))

                seen_ids.add(msg_id)
                messages.append({
                    "platform": platform_name,
                    "id": msg_id,
                    "timestamp": ts.isoformat(),
                    "author": author,
                    "channel": channel,
                    "content": content,
                })
            except Exception:
                continue

    messages.sort(key=lambda m: m["timestamp"])
    logger.info(
        "%s: %d message(s) from cache since %s UTC",
        platform_name,
        len(messages),
        cutoff.strftime("%Y-%m-%d %H:%M"),
    )
    return messages


def read_cached_pages(
    cache_path: Path,
    url_pattern: str | re.Pattern,
    *,
    platform_name: str = "web",
    min_text_length: int = 50,
    gzip_text_check: bool = True,
) -> list[dict]:
    """Read cached HTML pages as messages for event extraction.

    Extracts text from any cached webpage matching the URL pattern.
    Each page becomes a "message" with the URL as channel and the
    page text as content. No field mapping needed — works with any site.

    Args:
        cache_path: path to Cache_Data directory
        url_pattern: regex for matching cached URLs (e.g., "eventbrite|lu\\.ma|meetup")
        platform_name: platform label (default "web")
        min_text_length: skip pages with less text than this

    Returns:
        List of message dicts in standard format.
    """
    if isinstance(url_pattern, str):
        url_pattern = re.compile(url_pattern)

    # Copy to temp dir
    tmp = Path(tempfile.mkdtemp(prefix="chromium_cache_"))
    dst = tmp / "Cache_Data"
    try:
        shutil.copytree(str(cache_path), str(dst))
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        logger.error("Failed to copy cache: %s", e)
        return []

    messages: list[dict] = []
    seen_urls: set[str] = set()
    n_gzip_rejected = 0

    try:
        cache = ChromiumBlockFileCache(dst)
        for key in cache.cache_keys():
            url = key.url if hasattr(key, "url") else str(key)
            if not url_pattern.search(url):
                continue
            if url in seen_urls:
                continue

            try:
                buffers = cache.get_cachefile(key)
            except Exception:
                continue

            for buf in buffers:
                if buf is None or len(buf) == 0:
                    continue
                data = decompress(buf)

                # Try JSON first (API responses)
                objects = extract_json(data)
                if objects:
                    for obj in objects:
                        text = json.dumps(obj, ensure_ascii=False)[:2000]
                        if len(text) < min_text_length:
                            continue
                        # Gzip classifier gate
                        if gzip_text_check and not is_readable_text(text.encode()):
                            n_gzip_rejected += 1
                            continue
                        seen_urls.add(url)
                        messages.append({
                            "platform": platform_name,
                            "id": f"web:{hash(url + text[:100])}",
                            "timestamp": datetime.now().isoformat(),
                            "author": _extract_domain(url),
                            "channel": url[:120],
                            "content": text,
                        })
                    continue

                # Fall back to HTML text extraction
                try:
                    html = data.decode("utf-8", errors="replace")
                    # Skip binary data, JS bundles, CSS
                    if html[:20].count("\x00") > 2:
                        continue
                    if re.search(r"^\s*(function|var |const |import |export |\{\"use)", html[:200]):
                        continue
                    text = extract_text_from_html(html)
                    if len(text) < min_text_length:
                        continue
                    # Gzip classifier gate
                    if gzip_event_score(text) > gzip_threshold:
                        n_gzip_rejected += 1
                        continue
                    seen_urls.add(url)
                    messages.append({
                        "platform": platform_name,
                        "id": f"web:{hash(url)}",
                        "timestamp": datetime.now().isoformat(),
                        "author": _extract_domain(url),
                        "channel": url[:120],
                        "content": text[:2000],
                    })
                except Exception:
                    continue

        cache.close()
    except Exception as e:
        logger.error("Failed to read cache: %s", e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    logger.info(
        "%s: %d event-like page(s) from cache (%d rejected by gzip classifier).",
        platform_name, len(messages), n_gzip_rejected,
    )
    return messages


def _extract_domain(url: str) -> str:
    """Extract domain from a URL for use as author."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return url[:50]


def _get_nested(obj: dict, key: str | tuple, default=""):
    """Get a value from a dict, supporting nested access via tuple keys."""
    if isinstance(key, tuple):
        for k in key:
            if isinstance(obj, dict):
                obj = obj.get(k, default)
            else:
                return default
        return obj
    return obj.get(key, default)
