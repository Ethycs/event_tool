"""User-owned keyword lists, and what a keyword can actually mean per platform.

**The honest constraint, measured 2026-08-10.** "Search my keywords on Luma,
Meetup and Instagram" cannot be implemented uniformly, because the platforms do
not offer the same thing:

| Platform | Free-text search | What a keyword becomes here |
|---|---|---|
| Meetup | yes | a real query typed into the site (`search` mode) |
| Eventbrite | yes, via URL path | a real query |
| Luma | **no** — search says *"not signed in"* | a category if one matches
  (`ai`/`tech`/`climate`/`crypto`/`fooddrink`), else a local tag |
| Partiful | **no** — 5 trending events per city is the whole public surface | a local tag |
| Instagram | hashtag search needs a login | a local tag over the configured accounts |

So a keyword has three possible *strengths*, and the system says which one it
used rather than pretending they are equivalent — see :func:`plan_for_source`.
``NATIVE`` narrows what is fetched; ``CATEGORY`` narrows it coarsely;
``LOCAL`` cannot narrow the fetch at all and only marks what came back.

**Local matching tags, it does not drop.** Everything fetched still flows to
the pipeline; matches gain a score bonus via `analysis.score_message`. Dropping
non-matches was considered and rejected: Luma is currently the largest single
source of real events, and the caps already discard ~98% of what is fetched, so
a keyword filter on top would silently narrow the funnel twice.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from event_harvester.utils import data_path

logger = logging.getLogger("event_harvester.keywords")

KEYWORDS_FILE = data_path("keywords.json")

# How strongly a source can honour a keyword. Ordered weakest-last so
# `min()`/sorting reads naturally.
NATIVE = "native"      # typed into the platform's own search
CATEGORY = "category"  # mapped onto the platform's own taxonomy
LOCAL = "local"        # matched against what came back, after the fetch

STRENGTHS = (NATIVE, CATEGORY, LOCAL)

# Modes whose handler types a query into the site itself.
_NATIVE_MODES = frozenset({"search"})
# Luma's public category vocabulary, read off /discover 2026-08-10. Keywords
# are matched against these names, so "AI" reaches `cat-ai` but "queer" does
# not reach anything and falls back to LOCAL.
LUMA_CATEGORIES = {
    "ai": "cat-ai",
    "tech": "cat-tech",
    "climate": "cat-climate",
    "crypto": "cat-crypto",
    "food": "cat-fooddrink",
    "food & drink": "cat-fooddrink",
    "fooddrink": "cat-fooddrink",
}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")


class KeywordError(ValueError):
    """Raised when a keyword list is malformed."""


@dataclass
class KeywordList:
    name: str
    terms: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "terms": self.terms,
                "description": self.description}


def validate_name(name: str) -> str:
    """Lowercase kebab-case, so a name is safe as a config key and a URL part."""
    clean = (name or "").strip().lower()
    if not _NAME_RE.match(clean):
        raise KeywordError(
            f"invalid name {name!r}: use lowercase letters, digits and hyphens "
            "(1-39 chars, starting with a letter or digit)",
        )
    return clean


def normalise_terms(terms) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively, keep order.

    Order is kept because it is the order a `search`-mode source will type
    them, and the operator's first term is usually the one they care about.
    """
    if isinstance(terms, str):
        terms = [terms]
    out: list[str] = []
    seen: set[str] = set()
    for t in terms or []:
        t = " ".join(str(t).split())
        if not t or len(t) > 80:
            continue
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out


def load_keyword_lists(path: Path | None = None) -> list[KeywordList]:
    """Read the store. A missing or unreadable file is an empty list, not an error."""
    p = path or KEYWORDS_FILE
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error("keywords: could not read %s: %s", p, e)
        return []

    entries = raw.get("lists", raw) if isinstance(raw, dict) else raw
    out: list[KeywordList] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            name = validate_name(entry.get("name", ""))
        except KeywordError as e:
            logger.warning("keywords: skipping list — %s", e)
            continue
        out.append(KeywordList(
            name=name,
            terms=normalise_terms(entry.get("terms")),
            description=str(entry.get("description", ""))[:200],
        ))
    return out


def save_keyword_lists(lists: list[KeywordList], path: Path | None = None) -> None:
    """Write atomically — a half-written store would break every harvest."""
    p = Path(path or KEYWORDS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"lists": [kl.to_dict() for kl in lists]}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def terms_for(names, lists: list[KeywordList] | None = None) -> list[str]:
    """Flatten the named lists into one ordered, de-duplicated term list."""
    if isinstance(names, str):
        names = [names]
    available = {kl.name: kl for kl in (lists if lists is not None else load_keyword_lists())}
    picked: list[str] = []
    for n in names or []:
        kl = available.get(str(n).strip().lower())
        if kl is None:
            logger.warning("keywords: no list named %r", n)
            continue
        picked.extend(kl.terms)
    return normalise_terms(picked)


def _term_pattern(term: str) -> re.Pattern:
    """Word-boundary match, so "AI" does not fire on "chain" or "said".

    This is the whole reason matching is not a substring test: the most
    valuable short keywords ("AI", "VC", "ML") are exactly the ones a naive
    `in` check ruins.
    """
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def match_terms(text: str, terms: list[str]) -> list[str]:
    """Which of `terms` appear in `text`, in the order the terms were given."""
    if not text or not terms:
        return []
    return [t for t in terms if _term_pattern(t).search(text)]


def tag_messages(messages: list[dict], terms: list[str]) -> int:
    """Mark messages whose content matches, and return how many did.

    Sets ``_keywords`` to the matching terms. Never drops a message — see the
    module docstring for why.
    """
    if not terms:
        return 0
    hit = 0
    for m in messages:
        found = match_terms(m.get("content", ""), terms)
        if found:
            m["_keywords"] = found
            hit += 1
    return hit


@dataclass
class SearchPlan:
    """What a given source will actually do with a set of keywords."""

    source: str
    mode: str
    strength: str
    terms: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source, "mode": self.mode, "strength": self.strength,
            "terms": self.terms, "categories": self.categories, "note": self.note,
        }


def plan_for_source(src: dict, terms: list[str]) -> SearchPlan:
    """Say, before running anything, how this source will treat these keywords.

    This function exists so the UI can show the *degradation* rather than
    implying every platform searches equally. It is the honest answer to
    "what will my keywords actually do?".
    """
    name = src.get("name", "?")
    mode = src.get("mode", "auto")

    if not terms:
        return SearchPlan(name, mode, LOCAL, note="no keywords selected")

    if mode in _NATIVE_MODES:
        return SearchPlan(name, mode, NATIVE, terms=list(terms),
                          note="typed into the site's own search box, one pass per term")

    if mode == "luma":
        cats, matched = [], []
        for t in terms:
            cat = LUMA_CATEGORIES.get(t.strip().lower())
            if cat and cat not in cats:
                cats.append(cat)
                matched.append(t)
        if cats:
            unmatched = [t for t in terms if t not in matched]
            note = f"Luma has no public free-text search; mapped {matched} to categories"
            if unmatched:
                note += f"; {unmatched} can only be matched locally"
            return SearchPlan(name, mode, CATEGORY, terms=list(terms),
                              categories=cats, note=note)
        return SearchPlan(name, mode, LOCAL, terms=list(terms),
                          note="Luma has no public free-text search and no keyword "
                               "matched a category; tagged after fetching")

    return SearchPlan(name, mode, LOCAL, terms=list(terms),
                      note="this source cannot be queried; matches are tagged after fetching")
