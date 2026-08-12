"""Gmail API message reader via google-api-python-client.

Supports reading, replying, trashing, and permanently deleting messages.
"""

import base64
import json
import logging
import os
import re
import sys
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
from event_harvester.observability.tracing import set_span_attrs, traced

logger = logging.getLogger("event_harvester.gmail")
_TRACER = "event_harvester.sources.gmail"

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


def _use_token(label: str, creds: Optional[Credentials], token_path: Path) -> Optional[Credentials]:
    """Make one credential candidate usable, or return None so the caller moves on.

    Returning None rather than raising is the point: a candidate that cannot be
    refreshed is not a fatal condition while another candidate is still untried.
    """
    if creds is None:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist refreshed token back to env-compatible format
            token_path.write_text(creds.to_json())
            logger.info("Gmail: refreshed credentials from %s", label)
            return creds
        except Exception as e:
            logger.warning(
                "Gmail: %s could not be refreshed (%s) — trying the next source", label, e,
            )
            return None
    logger.warning(
        "Gmail: %s is unusable (expired=%s, refresh_token=%s)",
        label, creds.expired, bool(creds.refresh_token),
    )
    return None


def _oauth_flow_allowed() -> bool:
    """Whether it is safe to open a browser and block on human consent."""
    if os.getenv("GMAIL_OAUTH_INTERACTIVE", "").lower() in ("1", "true", "yes"):
        return True
    try:
        return sys.stdin.isatty()
    except Exception:
        # A closed or replaced stdin is itself evidence nobody is watching.
        return False


def _get_credentials(cfg: GmailConfig) -> Optional[Credentials]:
    """Load Gmail OAuth2 credentials.

    Priority:
    1. GMAIL_TOKEN_JSON env var (base64-encoded token)
    2. token.json file on disk
    3. Fresh OAuth flow using credentials (env or file)

    Each candidate is tried *to completion* — including its refresh — before the
    next is considered. An earlier candidate that parses but cannot be refreshed
    must not mask a later one that works: a stale `GMAIL_TOKEN_JSON` sitting in
    `.env` alongside a perfectly good `token.json` used to take Gmail down
    entirely, because merely parsing was enough to claim the slot.
    """
    token_path = Path(cfg.token_file)
    candidates: list[tuple[str, Credentials]] = []

    token_data = _decode_env_json("GMAIL_TOKEN_JSON")
    if token_data:
        try:
            candidates.append(
                ("GMAIL_TOKEN_JSON", Credentials.from_authorized_user_info(token_data, SCOPES)),
            )
        except Exception as e:
            logger.debug("Gmail: env token parse failed: %s", e)

    if token_path.exists():
        try:
            candidates.append(
                (cfg.token_file, Credentials.from_authorized_user_file(str(token_path), SCOPES)),
            )
        except Exception as e:
            logger.debug("Gmail: token file parse failed: %s", e)

    for label, candidate in candidates:
        creds = _use_token(label, candidate, token_path)
        if creds is not None:
            return creds

    # Every stored token is dead — a fresh OAuth flow is the only way forward,
    # and it opens a browser and blocks on human consent. That is fine at a
    # terminal and wrong everywhere else: under the MCP stdio server, a service
    # runner, or a scheduled harvest it hangs the run waiting for a click nobody
    # will make. Refuse unless there is a human present, or an explicit override.
    if not _oauth_flow_allowed():
        logger.error(
            "Gmail: all stored tokens are dead and no interactive terminal is "
            "attached, so the consent flow was skipped. Re-authorise by running "
            "a harvest from a terminal, or set GMAIL_OAUTH_INTERACTIVE=1 to force it.",
        )
        return None

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


@traced("source.gmail.fetch", tracer_name=_TRACER)
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
                thread_id = msg.get("threadId", "")
                is_read = "UNREAD" not in labels
                is_sent = "SENT" in labels

                messages.append(
                    {
                        "platform": "gmail",
                        "id": ref["id"],
                        "thread_id": thread_id,
                        "timestamp": timestamp or "",
                        "author": from_addr,
                        "channel": channel,
                        "content": content,
                        "is_read": is_read,
                        "is_sent": is_sent,
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

    # Hydrate here, not in the pipeline, so the message cache stores real
    # bodies and the extra API calls are paid once per harvest rather than
    # once per cache miss.
    if cfg.fetch_bodies and messages:
        try:
            hydrate_bodies(cfg, messages)
        except Exception as e:  # noqa: BLE001 -- never lose a harvest to enrichment
            logger.warning("Gmail: body hydration failed, keeping snippets: %s", e)

    set_span_attrs(**{"message.count": len(messages)})
    return messages


def filter_read_sent(messages: list[dict], *, drop_read: bool = True) -> list[dict]:
    """Keep only Gmail messages that still need attention.

    Filters out:
    - Messages you sent yourself (SENT label) — always
    - Messages already read (no UNREAD label) — only when ``drop_read``

    ``drop_read`` defaults to True because that is what the recruiter-triage
    callers mean by "actionable". **Event harvesting passes False** (see
    ``GmailConfig.drop_read``): read mail was 42% of the mailbox measured
    2026-08-10, and an invite you opened is still an invite. The default is
    left alone so `cli/commands/recruiters.py` and `training/eval_classifier.py`
    keep their existing behaviour without a change.

    Non-Gmail messages pass through unchanged.
    """
    result = []
    n_read = 0
    n_sent = 0
    for m in messages:
        if m.get("platform") != "gmail":
            result.append(m)
            continue
        if m.get("is_sent"):
            n_sent += 1
            continue
        if drop_read and m.get("is_read"):
            n_read += 1
            continue
        result.append(m)

    if n_read or n_sent:
        logger.info(
            "Gmail: filtered %d read + %d sent, %d remain.",
            n_read, n_sent, sum(1 for m in result if m.get("platform") == "gmail"),
        )
    return result


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


# Marketing mail pads the preview line with zero-width characters, and
# HTML-stripped bodies arrive full of hard-wrap artefacts. Neither is content.
_INVISIBLE_RE = re.compile(r"[­​‌‍⁠﻿͏]+")
_WHITESPACE_RE = re.compile(r"[ \t ]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")

# Platforms whose URL *is* a dated event reference. A link to one of these is
# the single most reliable event signal an email carries -- measured
# 2026-08-10, 4 of 32 otherwise-starved messages carried one, and every one
# was a real event.
_EVENT_HOST_RE = re.compile(
    r"https?://[^\s\"'<>)\]]*(?:"
    r"eventbrite|lu\.ma|luma\.com|meetup\.com|partiful|dice\.fm|"
    r"ticketmaster|withfriends|splashthat|eventzilla"
    r")[^\s\"'<>)\]]*",
    re.I,
)


# Everything below is sized against one downstream constant:
# `analysis.build_prompt` truncates each message to 400 characters *and*
# derives its `dates=`/`times=` hints from that same slice. So a date past
# char 400 is invisible twice -- to the LLM and to the hint extractor.
_PROMPT_WINDOW = 400
# Keep the date-dense digest well under the storage ceiling so decimation
# actually engages on a typical 2.5 KB newsletter.
_DIGEST_CHARS = 1500
# Reach back only far enough for the sentence carrying the date. The web path
# uses 300; that much preamble would consume three quarters of the window.
_CONTEXT_BEFORE = 80
# Two links, not three: they are the strongest signal an email carries, but a
# tracking URL runs 150+ chars and three of them would crowd out the prose.
_MAX_LINKS = 2


def clean_body(text: str) -> str:
    """Strip the padding and hard-wrap noise out of a decoded body."""
    if not text:
        return ""
    text = _INVISIBLE_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def event_links(text: str, limit: int = 3) -> list[str]:
    """Event-platform URLs in document order, deduplicated.

    Not truncated: half a URL is not a URL, and these carry the event slug
    (`...downtown-first-thursdays-w-eric-rhodes-aug-6-2026-tickets-...`),
    which is often a better date source than the prose around it.
    """
    seen: list[str] = []
    for m in _EVENT_HOST_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;")
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def expand_event_links(
    urls: list[str], timeout_s: int = 12, workers: int = 8,
) -> dict[str, dict]:
    """Fetch event pages and read their schema.org JSON-LD. No browser.

    **Why bother when the body is already in `content`.** The body says "You
    are invited" and puts the date on the *page*. Measured 2026-08-10 over 14
    links from a live mailbox, **12 returned a full JSON-LD `Event`** with
    exact `startDate`, `endDate` and a resolved venue address. That is ground
    truth handed to the extractor, in place of prose for it to infer from.

    **Luma serves it server-side despite being a React SPA** -- that is the
    fact that made this cheap, and it is not something to assume of the next
    platform. Both misses in the sample were pages that render their event
    data client-side; they degrade to og: tags in the body text, which is the
    behaviour that already existed.

    Reuses `feed_probe._fetch_page` (browser-free, sets a UA) and
    `web_utils._extract_structured_data` (the same JSON-LD reader the web
    calendar path uses), so this adds no new parsing or fetching machinery.

    Returns ``{url: event_dict}`` for the URLs that yielded one. Failures are
    simply absent -- an unreachable page must never cost the message.
    """
    import concurrent.futures

    from event_harvester.sources.feed_probe import _fetch_page
    from event_harvester.sources.web_utils import _extract_structured_data

    unique = list(dict.fromkeys(u for u in urls if u))
    if not unique:
        return {}

    def _one(url: str) -> tuple[str, Optional[dict]]:
        try:
            html = _fetch_page(url, timeout_s=timeout_s)
            return url, _extract_structured_data(html) if html else None
        except Exception as e:  # noqa: BLE001 -- a third-party page, anything goes
            logger.debug("Gmail: link expansion failed for %s: %s", url, e)
            return url, None

    out: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for url, data in pool.map(_one, unique):
            if data and data.get("title"):
                out[url] = data
    logger.info(
        "Gmail: expanded %d/%d event link(s) into structured data",
        len(out), len(unique),
    )
    return out


def format_event_facts(data: dict) -> str:
    """One line of authoritative event facts, for the top of `content`.

    Deliberately terse and first: `build_prompt` keeps only 400 characters,
    and these ~90 are worth more than any 90 in the body.
    """
    when = " ".join(p for p in [data.get("date", ""), data.get("time", "")] if p)
    parts = [
        f"EVENT: {data.get('title', '')}".strip(),
        f"WHEN: {when}" if when else "",
        f"WHERE: {data['location']}" if data.get("location") else "",
    ]
    return " | ".join(p for p in parts if p)


def hydrate_bodies(cfg: GmailConfig, messages: list[dict]) -> list[dict]:
    """Replace Gmail's 200-char snippet with the real body, in place.

    **Why this exists.** `fetch_messages` uses `format="metadata"`, so
    `content` is `subject + snippet` and Gmail caps that snippet at 200
    characters. Measured 2026-08-10 over 85 messages: median body 2505 chars,
    so the pipeline was reading ~8% of every email. 32 of those 85 reached the
    reranker carrying neither a date nor a link and were dropped for it --
    while 4 of them held an Eventbrite or Luma link that sat past the cut.

    **The shape of `content` is load-bearing, and it is the Instagram
    permalink-first lesson again.** `analysis.build_prompt` truncates content
    to **400 chars**, so simply concatenating a 5 KB body would hand the LLM a
    letterhead and an unsubscribe notice. So the order is: subject, then any
    event-platform links, then the body *decimated to its date-densest
    window* by the same helper the web path uses. What survives the 400-char
    cut is then the part that names the event.

    Failure is per-message and silent-but-logged: a body that will not fetch
    leaves that message on its snippet rather than losing the message.
    """
    if not messages:
        return messages
    ids = [m["id"] for m in messages if m.get("platform") == "gmail"]
    if not ids:
        return messages

    bodies = fetch_full_bodies(cfg, ids)
    if not bodies:
        logger.warning("Gmail: no bodies fetched; keeping snippets for %d message(s)", len(ids))
        return messages

    # Imported here rather than at module scope: this is the one place the
    # mail path touches the web path, and a top-level import would put
    # web_utils on the import graph of every Gmail caller.
    from event_harvester.sources.web_utils import _decimate_text

    limit = max(200, cfg.body_max_chars)

    # Pass 1: find the links. Pass 2 fetches them all at once, because one
    # pooled round of requests beats a sequential fetch inside the loop -- and
    # the same event is routinely linked from several emails, so deduplicating
    # first is what keeps the fetch count near the number of *events*.
    cleaned: dict[str, str] = {}
    wanted: list[str] = []
    for m in messages:
        if m.get("platform") != "gmail":
            continue
        body = clean_body(bodies.get(m["id"], ""))
        if not body:
            continue
        cleaned[m["id"]] = body
        wanted.extend(u for u in event_links(body, limit=_MAX_LINKS) if u not in wanted)

    expanded: dict[str, dict] = {}
    if cfg.expand_links and wanted:
        expanded = expand_event_links(
            wanted[: max(0, cfg.expand_max)],
            timeout_s=cfg.expand_timeout_s,
            workers=cfg.expand_workers,
        )
        if len(wanted) > cfg.expand_max:
            logger.warning(
                "Gmail: %d event link(s) over the expand_max=%d ceiling were not "
                "fetched", len(wanted) - cfg.expand_max, cfg.expand_max,
            )

    hydrated = 0
    for m in messages:
        if m.get("platform") != "gmail":
            continue
        body = cleaned.get(m["id"], "")
        if not body:
            continue
        subject = m["content"].partition("\n")[0]
        # Decimate against the body alone -- the subject is prepended after,
        # so it can never be decimated away. The target is _DIGEST_CHARS, not
        # `body_max_chars`: decimation no-ops on text already under its
        # threshold, so sizing it at the 5000-char storage ceiling would let a
        # typical 2.5 KB newsletter through whole and hand the prompt window a
        # letterhead. Sized for the prompt, it engages where it matters.
        core = _decimate_text(
            body, max_chars=min(_DIGEST_CHARS, limit), context_before=_CONTEXT_BEFORE,
        )
        links = event_links(body, limit=_MAX_LINKS)
        # Facts before links before prose. The JSON-LD line is exact where the
        # body is only suggestive, so it gets the front of the 400-char window.
        facts = [format_event_facts(expanded[u]) for u in links if u in expanded]
        parts = [subject, *facts, *links, core]
        m["content"] = "\n".join(p for p in parts if p)[:limit]
        m["body_chars"] = len(body)
        if facts:
            m["event_facts"] = True
        hydrated += 1

    logger.info(
        "Gmail: hydrated %d/%d message(s) with full bodies (snippet cap was 200 chars)",
        hydrated, len(ids),
    )
    set_span_attrs(**{"gmail.hydrated": hydrated})
    return messages


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


# ── Labels ──────────────────────────────────────────────────────────────


def ensure_label(cfg: GmailConfig, name: str) -> Optional[str]:
    """Return the id of label ``name``, creating it if it does not exist.

    Gmail label names are case-sensitive but *uniqueness* is not, so the
    lookup is case-insensitive: asking for "events" when "Events" already
    exists must adopt it rather than fail to create a duplicate.
    """
    service = _get_service(cfg)
    if service is None:
        return None

    try:
        existing = service.users().labels().list(userId="me").execute()
        for lbl in existing.get("labels", []):
            if lbl.get("name", "").lower() == name.lower():
                return lbl.get("id")
    except Exception as e:
        logger.error("Gmail: could not list labels: %s", e)
        return None

    try:
        created = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        logger.info("Gmail: created label %r", name)
        return created.get("id")
    except Exception as e:
        logger.error("Gmail: could not create label %r: %s", name, e)
        return None


def apply_label(cfg: GmailConfig, message_ids: list[str], label_id: str) -> int:
    """Add ``label_id`` to every message. Returns how many were labelled.

    Uses ``batchModify``, which takes up to 1000 ids in one call and returns
    no body. It is **additive** — this adds a label, it never removes one and
    never moves a message out of the inbox. Relabelling an already-labelled
    message is a no-op, so this is safe to re-run.
    """
    if not message_ids or not label_id:
        return 0
    service = _get_service(cfg)
    if service is None:
        return 0

    done = 0
    for i in range(0, len(message_ids), 1000):
        chunk = message_ids[i:i + 1000]
        try:
            service.users().messages().batchModify(
                userId="me", body={"ids": chunk, "addLabelIds": [label_id]},
            ).execute()
            done += len(chunk)
        except Exception as e:
            logger.error("Gmail: failed to label %d message(s): %s", len(chunk), e)
    if done:
        logger.info("Gmail: labelled %d message(s)", done)
    return done


def label_event_messages(
    cfg: GmailConfig, events: list[dict], name: str = "events",
) -> int:
    """Apply ``name`` to every Gmail message that produced an event.

    Reads the ``_source_ids`` back-reference that `analysis` attaches during
    extraction. Events with no confident back-reference are skipped rather
    than guessed at — this writes to a real mailbox, and a mislabelled
    message is worse than an unlabelled one.
    """
    ids: list[str] = []
    for ev in events:
        for mid in ev.get("_source_ids") or []:
            if mid not in ids:
                ids.append(mid)
    if not ids:
        logger.info("Gmail: no event had a resolved source message; nothing labelled")
        return 0

    label_id = ensure_label(cfg, name)
    if not label_id:
        return 0
    return apply_label(cfg, ids, label_id)


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
