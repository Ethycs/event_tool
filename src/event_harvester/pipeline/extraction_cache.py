"""Persistent memoization of LLM extraction, keyed by message content.

Re-running a harvest re-sends every message that survives filtering to the
LLM, even though most are the same messages as the last run and most produce
no event at all. Once the Discord collector began feeding thousands of
messages in, that turned each harvest into an hours-long extract — 11,093
cached messages, no caps, one batch-of-five at a time (measured 2026-09-02).

This cache remembers, per message, what extraction did with it last time — an
event, several, or nothing — so an unchanged message never reaches the LLM
twice. The cost of a harvest then tracks *new* messages, not total volume.

**The safety rule is: never suppress an event we are not certain about.** A
false negative here is an event that silently stops appearing, which is far
worse than an avoidable LLM call. So:

- A message is cached as *no-event* **only** when its whole extraction batch
  produced zero events (the backend's ``llm_no_events`` bucket) — proof the
  message yielded nothing, not merely that nothing was linked to it.
- A message is cached *with* events **only** for events the extractor
  confidently linked back to it (``_source_ids``, set by
  ``attach_source_ids`` on a single unambiguous match).
- A message that reached the model in a batch that *did* produce events but
  none that linked to it, and any message whose batch errored, is **not**
  cached — it is re-extracted next run. Correctness over hit-rate.

The key is a SHA-256 of the message content, its platform, and a cache-format
version (:data:`_VERSION`). Editing a message is therefore a miss (its content
changed), and bumping ``_VERSION`` invalidates the whole cache at once when the
extraction prompt or event schema changes in a way that would make old results
wrong. Set ``EH_EXTRACTION_CACHE=0`` to disable it entirely.

**Known limitation (v1):** chat events whose date the LLM inferred *relative to
today* ("this Friday") are cached with the date from the run that first saw
them; if the same message text recurs on a later day the cached date can be
stale. Web-calendar events are immune — their date is stated in the message and
re-applied every run by ``analysis._carry_calendar_dates`` over the full
filtered set (cached messages included). Bump ``_VERSION`` if relative-date
drift ever bites.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import time

from event_harvester.utils import data_path, load_json, save_json

logger = logging.getLogger("event_harvester.extraction_cache")

# Bump to invalidate every entry at once — do this whenever the extraction
# prompt or the event schema changes such that a stored result would now be
# wrong. It is part of the key, so old entries simply stop matching.
_VERSION = "1"

CACHE_FILE = data_path(".extraction_cache.json")

# A message older than this is past the harvest window (`days_back`) and will
# never be looked up again; drop it so the file cannot grow without bound.
_TTL_SECONDS = 30 * 86_400
# Hard ceiling regardless of TTL, oldest evicted first. 100k entries of a few
# hundred bytes is a handful of MB — comfortably more than one window's volume.
_MAX_ENTRIES = 100_000

_DISABLED = {"0", "false", "off", "no"}


def enabled() -> bool:
    """Whether extraction caching is on (default yes; ``EH_EXTRACTION_CACHE=0`` off)."""
    return os.environ.get("EH_EXTRACTION_CACHE", "1").strip().lower() not in _DISABLED


def message_key(msg: dict) -> str:
    """Stable content hash for one message.

    Content + platform + format version. Two messages with identical text on
    the same platform share a key (and one extraction); any edit is a new key.
    """
    h = hashlib.sha256()
    h.update(_VERSION.encode())
    h.update(b"\x1f")
    h.update(str(msg.get("platform") or "").encode("utf-8", "replace"))
    h.update(b"\x1f")
    h.update((msg.get("content") or "").encode("utf-8", "replace"))
    return h.hexdigest()


def load() -> dict:
    """Load the cache, or an empty dict if absent/corrupt."""
    data = load_json(CACHE_FILE, default={})
    return data if isinstance(data, dict) else {}


def partition(messages: list[dict], cache: dict) -> tuple[list[dict], list[dict], int]:
    """Split ``messages`` into (to_extract, cached_events, n_hits).

    ``to_extract`` are misses that must go to the LLM. ``cached_events`` are
    **deep copies** of the events previously extracted from the hits — copied
    so the downstream date/link carry and dedup can mutate them without
    corrupting the stored cache. ``n_hits`` counts messages served from cache
    (including no-event hits, which contribute nothing to ``cached_events``).
    """
    to_extract: list[dict] = []
    cached_events: list[dict] = []
    hits = 0
    for m in messages:
        entry = cache.get(message_key(m))
        if entry is None:
            to_extract.append(m)
            continue
        hits += 1
        for ev in entry.get("events") or []:
            cached_events.append(copy.deepcopy(ev))
    return to_extract, cached_events, hits


def record(cache: dict, to_extract: list[dict], events: list[dict],
           rejects: dict) -> None:
    """Write the outcome of one extraction back into ``cache`` (in place).

    See the module docstring for the safety rule. Only provably-empty messages
    (``llm_no_events``) and confidently-linked events (``_source_ids``) are
    stored; errored and ambiguous messages are left uncached.
    """
    now = time.time()
    no_event = {message_key(m) for m in (rejects.get("llm_no_events") or [])}
    errored = {message_key(m) for m in (rejects.get("llm_errors") or [])}

    # message id -> content key, for turning an event's `_source_ids` (which
    # are message ids) into the key we store under.
    idmap = {m["id"]: message_key(m) for m in to_extract if m.get("id")}
    positive: dict[str, list[dict]] = {}
    for ev in events:
        for sid in ev.get("_source_ids") or []:
            key = idmap.get(sid)
            if key is not None:
                positive.setdefault(key, []).append(copy.deepcopy(ev))

    stored_pos = stored_neg = 0
    for m in to_extract:
        key = message_key(m)
        if key in errored:
            continue  # an outage is not a 'no event' — must be retried
        if key in positive:
            cache[key] = {"events": positive[key], "ts": now}
            stored_pos += 1
        elif key in no_event:
            cache[key] = {"events": [], "ts": now}
            stored_neg += 1
        # else: reached the model but its batch's events did not link here —
        # leave uncached and re-extract next run rather than risk a false empty.

    if stored_pos or stored_neg:
        logger.info("extraction cache: stored %d event-bearing, %d empty message(s)",
                    stored_pos, stored_neg)


def save(cache: dict) -> None:
    """Prune (TTL, then size cap oldest-first) and write the cache atomically."""
    now = time.time()
    for k in [k for k, v in cache.items()
              if now - float((v or {}).get("ts") or 0) > _TTL_SECONDS]:
        del cache[k]
    if len(cache) > _MAX_ENTRIES:
        oldest = sorted(cache.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0))
        for k, _ in oldest[: len(cache) - _MAX_ENTRIES]:
            del cache[k]
    save_json(CACHE_FILE, cache)
