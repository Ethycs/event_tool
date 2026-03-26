"""Gmail API message reader via google-api-python-client.

Supports reading, replying, trashing, and permanently deleting messages.
"""

import base64
import json
import logging
import os
import tempfile
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from event_harvester.config import GmailConfig

logger = logging.getLogger("event_harvester.gmail")

# gmail.modify: read + label changes + trash/untrash
# gmail.compose: send and reply
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _decode_env_json(env_key: str) -> dict | None:
    """Decode a base64-encoded JSON env var, or return None."""
    raw = os.getenv(env_key, "")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw))
    except Exception:
        return None


def _get_credentials(cfg: GmailConfig) -> Optional[Credentials]:
    """Load Gmail OAuth2 credentials.

    Priority:
    1. GMAIL_TOKEN_JSON env var (base64-encoded token)
    2. token.json file on disk
    3. Fresh OAuth flow using credentials (env or file)
    """
    creds = None

    # Try token from env first
    token_data = _decode_env_json("GMAIL_TOKEN_JSON")
    if token_data:
        try:
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        except Exception as e:
            logger.debug("Gmail: env token parse failed: %s", e)

    # Fall back to token file
    token_path = Path(cfg.token_file)
    if creds is None and token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist refreshed token back to env-compatible format
            token_path.write_text(creds.to_json())
            return creds
        except Exception as e:
            logger.warning("Gmail: token refresh failed: %s", e)

    # Need fresh OAuth flow — try credentials from env, then file
    creds_data = _decode_env_json("GMAIL_CREDENTIALS_JSON")
    if creds_data:
        # Write to temp file for InstalledAppFlow (it needs a file path)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        )
        try:
            json.dump(creds_data, tmp)
            tmp.close()
            flow = InstalledAppFlow.from_client_secrets_file(tmp.name, SCOPES)
            creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
            return creds
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    if Path(cfg.credentials_file).exists():
        flow = InstalledAppFlow.from_client_secrets_file(
            str(cfg.credentials_file), SCOPES,
        )
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        return creds

    logger.info("Gmail: no credentials found (env or file)")
    return None


def _get_service(cfg: GmailConfig):
    """Build and return an authenticated Gmail API service, or None."""
    if not cfg.is_configured:
        logger.info("Gmail: credentials not found - skipping.")
        return None

    creds = _get_credentials(cfg)
    if creds is None:
        logger.warning("Gmail: could not obtain credentials - skipping.")
        return None

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _parse_timestamp(headers: list[dict]) -> Optional[str]:
    """Extract and parse the Date header into an ISO timestamp."""
    for h in headers:
        if h.get("name", "").lower() == "date":
            try:
                dt = parsedate_to_datetime(h["value"])
                return dt.isoformat()
            except Exception:
                return h["value"]
    return None


def _get_header(headers: list[dict], name: str) -> str:
    """Get a header value by name (case-insensitive)."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def fetch_messages(cfg: GmailConfig, since: datetime) -> list[dict]:
    """Fetch Gmail messages newer than `since` and return them in standard format.

    Returns a list of message dicts matching the event_harvester format:
        platform, channel, author, timestamp, content, id
    """
    service = _get_service(cfg)
    if service is None:
        return []

    # Build query: combine configured query with date filter
    date_filter = f"after:{since.strftime('%Y/%m/%d')}"
    query = f"{cfg.query} {date_filter}" if cfg.query else date_filter
    logger.info("Gmail: query=%s", query)

    messages: list[dict] = []
    page_token: Optional[str] = None
    fetched = 0

    while fetched < cfg.max_results:
        batch_size = min(100, cfg.max_results - fetched)
        result = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=batch_size,
                pageToken=page_token,
            )
            .execute()
        )

        msg_refs = result.get("messages", [])
        if not msg_refs:
            break

        for ref in msg_refs:
            try:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=ref["id"], format="metadata")
                    .execute()
                )

                headers = msg.get("payload", {}).get("headers", [])
                subject = _get_header(headers, "Subject")
                from_addr = _get_header(headers, "From")
                timestamp = _parse_timestamp(headers)
                snippet = msg.get("snippet", "")
                labels = msg.get("labelIds", [])

                # Use first non-system label as channel, fallback to INBOX
                channel = "INBOX"
                for lbl in labels:
                    if not lbl.startswith(("CATEGORY_", "UNREAD", "IMPORTANT", "SENT", "DRAFT")):
                        channel = lbl
                        break

                content = f"{subject}\n{snippet}" if subject else snippet

                messages.append(
                    {
                        "platform": "gmail",
                        "id": ref["id"],
                        "timestamp": timestamp or "",
                        "author": from_addr,
                        "channel": channel,
                        "content": content,
                    }
                )
            except Exception as e:
                logger.debug("Gmail: failed to fetch message %s: %s", ref["id"], e)
                continue

        fetched += len(msg_refs)
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    messages.sort(key=lambda m: m["timestamp"])
    logger.info(
        "Gmail: %d message(s) since %s UTC",
        len(messages),
        since.strftime("%Y-%m-%d %H:%M"),
    )
    return messages


# ── Full body fetch ─────────────────────────────────────────────────────


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        padded = body_data + "=" * (4 - len(body_data) % 4)
        try:
            return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            return ""

    # Recurse into multipart
    parts = payload.get("parts", [])
    # Prefer text/plain over text/html
    for part in parts:
        if part.get("mimeType") == "text/plain":
            result = _extract_body(part)
            if result:
                return result
    # Fallback to text/html stripped of tags
    for part in parts:
        if part.get("mimeType") == "text/html":
            body_data = part.get("body", {}).get("data", "")
            if body_data:
                import re

                padded = body_data + "=" * (4 - len(body_data) % 4)
                try:
                    html = base64.urlsafe_b64decode(padded).decode(
                        "utf-8", errors="replace"
                    )
                    return re.sub(r"<[^>]+>", "", html).strip()
                except Exception:
                    pass
    # Recurse deeper for nested multipart
    for part in parts:
        result = _extract_body(part)
        if result:
            return result
    return ""


def fetch_full_bodies(
    cfg: GmailConfig, message_ids: list[str],
) -> dict[str, str]:
    """Fetch full body text for a list of message IDs.

    Returns dict mapping message_id -> decoded body text.
    """
    service = _get_service(cfg)
    if service is None:
        return {}

    bodies: dict[str, str] = {}
    for msg_id in message_ids:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
            bodies[msg_id] = _extract_body(msg.get("payload", {}))
        except Exception as e:
            logger.debug("Gmail: failed to fetch body for %s: %s", msg_id, e)

    return bodies


# ── Reply ───────────────────────────────────────────────────────────────


def reply(cfg: GmailConfig, message_id: str, body_text: str) -> Optional[str]:
    """Reply to a Gmail message. Returns the sent message ID, or None on failure."""
    service = _get_service(cfg)
    if service is None:
        return None

    try:
        # Fetch the original to get threading headers
        original = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata")
            .execute()
        )
        headers = original.get("payload", {}).get("headers", [])
        subject = _get_header(headers, "Subject")
        from_addr = _get_header(headers, "From")
        message_id_header = _get_header(headers, "Message-Id")
        thread_id = original.get("threadId", "")

        # Build the reply
        reply_msg = MIMEText(body_text)
        reply_msg["To"] = from_addr
        reply_msg["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject
        reply_msg["In-Reply-To"] = message_id_header
        reply_msg["References"] = message_id_header

        raw = base64.urlsafe_b64encode(reply_msg.as_bytes()).decode()
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": thread_id})
            .execute()
        )
        logger.info("Gmail: replied to %s -> sent %s", message_id, sent["id"])
        return sent["id"]
    except Exception as e:
        logger.error("Gmail: failed to reply to %s: %s", message_id, e)
        return None


# ── Trash / Delete ──────────────────────────────────────────────────────


def trash(cfg: GmailConfig, message_id: str) -> bool:
    """Move a message to trash. Returns True on success."""
    service = _get_service(cfg)
    if service is None:
        return False

    try:
        service.users().messages().trash(userId="me", id=message_id).execute()
        logger.info("Gmail: trashed %s", message_id)
        return True
    except Exception as e:
        logger.error("Gmail: failed to trash %s: %s", message_id, e)
        return False


def delete(cfg: GmailConfig, message_id: str) -> bool:
    """Permanently delete a message (cannot be undone). Returns True on success."""
    service = _get_service(cfg)
    if service is None:
        return False

    try:
        service.users().messages().delete(userId="me", id=message_id).execute()
        logger.info("Gmail: permanently deleted %s", message_id)
        return True
    except Exception as e:
        logger.error("Gmail: failed to delete %s: %s", message_id, e)
        return False


# ── Mark read/unread ────────────────────────────────────────────────────


def mark_read(cfg: GmailConfig, message_id: str) -> bool:
    """Mark a message as read by removing the UNREAD label."""
    service = _get_service(cfg)
    if service is None:
        return False

    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return True
    except Exception as e:
        logger.error("Gmail: failed to mark read %s: %s", message_id, e)
        return False


def mark_unread(cfg: GmailConfig, message_id: str) -> bool:
    """Mark a message as unread by adding the UNREAD label."""
    service = _get_service(cfg)
    if service is None:
        return False

    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": ["UNREAD"]}
        ).execute()
        return True
    except Exception as e:
        logger.error("Gmail: failed to mark unread %s: %s", message_id, e)
        return False
