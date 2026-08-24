"""Instagram — ask for the posts instead of waiting for them.

The pre-existing Instagram source was a *listener*: open the logged-in home
feed, scroll for 25 seconds, and parse whatever GraphQL happened to fly past.
That has three problems, and only the third one is about code.

1. **The home feed is not ours to choose.** It is ranked by Meta for
   engagement, so which venues appear — and whether any appear — changes run
   to run. A venue that posts its Friday lineup at 4pm on Thursday is in the
   feed only if the algorithm decided so.
2. **Scrolling is a slow way to ask a question.** 25s of dwell yields whatever
   ~4 XHRs the page chose to fire.
3. **The walker only understood two shapes.** It matched ``edges`` and
   ``items`` structurally and never recursed into lists, so a payload nesting
   its posts one array deeper produced silence rather than an error.

This module inverts the first two. Instagram's own web client talks to
``/graphql/query`` with **persisted queries** — a numeric ``doc_id`` plus a
JSON ``variables`` blob. We do not hardcode either. We *record* the queries
the real page issues, then **replay one with a patched cursor**, which is
exactly what the page does when you scroll — minus the scrolling.

Recording rather than hardcoding is the load-bearing choice. Every hand-rolled
Instagram client rots the day Meta rotates a ``doc_id`` or adds a required
``__csr``/``lsd``/``jazoest`` parameter. Here those parameters are whatever
the page just sent, because we reuse its request body verbatim and change one
field. When Meta rotates a query, the next run records the new one.

`unverified`: the replay path has not been exercised against a live logged-in
session in this repo — that needs credentials this environment does not have.
Everything is written to degrade to the recorded first page (which is the old
behaviour, only parsed better) if the replay returns non-JSON, an error
payload, or nothing. See ``docs/03 - Architecture/03 - Sources — Web.md``.

Handle discovery (:func:`discover_handles`) is a separate, browser-free half:
venues link their own Instagram from their own website, so the account list is
*read off the venues*, never guessed. This matters more than it sounds — Oasis
is ``@theoasissf``, not ``@sfoasis``, and a guessed handle fails silently by
returning an empty account rather than an error.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlencode

from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.instagram")
tracer = get_tracer("event_harvester.sources.instagram")

# ── Constants ─────────────────────────────────────────────────────────

PROFILE_URL = "https://www.instagram.com/{handle}/"
POST_URL = "https://www.instagram.com/p/{code}/"
REEL_URL = "https://www.instagram.com/reel/{code}/"
GRAPHQL_URL_RE = re.compile(r"instagram\.com/(api/)?graphql", re.I)

# Hard ceiling on replayed pages per account, independent of config. Each page
# is one request Instagram did not ask for; a config typo should not turn into
# a hundred of them.
MAX_PAGES_HARD_CAP = 8

# Keys that mark an injected ad rather than a post by the account we asked for.
_AD_KEYS = ("ad_id", "is_ad", "injected", "ad_action", "is_paid_ad")

# Caption text below this is a bare emoji or a "🔥🔥" — nothing to extract.
MIN_CAPTION_CHARS = 10


class InstagramLoginRequired(RuntimeError):
    """Raised when Instagram serves a login wall instead of content.

    Distinct from "no posts found" on purpose: an expired session and a quiet
    account are the same empty list downstream, and only one of them is fixed
    by running ``event-harvester web login``.
    """


# ── Post normalisation (shared with feed mode) ────────────────────────
#
# The old walker keyed on *container* shape (``edges``/``items``). This one
# keys on the *object* shape — "a dict carrying a caption and an id is a
# post" — and recurses through lists as well as dicts. That single change is
# what makes it survive Instagram renaming its connection fields, which it
# does routinely (``edge_owner_to_timeline_media`` →
# ``xdt_api__v1__feed__user_timeline_graphql_connection``).


def _caption_text(media: dict) -> str:
    """Caption text, whether it arrived as a dict, a string, or not at all."""
    caption = media.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "")
    if isinstance(caption, str):
        return caption
    # Some shapes hoist the text up a level (``edge_media_to_caption``).
    edge = media.get("edge_media_to_caption")
    if isinstance(edge, dict):
        edges = edge.get("edges")
        if isinstance(edges, list) and edges:
            node = edges[0].get("node") if isinstance(edges[0], dict) else None
            if isinstance(node, dict):
                return str(node.get("text") or "")
    return ""


def _media_id(media: dict) -> str:
    """Stable id for one post: ``pk`` if present, else ``id``, else ``code``."""
    for key in ("pk", "id", "code", "shortcode"):
        val = media.get(key)
        if isinstance(val, (str, int)) and str(val):
            return str(val)
    return ""


def looks_like_media(obj: Any) -> bool:
    """True when ``obj`` is a post-shaped dict.

    Requires a caption *and* an id. Either alone matches too much: comments
    carry text, and half the objects in a GraphQL payload carry an id.
    """
    if not isinstance(obj, dict):
        return False
    if not _media_id(obj):
        return False
    return bool(_caption_text(obj))


def iter_media(payload: Any, *, max_depth: int = 10) -> Iterator[dict]:
    """Yield every post-shaped dict in ``payload``, depth-first.

    Walks lists as well as dicts — the old walker did not, which is why a
    payload whose posts sat inside an array of sections produced zero posts
    and no error. Does not descend *into* a matched media object, so a
    carousel's child items don't each become a separate event.

    Yields in **document order**, which for a timeline connection is
    newest-first. Callers cap on that order, so a stack that popped children
    in reverse would quietly hand back the *oldest* posts of each account.
    """
    stack: list[tuple[Any, int]] = [(payload, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            if looks_like_media(node):
                yield node
                continue
            children = list(node.values())
        elif isinstance(node, list):
            children = list(node)
        else:
            continue
        # Reversed, because the stack pops from the end: this is what makes
        # the traversal document-ordered rather than mirror-imaged.
        for child in reversed(children):
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))


def _is_ad(media: dict) -> bool:
    return any(media.get(key) for key in _AD_KEYS)


def _post_timestamp(media: dict) -> str:
    """ISO-8601 post time, or ``""`` when the payload carries none.

    Instagram spells this four ways depending on which query answered.
    """
    caption = media.get("caption")
    candidates = [
        media.get("taken_at"),
        media.get("taken_at_timestamp"),
        caption.get("created_at") if isinstance(caption, dict) else None,
        caption.get("created_at_utc") if isinstance(caption, dict) else None,
    ]
    for raw in candidates:
        if raw in (None, "", 0):
            continue
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
        except (ValueError, OSError, TypeError):
            continue
    return ""


def _permalink(media: dict) -> str:
    """Public URL of the post, from its shortcode. ``""`` if absent."""
    code = media.get("code") or media.get("shortcode")
    if not isinstance(code, str) or not code:
        return ""
    # /p/<code>/ resolves for reels too, but the canonical URL is the one a
    # human would recognise from the app.
    if str(media.get("product_type") or "") in ("clips", "reel"):
        return REEL_URL.format(code=code)
    return POST_URL.format(code=code)


def _username(media: dict) -> str:
    for key in ("user", "owner"):
        holder = media.get(key)
        if isinstance(holder, dict) and holder.get("username"):
            return str(holder["username"])
    return ""


def _location_line(media: dict) -> str:
    loc = media.get("location")
    if not isinstance(loc, dict):
        return ""
    name = str(loc.get("name") or "").strip()
    if not name:
        return ""
    city = str(loc.get("city") or "").strip()
    return f"at: {name}" + (f", {city}" if city and city.lower() not in name.lower() else "")


def _image_url(media: dict) -> str:
    """The post's best still image, for OCR. Instagram serves this several ways
    depending on which endpoint produced the payload; try them largest-first.

    This is the whole point of OCR for Instagram: the code already notes an
    image-only flyer "carries its real content in the image" and fell back to
    Instagram's accessibility caption, which is usually the useless "may be an
    image of ..." form. The real poster text is in the pixels.
    """
    iv = media.get("image_versions2")
    if isinstance(iv, dict):
        cands = iv.get("candidates")
        if isinstance(cands, list) and cands:
            best = max(cands, key=lambda c: (c.get("width", 0) or 0) * (c.get("height", 0) or 0))
            if best.get("url"):
                return str(best["url"])
    for key in ("display_url", "thumbnail_src"):
        if media.get(key):
            return str(media[key])
    dr = media.get("display_resources")
    if isinstance(dr, list) and dr:
        best = max(dr, key=lambda c: c.get("config_width", 0) or 0)
        if best.get("src"):
            return str(best["src"])
    return ""


def normalize_media(
    media: dict,
    *,
    source_name: str,
    channel: str | None = None,
    fallback_handle: str = "",
) -> dict | None:
    """One post-shaped dict → one message record, or ``None`` if unusable.

    ``content`` leads with the permalink because ``analysis.build_prompt``
    truncates content to 400 chars: a link appended after a long caption is a
    link the LLM never sees, and an Instagram event with no link back is an
    event nobody can check.
    """
    text = _caption_text(media).strip()
    if len(text) < MIN_CAPTION_CHARS or _is_ad(media):
        return None

    username = _username(media) or fallback_handle or "?"
    link = _permalink(media)
    location = _location_line(media)

    # An image-only flyer post ("🌈 tonight!!" + a poster) carries its real
    # content in the image. Instagram's own alt text sometimes transcribes it;
    # attach it only when the caption is too thin to stand alone, since it is
    # usually the useless "may be an image of one person" form.
    parts = [p for p in (link, location, text) if p]
    if len(text) < 80:
        alt = str(media.get("accessibility_caption") or "").strip()
        if alt and "may be an image of" not in alt.lower():
            parts.append(f"alt: {alt}")

    media_id = _media_id(media)
    record = {
        "platform": "web",
        "id": f"ig:{media_id}" if media_id else f"ig:{hash(text[:50])}",
        "timestamp": _post_timestamp(media) or datetime.now(timezone.utc).isoformat(),
        "author": f"@{username}",
        "channel": channel or f"{source_name}-feed",
        "content": "\n".join(parts)[:2000],
    }
    # Hand the poster image to OCR enrichment — an Instagram event post is very
    # often a flyer whose caption is just "🌈 tonight!!"; the date and venue are
    # in the image.
    img = _image_url(media)
    if img:
        record["images"] = [img]
    return record


def normalize_posts(
    payload: Any,
    *,
    source_name: str,
    channel: str | None = None,
    seen: set[str] | None = None,
    fallback_handle: str = "",
) -> list[dict]:
    """Every post in one API payload, as message records.

    Pass a shared ``seen`` set across payloads from one run: Instagram repeats
    the same media in the timeline query and again in a prefetch, so without
    it a single scroll yields the same night twice.
    """
    out: list[dict] = []
    seen = seen if seen is not None else set()
    for media in iter_media(payload):
        msg = normalize_media(
            media, source_name=source_name, channel=channel,
            fallback_handle=fallback_handle,
        )
        if msg is None:
            continue
        if msg["id"] in seen:
            continue
        seen.add(msg["id"])
        out.append(msg)
    return out


# ── GraphQL: record, then replay with our own cursor ──────────────────


def loads_multi(text: str) -> list[dict]:
    """Parse a response body that may hold several JSON objects.

    Instagram streams ``@defer``'d GraphQL as newline-delimited chunks. The
    old code ran ``json.loads`` on the whole body, caught ``JSONDecodeError``,
    and dropped it — so the multi-chunk responses (which are the ones carrying
    the deferred timeline) were exactly the ones silently discarded.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
        return [obj] if isinstance(obj, dict) else []
    except json.JSONDecodeError:
        pass
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def find_page_info(payload: Any, *, max_depth: int = 10) -> dict | None:
    """The first ``page_info``-shaped dict with a cursor, anywhere in ``payload``.

    Searched structurally rather than by path for the same reason as
    :func:`iter_media`: the path is renamed often, the shape never is.
    """
    stack: list[tuple[Any, int]] = [(payload, 0)]
    best: dict | None = None
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            if "end_cursor" in node or ("has_next_page" in node and "next_max_id" in node):
                cursor = node.get("end_cursor") or node.get("next_max_id")
                if cursor:
                    info = {
                        "end_cursor": str(cursor),
                        "has_next_page": bool(node.get("has_next_page", True)),
                    }
                    if info["has_next_page"]:
                        return info
                    best = best or info
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
    return best


@dataclass
class GraphQLQuery:
    """One persisted query, captured verbatim from the live page.

    ``body`` is the form-encoded POST body already split into fields — it
    carries ``doc_id``, ``variables``, and the dozen ``__``-prefixed session
    parameters we neither understand nor need to: replaying them unchanged is
    the entire trick.
    """

    url: str
    friendly_name: str
    body: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def doc_id(self) -> str:
        return self.body.get("doc_id", "")

    def variables(self) -> dict:
        try:
            parsed = json.loads(self.body.get("variables") or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def with_cursor(self, cursor: str) -> str:
        """This query's body, form-encoded, asking for the page after ``cursor``.

        Only keys the original request already used are touched, plus a
        top-level ``after`` — which is what the page itself sets when you
        scroll. Nothing else in the body is rewritten.
        """
        variables = self.variables()
        variables["after"] = cursor
        for key in ("before", "last"):
            if key in variables:
                variables[key] = None
        data = variables.get("data")
        if isinstance(data, dict) and "after" in data:
            data["after"] = cursor
        # ``max_id`` is the REST-flavoured spelling used by the api/v1 queries.
        if "max_id" in variables:
            variables["max_id"] = cursor
        body = dict(self.body)
        body["variables"] = json.dumps(variables, separators=(",", ":"))
        return urlencode(body)

    def fetch_headers(self) -> dict[str, str]:
        """Headers safe to pass to an in-page ``fetch``.

        Cookies, length and host are the browser's job; sending them from JS
        is either ignored or an error. Everything Instagram actually checks
        (``x-csrftoken``, ``x-ig-app-id``, ``x-fb-lsd``) is an ``x-`` header.
        """
        out = {}
        for key, value in self.headers.items():
            k = key.lower()
            if k == "content-type" or k.startswith("x-"):
                out[key] = value
        out.setdefault("content-type", "application/x-www-form-urlencoded")
        return out


def _parse_form_body(body: str | None) -> dict[str, str]:
    from urllib.parse import parse_qsl

    if not body:
        return {}
    try:
        return dict(parse_qsl(body, keep_blank_values=True))
    except Exception:  # noqa: BLE001 — malformed bodies are not our problem
        return {}


def _friendly_name(request) -> str:
    """Query name from the request, header first then body."""
    try:
        header = request.headers.get("x-fb-friendly-name")
    except Exception:  # noqa: BLE001 — Playwright objects are duck-typed here
        header = None
    if header:
        return str(header)
    return _parse_form_body(getattr(request, "post_data", None)).get(
        "fb_api_req_friendly_name", "",
    )


class QueryRecorder:
    """Watches one page and remembers how it asks Instagram for posts.

    Holds two things: the request templates (so we can ask again) and the
    response payloads (so the first page costs nothing extra — it already
    loaded). Attach before navigating; the page's first burst of queries is
    the one worth having.
    """

    def __init__(self) -> None:
        self.queries: dict[str, GraphQLQuery] = {}
        self.payloads: list[tuple[str, dict]] = []  # (friendly_name, payload)
        self.cursors: dict[str, str] = {}
        self.order: list[str] = []  # friendly names, most recent last

    # -- capture --------------------------------------------------------

    def _on_request(self, request) -> None:
        try:
            if request.method != "POST" or not GRAPHQL_URL_RE.search(request.url):
                return
            body = _parse_form_body(request.post_data)
            if "doc_id" not in body and "query_hash" not in body:
                return
            name = _friendly_name(request) or body.get("doc_id", "query")
            self.queries[name] = GraphQLQuery(
                url=request.url,
                friendly_name=name,
                body=body,
                headers=dict(request.headers),
            )
        except Exception as e:  # noqa: BLE001 — a listener must never raise
            logger.debug("instagram: request capture failed: %s", e)

    def _on_response(self, response) -> None:
        try:
            if not GRAPHQL_URL_RE.search(response.url):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            name = _friendly_name(response.request)
            for payload in loads_multi(response.text()):
                self.absorb(name, payload)
        except Exception as e:  # noqa: BLE001 — bodies expire, pages close
            logger.debug("instagram: response capture failed: %s", e)

    def attach(self, page) -> "QueryRecorder":
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        return self

    # -- state ----------------------------------------------------------

    def absorb(self, name: str, payload: dict) -> None:
        """Record a payload and whatever cursor it carries."""
        self.payloads.append((name, payload))
        info = find_page_info(payload)
        if info and info.get("has_next_page") and info.get("end_cursor"):
            self.cursors[name] = info["end_cursor"]
            if name in self.order:
                self.order.remove(name)
            self.order.append(name)
        elif info and not info.get("has_next_page"):
            self.cursors.pop(name, None)
            if name in self.order:
                self.order.remove(name)

    def next_target(self) -> tuple[GraphQLQuery, str] | None:
        """The query to replay next, and the cursor to ask it for.

        Prefers the most recently-advanced connection: on a profile page that
        is the posts grid, which is the one we came for.
        """
        for name in reversed(self.order):
            cursor = self.cursors.get(name)
            query = self.queries.get(name)
            if cursor and query:
                return query, cursor
        return None


def replay_query(page, query: GraphQLQuery, cursor: str) -> list[dict]:
    """Re-issue ``query`` from inside the page, asking for the next page.

    Runs as ``fetch`` in the page's own context rather than through a Python
    HTTP client so cookies, TLS fingerprint, and origin are the session's —
    a request that looks like it came from somewhere else is a request
    Instagram is entitled to refuse.
    """
    script = """
        async ([url, body, headers]) => {
            const res = await fetch(url, {
                method: "POST",
                headers: headers,
                body: body,
                credentials: "include",
            });
            return {status: res.status, text: await res.text()};
        }
    """
    try:
        result = page.evaluate(
            script, [query.url, query.with_cursor(cursor), query.fetch_headers()],
        )
    except Exception as e:  # noqa: BLE001 — page may have navigated/closed
        logger.debug("instagram: replay of %s failed: %s", query.friendly_name, e)
        return []
    status = (result or {}).get("status")
    if status != 200:
        logger.info(
            "instagram: %s replay returned HTTP %s — falling back to what loaded.",
            query.friendly_name, status,
        )
        return []
    return loads_multi((result or {}).get("text", ""))


def paginate(
    page,
    recorder: QueryRecorder,
    *,
    max_pages: int,
    delay_ms: int = 2500,
    tracer=None,
) -> int:
    """Replay the feed query up to ``max_pages`` times. Returns pages fetched.

    Stops at the first page that yields no cursor — which is both "the account
    has no more posts" and "Instagram declined", and is treated the same way
    on purpose: in either case the next request is one we should not send.
    """
    pages = 0
    for _ in range(min(max_pages, MAX_PAGES_HARD_CAP)):
        target = recorder.next_target()
        if target is None:
            break
        query, cursor = target
        # Clear the cursor first: if the replay comes back without one, the
        # loop must not re-ask the same question forever.
        recorder.cursors.pop(query.friendly_name, None)
        payloads = replay_query(page, query, cursor)
        if not payloads:
            break
        for payload in payloads:
            recorder.absorb(query.friendly_name, payload)
        pages += 1
        _trace(tracer, None, None, kind="xhr", url=query.url, label="graphql-replay")
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:  # noqa: BLE001 — page closing mid-wait is not fatal
            break
    return pages


# ── Account harvesting ────────────────────────────────────────────────


def is_login_wall(page) -> bool:
    """True when the page is Instagram's login screen rather than content."""
    try:
        if "/accounts/login" in (page.url or ""):
            return True
        return page.query_selector("input[name='password']") is not None
    except Exception as e:  # noqa: BLE001 — a closed page is not a login wall
        logger.debug("instagram: login-wall check failed: %s", e)
        return False


def _open_recorded(uc, url: str, *, wait_ms: int, label: str, tracer=None, capture=None):
    """Open ``url`` with a :class:`QueryRecorder` attached before navigation.

    The recorder has to exist before ``goto`` — the queries worth having are
    the ones the page fires on first paint, and a listener attached after that
    sees none of them. Returns ``(page, recorder)``.

    ``tracer``/``capture`` are the ``--debug-web-trace`` pair. One row per
    account is the useful granularity here: unlike a scroll-and-scrape mode
    there is no interesting intermediate state, just "we asked, this is what
    the profile looked like".
    """
    page = uc._context.new_page()
    if getattr(uc, "_stealth_plugin", None):
        try:
            uc._stealth_plugin.apply_stealth_sync(page)
        except Exception as e:  # noqa: BLE001 — stealth is best-effort
            logger.debug("instagram: stealth failed for %s: %s", label, e)

    recorder = QueryRecorder().attach(page)
    page.goto(url, timeout=uc.timeout_ms, wait_until="domcontentloaded")
    page.wait_for_timeout(wait_ms)
    _trace(tracer, capture, page, kind="open", url=url, label=label)
    return page, recorder


def _trace(tracer, capture, page, *, kind: str, url: str, label: str) -> None:
    """Best-effort trace row; a debugging aid must never break a harvest."""
    if tracer is None:
        return
    try:
        if capture is not None and page is not None:
            tracer.record_with_screenshot(
                kind=kind, url=url, page=page, capture=capture, label=label,
            )
        else:
            tracer.record(kind=kind, url=url, label=label)
    except Exception as e:  # noqa: BLE001 — tracer is duck-typed
        logger.debug("instagram: trace row failed (%s): %s", label, e)


def _drain(
    recorder: QueryRecorder, *, source_name: str, channel: str,
    max_posts: int, fallback_handle: str = "",
) -> list[dict]:
    """Every recorded payload → message records, deduped, capped."""
    seen: set[str] = set()
    messages: list[dict] = []
    for _name, payload in recorder.payloads:
        messages.extend(
            normalize_posts(
                payload, source_name=source_name, channel=channel,
                seen=seen, fallback_handle=fallback_handle,
            ),
        )
        if len(messages) >= max_posts:
            break
    return messages[:max_posts]


def harvest_account(
    uc,
    handle: str,
    *,
    source_name: str = "instagram",
    max_pages: int = 1,
    max_posts: int = 24,
    delay_ms: int = 2500,
    wait_ms: int = 3500,
    tracer=None,
    capture=None,
) -> list[dict]:
    """One account's recent posts as message records.

    Opens the profile (which loads page 1 by itself), then replays the grid
    query for further pages. ``max_pages`` counts *extra* pages, so the
    default of 1 is two requests total — one Instagram expected and one it
    did not.

    Raises :class:`InstagramLoginRequired` rather than returning ``[]`` when
    the session is dead, so the caller can stop instead of walking the rest of
    the account list into the same wall.
    """
    handle = handle.lstrip("@").strip("/")
    url = PROFILE_URL.format(handle=handle)
    channel = f"instagram/@{handle}"

    page, recorder = _open_recorded(
        uc, url, wait_ms=wait_ms, label=handle, tracer=tracer, capture=capture,
    )
    try:
        if is_login_wall(page):
            raise InstagramLoginRequired(
                f"Instagram served a login wall for @{handle} — "
                "run `event-harvester web login` to refresh the session.",
            )

        pages = paginate(
            page, recorder, max_pages=max_pages, delay_ms=delay_ms, tracer=tracer,
        )
        messages = _drain(
            recorder, source_name=source_name, channel=channel,
            max_posts=max_posts, fallback_handle=handle,
        )

        logger.info(
            "instagram: @%s — %d post(s) from %d recorded response(s) (+%d replayed page(s)).",
            handle, len(messages), len(recorder.payloads), pages,
        )
        if not messages and not recorder.queries:
            logger.warning(
                "instagram: @%s produced no GraphQL at all — the profile may be "
                "private, renamed, or served from cache.", handle,
            )
        return messages
    finally:
        try:
            page.close()
        except Exception as e:  # noqa: BLE001 — already-closed page
            logger.debug("instagram: page close failed for %s: %s", handle, e)


def harvest_home_feed(
    uc,
    *,
    url: str = "https://www.instagram.com/",
    source_name: str = "instagram",
    max_pages: int = 2,
    max_posts: int = 30,
    delay_ms: int = 2500,
    wait_ms: int = 4000,
    tracer=None,
    capture=None,
) -> list[dict]:
    """The logged-in home timeline, paginated by cursor rather than scrolled.

    Kept because it is the only path that surfaces accounts nobody thought to
    list. It is the *weaker* half of this source — Meta ranks it, so coverage
    is neither complete nor repeatable — which is why it is off by default
    once an account list exists.
    """
    page, recorder = _open_recorded(
        uc, url, wait_ms=wait_ms, label="home", tracer=tracer, capture=capture,
    )
    try:
        if is_login_wall(page):
            raise InstagramLoginRequired(
                "Instagram served a login wall for the home feed — "
                "run `event-harvester web login` to refresh the session.",
            )
        pages = paginate(
            page, recorder, max_pages=max_pages, delay_ms=delay_ms, tracer=tracer,
        )
        messages = _drain(
            recorder, source_name=source_name, channel=f"{source_name}-feed",
            max_posts=max_posts,
        )
        logger.info(
            "instagram: home feed — %d post(s), %d replayed page(s).",
            len(messages), pages,
        )
        return messages
    finally:
        try:
            page.close()
        except Exception as e:  # noqa: BLE001 — already-closed page
            logger.debug("instagram: home page close failed: %s", e)


# ── Handle discovery (browser-free) ───────────────────────────────────
#
# Same principle as feed_probe: the site tells you, you do not guess. The
# difference is that a handle cannot be *validated* the way a feed can —
# logged-out, instagram.com/<anything>/ answers 200 whether or not the
# account exists (checked 2026-08-06), so a fetch proves nothing. Ranking
# therefore has to carry the weight validation carries there.

_IG_LINK_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)

# Paths that are Instagram's, not an account's.
_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "about", "legal", "developer", "developers", "api", "graphql", "web",
    "sharer", "share", "oauth", "challenge", "emails", "privacy", "terms",
    "help", "press", "blog", "ads", "business", "creators", "shop", "static",
    "embed", "u", "v", "s", "t", "session", "login", "signup", "download",
}

# Two normalised strings sharing a run this long are the same brand. Tuned
# against the live source list: "beaux"/"beauxdragbrunch" is exactly 5, and
# nothing shorter separated a venue from a promoter it links to.
MIN_AFFINITY = 5


@dataclass
class HandleCandidate:
    """An Instagram handle found on a venue's site, and how much to trust it."""

    handle: str
    count: int = 0
    affinity: int = 0
    page_url: str = ""

    @property
    def confident(self) -> bool:
        """Whether this may be adopted without a human looking at it."""
        return self.affinity >= MIN_AFFINITY

    @property
    def rank_key(self) -> tuple:
        return (-self.affinity, -self.count, self.handle)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle, "count": self.count,
            "affinity": self.affinity, "confident": self.confident,
            "page_url": self.page_url,
        }


def _normalize_token(value: str) -> str:
    """Lowercase alphanumerics only — ``sf-eagle.com`` and ``sfeagle`` match."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _longest_common_run(a: str, b: str) -> int:
    """Length of the longest substring shared by ``a`` and ``b``."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def handle_affinity(handle: str, *names: str) -> int:
    """How strongly ``handle`` belongs to a site called any of ``names``.

    Returns the longest shared run of characters. A venue's own account shares
    its name; the drag night it cross-promotes usually does not.
    """
    h = _normalize_token(handle)
    if not h:
        return 0
    best = 0
    for name in names:
        n = _normalize_token(name)
        if not n:
            continue
        if n == h:
            return len(h)
        best = max(best, _longest_common_run(h, n))
    return best


def _plausible_handle(handle: str) -> bool:
    """Reject Instagram's own paths and CDN fragments that look like handles."""
    h = handle.lower().strip(".")
    if not h or h in _RESERVED:
        return False
    if len(h) < 3:
        return False
    # ``t51.2885`` and friends come from image CDN URLs on the same host.
    if sum(c.isalpha() for c in h) < 3:
        return False
    return bool(re.fullmatch(r"[a-z0-9_][a-z0-9_.]{1,28}[a-z0-9_]", h))


def extract_handles(html: str, *, page_url: str = "", names: tuple = ()) -> list[HandleCandidate]:
    """Rank every Instagram handle linked from one page of HTML."""
    counts: dict[str, int] = {}
    for match in _IG_LINK_RE.finditer(html or ""):
        handle = match.group(1).lower()
        if not _plausible_handle(handle):
            continue
        counts[handle] = counts.get(handle, 0) + 1

    from urllib.parse import urlparse

    host = urlparse(page_url).netloc if page_url else ""
    match_names = tuple(n for n in (*names, host.replace("www.", "")) if n)

    cands = [
        HandleCandidate(
            handle=h, count=c,
            affinity=handle_affinity(h, *match_names),
            page_url=page_url,
        )
        for h, c in counts.items()
    ]
    return sorted(cands, key=lambda c: c.rank_key)


def discover_handles(
    url: str, *, names: tuple = (), timeout_s: int = 15,
) -> list[HandleCandidate]:
    """Fetch one page and rank the Instagram accounts it links to.

    No browser, one request. Reuses ``feed_probe``'s fetcher so both probes
    present the same UA and the same 2MB cap to a venue's server.
    """
    from event_harvester.sources.feed_probe import _fetch_page

    with tracer.start_as_current_span("instagram.discover_handles") as span:
        span.set_attribute("http.url", url)
        html = _fetch_page(url, timeout_s)
        if not html:
            span.set_attribute("candidate.count", 0)
            return []
        cands = extract_handles(html, page_url=url, names=names)
        span.set_attribute("candidate.count", len(cands))
        span.set_attribute(
            "candidate.confident_count", sum(1 for c in cands if c.confident),
        )
        return cands


def best_handle(url: str, **kw) -> HandleCandidate | None:
    """Top-ranked *confident* handle for a site, or ``None``.

    Returning ``None`` rather than the best guess is deliberate: a wrong
    handle is a source that harvests someone else's posts forever, and the
    only symptom is events that don't belong to the venue.
    """
    for cand in discover_handles(url, **kw):
        if cand.confident:
            return cand
    return None
