"""Discord LevelDB cache reader with platform-aware path resolution."""

import json
import logging
import os
import platform
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_harvester.discord")

# Conditional LevelDB import: plyvel (fast, C ext) or ccl_leveldb (pure Python fallback)
try:
    import plyvel

    USE_PLYVEL = True
except ImportError:
    USE_PLYVEL = False
    try:
        import ccl_leveldb

        logger.debug("Using ccl_leveldb (pure-Python fallback)")
    except ImportError:
        ccl_leveldb = None
        logger.warning(
            "No LevelDB reader available. "
            "Install plyvel (Linux/WSL) or ccl-leveldb (Windows)."
        )


def find_discord_cache(override_path: Optional[str] = None) -> Optional[Path]:
    """Find the Discord cache directory, supporting WSL, native Windows, and env override."""
    if override_path:
        p = Path(override_path)
        if p.exists():
            return p
        logger.warning("DISCORD_CACHE_PATH=%s does not exist", override_path)
        return None

    system = platform.system()

    # WSL: access Windows filesystem via /mnt/c/
    if system == "Linux" and Path("/mnt/c/Users").exists():
        return _find_cache_wsl()

    # Native Windows
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidate = Path(appdata) / "discord" / "Cache" / "Cache_Data"
            if candidate.exists():
                return candidate
        return None

    # Linux (non-WSL) — check ~/.config/discord
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


def _open_leveldb_plyvel(cache_path: Path) -> tuple:
    """Open LevelDB with plyvel, falling back to a temp copy if locked."""
    direct_err = None
    try:
        return plyvel.DB(str(cache_path), create_if_missing=False), None
    except Exception as e:
        direct_err = e

    tmp = Path(tempfile.mkdtemp(prefix="discord_cache_"))
    try:
        shutil.copytree(str(cache_path), str(tmp / "Cache_Data"))
        return plyvel.DB(str(tmp / "Cache_Data"), create_if_missing=False), tmp
    except Exception as copy_err:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(
            f"Could not open LevelDB directly ({direct_err}) "
            f"or via copy ({copy_err})."
        )


def _iter_leveldb(cache_path: Path):
    """Iterate key-value pairs from LevelDB using whichever backend is available."""
    if USE_PLYVEL:
        db, tmp_dir = _open_leveldb_plyvel(cache_path)
        if tmp_dir:
            logger.info("DB locked — reading from temp copy.")
        try:
            yield from db
        finally:
            db.close()
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
    elif ccl_leveldb is not None:
        db = ccl_leveldb.RawLevelDb(str(cache_path))
        for record in db.iterate_records_raw():
            yield record.user_key, record.value
    else:
        raise RuntimeError(
            "No LevelDB reader available. Install plyvel (Linux/WSL) or ccl-leveldb (Windows)."
        )


def _decode_cache_body(raw: bytes) -> Optional[bytes]:
    """Strip Chromium cache entry header to get the HTTP response body."""
    try:
        data = raw[8:]  # skip 8-byte Chromium Simple Cache entry header
        for sep in (b"\r\n\r\n", b"\n\n"):
            idx = data.find(sep)
            if idx != -1:
                return data[idx + len(sep) :]
        return data
    except Exception:
        return None


def _parse_message_blobs(body: bytes) -> list[dict]:
    """Parse Discord message JSON from a cache body."""
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
            return [m for m in parsed if isinstance(m, dict)]
        if isinstance(parsed, dict) and "id" in parsed:
            return [parsed]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return []


_MSG_URL_RE = re.compile(rb"https://discord\.com/api/v\d+/channels/(\d+)/messages")


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

    try:
        messages: list[dict] = []
        seen_ids: set[str] = set()

        for key, value in _iter_leveldb(cache_path):
            if not _MSG_URL_RE.search(key):
                continue
            body = _decode_cache_body(value)
            if not body:
                continue
            for msg in _parse_message_blobs(body):
                try:
                    msg_id = str(msg.get("id", ""))
                    content = msg.get("content", "").strip()
                    ts_str = msg.get("timestamp", "")
                    author = (msg.get("author") or {}).get("username", "?")
                    channel = str(msg.get("channel_id", "?"))

                    if not msg_id or not ts_str or not content or msg_id in seen_ids:
                        continue
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
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

        messages.sort(key=lambda m: m["timestamp"])
        logger.info(
            "Discord: %d message(s) since %s UTC",
            len(messages), cutoff.strftime("%Y-%m-%d %H:%M"),
        )
        return messages

    except RuntimeError as e:
        logger.error("Discord: %s", e)
        return []
