"""Access log: when each web source's state changed, per method, and every failure.

Some things about a source cannot be read; they can only be observed. How long
a passed bot check lasts is one: the clearance cookie says a year, the site
decides, and nothing on this side says which. So every access to a source is
compared with the last one **by the same access method**, and three kinds of
line are appended to ``data/.web_access_log.jsonl``:

* ``change`` — the state differs from last time. The line says what it was,
  since when, and how long it held: at least ``held_min_s`` (it was last *seen*
  in the old state that long after it began) and at most ``held_max_s`` (it had
  changed by now). The truth lies between; the gap is the sampling interval.
* ``fail`` — the state is a failing one and has not changed. One line per
  access, so a source that fails every night leaves a line every night.
* ``click`` — something this program did during an access that is not a
  state: it pressed a bot check's box, or could not and says why
  (:func:`note`). It moves no state; the access's own line follows it.

**Per method, because methods do not share verifications.** A check passed in
one browser, or by one way of driving it, says nothing about another:
``erobay`` cleared by hand in a plain Chrome was challenged again fifteen
minutes later in the harvest's own Chrome on the same profile (2026-11-24).
So the state is kept for each ``(source, method)`` pair, where the method is
the route ``web_fetch`` took: ``http``, ``standard``, ``native``,
``native:open-browser`` or ``service:<window>``, with ``+click`` appended for a
source whose check this program clicks.

``data/.web_access_state.json`` holds each pair's current state, when it began
and when it was last seen, which is what makes a change detectable across runs
and processes.

A state recorded with ``observed=False`` (a source skipped because its browser
was lost) is logged as a failure of the run but does not move the source's
state: nothing was learned about the site.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from event_harvester.utils import data_path, load_json, save_json

logger = logging.getLogger("event_harvester.web_fetch")

LOG_FILE = data_path(".web_access_log.jsonl")
STATE_FILE = data_path(".web_access_state.json")

#: States that are logged on every access, changed or not.
FAIL_STATES = frozenset({"blocked", "deviated", "failed", "hung", "low", "skipped"})

_lock = threading.Lock()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _span(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    hours, rest = divmod(max(seconds, 0), 3600)
    return f"{hours}h{rest // 60:02d}m"


def record(source: str, state: str, detail: str = "", *, method: str, messages: int = 0,
           observed: bool = True, at: datetime | None = None) -> dict | None:
    """Record one access to ``source`` by ``method``. Returns the line written, if any.

    Called by the code that made the access, as it finishes. Nothing is
    entered by hand. ``at`` sets the clock for tests.
    """
    now = at or datetime.now(timezone.utc)
    key = f"{source} via {method}"
    base = {"at": _iso(now), "source": source, "method": method}
    with _lock:
        states = load_json(STATE_FILE, default={}) or {}
        previous = states.get(key)
        line: dict | None = None

        if not observed:
            line = {**base, "event": "fail", "state": state, "detail": detail}
        elif previous is None or previous["state"] != state:
            line = {**base, "event": "change", "state": state,
                    "messages": messages, "detail": detail,
                    "was": None, "was_since": None, "held_min_s": None, "held_max_s": None}
            if previous is not None:
                since = datetime.fromisoformat(previous["since"])
                last = datetime.fromisoformat(previous.get("last", previous["since"]))
                line.update(
                    was=previous["state"], was_since=previous["since"],
                    held_min_s=int((last - since).total_seconds()),
                    held_max_s=int((now - since).total_seconds()),
                )
            states[key] = {"state": state, "since": _iso(now), "last": _iso(now)}
        else:
            previous["last"] = _iso(now)
            if state in FAIL_STATES:
                line = {**base, "event": "fail", "state": state,
                        "since": previous["since"], "detail": detail}

        if line is not None:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        if observed:
            save_json(STATE_FILE, states)

    if line is not None and line["event"] == "change" and line["was"] is not None:
        logger.warning(
            "%s: access state changed %s → %s (was %s for between %s and %s).",
            key, line["was"], state, line["was"],
            _span(line["held_min_s"]), _span(line["held_max_s"]),
        )
    return line


def note(source: str, event: str, detail: str, *, method: str,
         at: datetime | None = None) -> dict:
    """Append a line for something done during an access. Returns the line.

    Not a state: nothing in ``STATE_FILE`` moves. ``event`` names what was
    done (``click``).
    """
    line = {"at": _iso(at or datetime.now(timezone.utc)), "source": source,
            "method": method, "event": event, "detail": detail}
    with _lock:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line
