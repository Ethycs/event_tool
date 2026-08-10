"""Instagram mode: harvest named accounts over GraphQL, not the ranked feed.

`feed` mode already handles instagram.com — badly, and for a reason that is
about the site rather than the code. A feed source scrolls whatever the page
decides to show; on Instagram that is a Meta-ranked timeline, so "did we see
the Oasis lineup this week" has no answer. This mode asks for accounts by
name instead: the venues, in order, one page each.

Why a mode rather than a `steps` route: a route is a walk over the *DOM*, and
none of the L1 verbs can express "re-issue the persisted query with the next
cursor". The mechanism lives in ``sources/instagram.py``; this file is only
policy — which accounts, how many pages, how long to wait between them.

Rate discipline is the design constraint here, not throughput. One profile
load plus ``max_pages`` replays per account, a delay between every request,
and a hard stop the moment Instagram shows a login wall — walking twenty
accounts into the same wall is how a session gets flagged.
"""

from __future__ import annotations

import logging
import time

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.instagram import (
    InstagramLoginRequired,
    harvest_account,
    harvest_home_feed,
)

logger = logging.getLogger("event_harvester.web_fetch")

# Config defaults. Extra pages *per account* — one profile load already
# carries ~12 posts, which is a fortnight for a venue that posts every other
# day, so the default asks for one more and stops.
DEFAULT_MAX_PAGES = 1
DEFAULT_MAX_POSTS = 24
DEFAULT_DELAY_S = 2.5
DEFAULT_ACCOUNT_GAP_S = 3.0
# A ceiling on how far one config edit can grow the request budget: 25
# accounts is already ~3 minutes of paced loading.
DEFAULT_MAX_ACCOUNTS = 25


def _normalize_accounts(src: dict) -> list[dict]:
    """Read ``accounts`` into ``[{handle, name}]``.

    Accepts ``"studsf"``, ``"@studsf"``, a profile URL, or
    ``{"handle": "studsf", "name": "stud"}`` — the dict form exists so the
    venue's name in ``web_sources.json`` survives into the report, which a
    bare handle would not tell you (``@theoasissf`` is Oasis).
    """
    raw = src.get("accounts") or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, dict):
            handle = str(entry.get("handle") or "").strip()
            name = str(entry.get("name") or "").strip()
        else:
            handle, name = str(entry).strip(), ""
        handle = handle.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        if not handle or handle.lower() in seen:
            continue
        seen.add(handle.lower())
        out.append({"handle": handle, "name": name or handle})
    return out


def _interleave(per_account: list[list[dict]], limit: int) -> list[dict]:
    """Merge each account's posts round-robin, newest first within an account.

    The cap that matters here is ``max_events`` — 30 by default, against 20
    venues. Concatenating and truncating would spend the whole budget on the
    first two accounts in config order, so the same two venues would be the
    only ones ever seen and the rest would look like they never post. Every
    venue's newest post is taken before any venue's second.
    """
    ordered = [
        sorted(posts, key=lambda m: m.get("timestamp", ""), reverse=True)
        for posts in per_account if posts
    ]
    out: list[dict] = []
    for rank in range(max((len(p) for p in ordered), default=0)):
        for posts in ordered:
            if rank < len(posts):
                out.append(posts[rank])
                if len(out) >= limit:
                    return out
    return out


def _do_instagram(
    uc, src: dict, max_events: int = 30, page=None, tracer=None, capture=None,
) -> list[dict]:
    """Instagram mode (OTel-wrapped). Body lives in ``_do_instagram_inner``."""
    name = src.get("name", "instagram")
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.instagram") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", "instagram")
        try:
            messages = _do_instagram_inner(
                uc, src, max_events=max_events, page=page,
                tracer=tracer, capture=capture,
            )
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _do_instagram_inner(
    uc, src: dict, max_events: int = 30, page=None, tracer=None, capture=None,
) -> list[dict]:
    """Walk the configured accounts, then optionally the home feed.

    The pre-opened ``page`` is closed on entry: every request here has to be
    recorded from first paint, and a page that has already navigated cannot
    be. Same trade-off `feed` mode documents, same reason.
    """
    if page is not None:
        try:
            page.close()
        except Exception as e:  # noqa: BLE001 — already-closed page
            logger.debug("instagram: pre-opened page close failed: %s", e)

    name = src.get("name", "instagram")
    accounts = _normalize_accounts(src)
    max_accounts = int(src.get("max_accounts", DEFAULT_MAX_ACCOUNTS))
    if len(accounts) > max_accounts:
        logger.warning(
            "Instagram: %d accounts configured, visiting the first %d "
            "(raise max_accounts to widen).", len(accounts), max_accounts,
        )
        accounts = accounts[:max_accounts]
    max_pages = int(src.get("max_pages", DEFAULT_MAX_PAGES))
    max_posts = int(src.get("max_posts", DEFAULT_MAX_POSTS))
    delay_ms = int(float(src.get("request_delay_s", DEFAULT_DELAY_S)) * 1000)
    gap_ms = int(float(src.get("account_gap_s", DEFAULT_ACCOUNT_GAP_S)) * 1000)
    # The home feed is the fallback for having no account list, not a
    # supplement to one — asking for it as well doubles the request budget to
    # re-read posts the account walk already has.
    include_home = bool(src.get("include_home_feed", not accounts))

    per_account: list[list[dict]] = []
    walked = 0

    if accounts:
        logger.info("Instagram: %d account(s), %d extra page(s) each.", len(accounts), max_pages)
    for account in accounts:
        try:
            posts = harvest_account(
                uc, account["handle"],
                source_name=name,
                max_pages=max_pages,
                max_posts=max_posts,
                delay_ms=delay_ms,
                tracer=tracer,
                capture=capture,
            )
        except InstagramLoginRequired as e:
            # Stop the whole source, not just this account: every remaining
            # one would hit the same wall, and twenty profile loads that all
            # bounce is exactly the pattern that gets a session challenged.
            logger.error(
                "Instagram: %s Skipping %d remaining account(s).",
                e, len(accounts) - walked,
            )
            break
        except Exception as e:  # noqa: BLE001 — one bad account must not end the run
            logger.warning("Instagram: @%s failed: %s", account["handle"], e)
            posts = []
        walked += 1
        if posts:
            per_account.append(posts)
        if walked < len(accounts):
            time.sleep(gap_ms / 1000)

    messages = _interleave(per_account, max_events)

    if include_home and len(messages) < max_events:
        try:
            messages.extend(
                harvest_home_feed(
                    uc,
                    url=src.get("url", "https://www.instagram.com/"),
                    source_name=name,
                    max_pages=int(src.get("home_max_pages", 2)),
                    max_posts=max_events - len(messages),
                    delay_ms=delay_ms,
                    tracer=tracer,
                    capture=capture,
                ),
            )
        except InstagramLoginRequired as e:
            logger.error("Instagram: %s", e)
        except Exception as e:  # noqa: BLE001 — the account walk still counts
            logger.warning("Instagram: home feed failed: %s", e)

    harvested = sum(len(p) for p in per_account)
    if harvested > len(messages):
        logger.info(
            "Instagram: %d post(s) from %d account(s), trimmed to %d by max_events "
            "(newest first, one per venue per round — raise WEB_MAX_EVENTS to keep more).",
            harvested, walked, max_events,
        )
    else:
        logger.info("Instagram: %d post(s) from %d account(s).", len(messages), walked)
    return messages[:max_events]
