"""Telegram MTProto message reader via Telethon."""

import logging
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import InputMessagesFilterPinned as _PinnedFilter
from telethon.tl.types import Message

from event_harvester.config import TelegramConfig

logger = logging.getLogger("event_harvester.telegram")


def _should_include_dialog(
    name: str,
    channels_allowlist: list[str],
    channels_blocklist: list[str],
) -> bool:
    """Check if a dialog should be scanned based on allowlist/blocklist."""
    name_lower = name.lower()
    if channels_allowlist:
        return any(c.lower() in name_lower for c in channels_allowlist)
    if channels_blocklist:
        return not any(c.lower() in name_lower for c in channels_blocklist)
    return True


def _msg_to_dict(
    msg: Message, channel_name: str, pinned: bool = False,
) -> Optional[dict]:
    """Convert a Telethon Message to our dict format."""
    if not isinstance(msg, Message):
        return None
    if not msg.text:
        return None
    msg_date = msg.date.replace(tzinfo=timezone.utc)
    sender = (
        getattr(msg.sender, "username", None)
        or getattr(msg.sender, "first_name", None)
        or "unknown"
    ) if msg.sender else "unknown"
    d = {
        "platform": "telegram",
        "id": str(msg.id),
        "timestamp": msg_date.isoformat(),
        "author": sender,
        "channel": channel_name,
        "content": msg.text,
    }
    if pinned:
        d["pinned"] = True
    return d


async def read_telegram_messages(
    cutoff: datetime,
    cfg: TelegramConfig,
    *,
    channels_allowlist: Optional[list[str]] = None,
    channels_blocklist: Optional[list[str]] = None,
    client: Optional[TelegramClient] = None,
    include_pinned: bool = True,
) -> list[dict]:
    """Read Telegram messages newer than cutoff.

    If `client` is provided, it is used directly (and NOT disconnected).
    Otherwise a new client is created and disconnected after use.
    """
    if not cfg.is_configured:
        logger.info("TELEGRAM_API_ID/HASH not set - skipping.")
        return []

    own_client = client is None
    if own_client:
        client = TelegramClient(cfg.session, cfg.api_id, cfg.api_hash)
        await client.start(phone=cfg.phone or None)

    messages: list[dict] = []
    seen_ids: set[str] = set()

    try:
        me = await client.get_me()
        logger.info("Telegram: logged in as @%s", me.username or me.first_name)

        dialogs = await client.get_dialogs()
        logger.info("Telegram: scanning %d dialog(s) ...", len(dialogs))

        for dialog in dialogs:
            name = (
                getattr(dialog.entity, "title", None)
                or getattr(dialog.entity, "first_name", None)
                or "unknown"
            )

            if not _should_include_dialog(
                name,
                channels_allowlist or [],
                channels_blocklist or [],
            ):
                logger.debug("Telegram: skipping dialog '%s' (filtered)", name)
                continue

            # Pinned messages (regardless of cutoff date, last 10 per channel)
            if include_pinned:
                pinned_count = 0
                try:
                    async for msg in client.iter_messages(
                        dialog.entity, filter=_PinnedFilter,
                    ):
                        if pinned_count >= 10:
                            break
                        d = _msg_to_dict(msg, name, pinned=True)
                        if d and d["id"] not in seen_ids:
                            seen_ids.add(d["id"])
                            messages.append(d)
                            pinned_count += 1
                except Exception as e:
                    logger.debug(
                        "Telegram: pinned msgs for '%s': %s", name, e,
                    )

            # Recent messages within cutoff
            try:
                async for msg in client.iter_messages(
                    dialog.entity,
                    offset_date=datetime.now(timezone.utc),
                    reverse=False,
                    limit=None,
                ):
                    if not isinstance(msg, Message):
                        continue
                    msg_date = msg.date.replace(tzinfo=timezone.utc)
                    if msg_date < cutoff:
                        break
                    d = _msg_to_dict(msg, name)
                    if d and d["id"] not in seen_ids:
                        seen_ids.add(d["id"])
                        messages.append(d)
            except Exception as e:
                logger.warning("Telegram: skipping '%s': %s", name, e)
    finally:
        if own_client:
            await client.disconnect()

    messages.sort(key=lambda m: m["timestamp"])
    n_pinned = sum(1 for m in messages if m.get("pinned"))
    logger.info(
        "Telegram: %d message(s) (%d pinned) since %s UTC",
        len(messages), n_pinned, cutoff.strftime("%Y-%m-%d %H:%M"),
    )
    return messages


async def create_persistent_client(cfg: TelegramConfig) -> TelegramClient:
    """Create and start a TelegramClient that stays connected (for watch mode)."""
    client = TelegramClient(cfg.session, cfg.api_id, cfg.api_hash)
    await client.start(phone=cfg.phone or None)
    return client
