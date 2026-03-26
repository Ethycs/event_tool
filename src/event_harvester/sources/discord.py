"""Discord cache reader - parses Chromium BlockFile cache for message JSON."""

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

logger = logging.getLogger("event_harvester.discord")

_MSG_URL_RE = re.compile(r"/channels/(\d+)/messages")


def find_discord_cache(override_path: Optional[str] = None) -> Optional[Path]:
    """Find the Discord cache directory."""
    if override_path:
        p = Path(override_path)
        if p.exists():
            return p
        logger.warning("DISCORD_CACHE_PATH=%s does not exist", override_path)
        return None

    system = platform.system()

    if system == "Linux" and Path("/mnt/c/Users").exists():
        return _find_cache_wsl()

    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidate = Path(appdata) / "discord" / "Cache" / "Cache_Data"
            if candidate.exists():
                return candidate
        return None

    home = Path.home()
    candidate = home / ".config" / "discord" / "Cache" / "Cache_Data"
    if candidate.exists():
        return candidate

    return None


def _find_cache_wsl() -> Optional[Path]:
    """Scan /mnt/c/Users/ for Discord cache (WSL)."""
    users = Path("/mnt/c/Users")
    skip = {"Public", "Default", "Default User", "All Users"}
    for user_dir in users.iterdir():
        if not user_dir.is_dir() or user_dir.name in skip:
            continue
        candidate = user_dir / "AppData/Roaming/discord/Cache/Cache_Data"
        if candidate.exists():
            return candidate
    return None


def _decompress(data: bytes) -> bytes:
    """Decompress gzip or zlib, or return raw bytes."""
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


def _parse_message_blobs(body: bytes) -> list[dict]:
    """Parse Discord message JSON from raw bytes."""
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
            return [
                m for m in parsed
                if isinstance(m, dict) and "id" in m and "content" in m
            ]
        if isinstance(parsed, dict) and "id" in parsed:
            return [parsed]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return []


def read_discord_messages(
    cutoff: datetime,
    cache_path: Optional[Path] = None,
    override_path: Optional[str] = None,
) -> list[dict]:
    """Read cached Discord messages newer than cutoff."""
    if cache_path is None:
        cache_path = find_discord_cache(override_path)
    if cache_path is None:
        logger.info("Discord cache not found.")
        return []

    logger.info("Discord cache: %s", cache_path)

    # Copy cache to temp dir to avoid lock from running Discord
    tmp = Path(tempfile.mkdtemp(prefix="discord_cache_"))
    dst = tmp / "Cache_Data"
    try:
        shutil.copytree(str(cache_path), str(dst))
        logger.info("Copied cache to temp dir for safe reading.")
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        logger.error("Failed to copy Discord cache: %s", e)
        return []

    messages: list[dict] = []
    seen_ids: set[str] = set()

    try:
        cache = ChromiumBlockFileCache(dst)
        for key in cache.cache_keys():
            url = key.url if hasattr(key, "url") else str(key)
            if not _MSG_URL_RE.search(url):
                continue

            try:
                buffers = cache.get_cachefile(key)
            except Exception:
                continue

            for buf in buffers:
                if buf is None or len(buf) == 0:
                    continue
                data = _decompress(buf)
                for msg in _parse_message_blobs(data):
                    try:
                        msg_id = str(msg.get("id", ""))
                        content = msg.get("content", "").strip()
                        ts_str = msg.get("timestamp", "")
                        author = (msg.get("author") or {}).get(
                            "username", "?"
                        )
                        channel = str(msg.get("channel_id", "?"))

                        if not msg_id or not ts_str or msg_id in seen_ids:
                            continue
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if ts < cutoff:
                            continue
                        seen_ids.add(msg_id)
                        messages.append(
                            {
                                "platform": "discord",
                                "id": msg_id,
                                "timestamp": ts.isoformat(),
                                "author": author,
                                "channel": channel,
                                "content": content,
                            }
                        )
                    except Exception:
                        continue

        cache.close()
    except Exception as e:
        logger.error("Failed to parse Discord cache: %s", e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    messages.sort(key=lambda m: m["timestamp"])
    logger.info(
        "Discord: %d message(s) since %s UTC",
        len(messages), cutoff.strftime("%Y-%m-%d %H:%M"),
    )
    return messages
