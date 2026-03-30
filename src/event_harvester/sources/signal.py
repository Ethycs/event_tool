"""Signal Desktop message reader via local SQLCipher database.

Reads messages directly from Signal Desktop's encrypted SQLite database.
Requires Signal Desktop to be installed. Uses sigexport's crypto module
for key extraction (handles DPAPI on Windows, Keychain on macOS, etc.).
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_harvester.signal")


def _find_signal_dir(override_path: Optional[str] = None) -> Optional[Path]:
    """Find Signal Desktop's data directory."""
    if override_path:
        p = Path(override_path)
        if p.exists():
            return p
        logger.warning("SIGNAL_DATA_PATH=%s does not exist", override_path)
        return None

    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidate = Path(appdata) / "Signal"
        if (candidate / "sql" / "db.sqlite").exists():
            return candidate

    # macOS
    home = Path.home()
    candidate = home / "Library" / "Application Support" / "Signal"
    if (candidate / "sql" / "db.sqlite").exists():
        return candidate

    # Linux
    candidate = home / ".config" / "Signal"
    if (candidate / "sql" / "db.sqlite").exists():
        return candidate

    return None


def _connect_db(signal_dir: Path):
    """Connect to Signal's encrypted SQLite database."""
    from sigexport import crypto
    from sqlcipher3 import dbapi2

    db_file = signal_dir / "sql" / "db.sqlite"

    key = crypto.get_key(signal_dir, None)
    if not key:
        raise RuntimeError("Failed to extract Signal Desktop encryption key")

    db = dbapi2.connect(str(db_file))
    c = db.cursor()
    c.execute(f"PRAGMA KEY = \"x'{key}'\"")
    c.execute("PRAGMA cipher_page_size = 4096")
    c.execute("PRAGMA kdf_iter = 64000")
    c.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
    c.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")

    return db


def read_signal_messages(
    cutoff: datetime,
    override_path: Optional[str] = None,
) -> list[dict]:
    """Read Signal Desktop messages newer than cutoff.

    Returns message dicts in the standard event_harvester format.
    """
    signal_dir = _find_signal_dir(override_path)
    if signal_dir is None:
        logger.info("Signal Desktop not found.")
        return []

    logger.info("Signal: reading from %s", signal_dir)

    try:
        db = _connect_db(signal_dir)
    except Exception as e:
        logger.error("Signal: failed to open database: %s", e)
        return []

    c = db.cursor()

    # Load conversations for name lookup
    conversations: dict[str, dict] = {}
    try:
        c.execute(
            "SELECT id, type, name, profileName FROM conversations"
        )
        for row in c.fetchall():
            cid = row[0]
            conv_type = row[1]  # "private" or "group"
            name = row[2] or row[3] or cid
            conversations[cid] = {
                "name": name,
                "is_group": conv_type == "group",
            }
    except Exception as e:
        logger.warning("Signal: failed to load conversations: %s", e)

    # Fetch messages newer than cutoff
    cutoff_ms = int(cutoff.timestamp() * 1000)
    messages: list[dict] = []

    try:
        c.execute(
            "SELECT id, body, sent_at, type, conversationId, source "
            "FROM messages "
            "WHERE sent_at > ? AND body IS NOT NULL AND body != '' "
            "ORDER BY sent_at",
            (cutoff_ms,),
        )

        for row in c.fetchall():
            msg_id = str(row[0])
            body = row[1]
            sent_at_ms = row[2]
            msg_type = row[3]  # "incoming" or "outgoing"
            conv_id = row[4]
            source = row[5] or ""

            # Convert ms timestamp to ISO
            ts = datetime.fromtimestamp(sent_at_ms / 1000, tz=timezone.utc)
            timestamp = ts.isoformat()

            # Get conversation name
            conv = conversations.get(conv_id, {})
            channel = conv.get("name", conv_id or "Unknown")

            # Author: for outgoing it's "You", for incoming use source or conv name
            if msg_type == "outgoing":
                author = "You"
            else:
                author = source or channel

            d = {
                "platform": "signal",
                "id": msg_id,
                "timestamp": timestamp,
                "author": author,
                "channel": channel,
                "content": body,
            }
            if msg_type == "outgoing":
                d["is_sent"] = True

            messages.append(d)

    except Exception as e:
        logger.error("Signal: failed to read messages: %s", e)
    finally:
        db.close()

    messages.sort(key=lambda m: m["timestamp"])
    logger.info(
        "Signal: %d message(s) since %s UTC",
        len(messages),
        cutoff.strftime("%Y-%m-%d %H:%M"),
    )
    return messages
