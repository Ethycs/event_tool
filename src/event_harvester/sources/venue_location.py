"""Work out where a venue is, once, when it is added.

**Why at add-time.** `outputs/regions.py` classifies each event's free-text
location, and its weakest input is a bare venue name — the `raw`-mode sources
are bars scraping their own homepages, and a bar does not write its own city on
its own site. That left a hand-maintained venue table in the report layer,
which a new venue is missing from until somebody notices.

A venue's city is a fact about the *source*, not about each event. So it is
resolved once when the source is added, stored on the config entry, and
inherited by everything that source produces.

**Four signals, strongest first**, all from one browser-free page fetch:

1. **schema.org `PostalAddress`** — a venue with `LocalBusiness` markup states
   its own locality and postcode. Unambiguous when present.
2. **`og:locality` / `og:postal-code`** — the Open Graph profile equivalent.
3. **A ZIP in the page text.** Weaker: a footer can carry a partner's address.
4. **The phone area code.** The quiet workhorse for exactly the sites that
   defeat the other three — a one-page bar site with no markup still prints
   its number, and 415/628 is San Francisco while 510 is the East Bay. Last
   because area codes survive moves and mobiles, so it is the least certain.

Every result carries `evidence`, because a wrong city silently mis-files every
event a venue ever produces, and an operator reviewing `web add` output should
see *why* it decided rather than only what it decided.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("event_harvester.venue_location")

TIMEOUT_S = 15

# Region codes match `outputs/regions.py` so the two never drift.
SF = "sf"
OAKLAND = "oakland"
BROADER = "broader"
UNKNOWN = "unknown"

_SF_ZIPS = range(94102, 94135)
_EASTBAY_ZIPS = (range(94501, 94503), range(94601, 94622), range(94702, 94721))

# 628 is San Francisco's overlay; 341 overlays the East Bay.
_AREA_CODES = {
    "415": SF, "628": SF,
    "510": OAKLAND, "341": OAKLAND,
    "650": BROADER, "408": BROADER, "669": BROADER, "925": BROADER,
    "707": BROADER, "916": BROADER, "831": BROADER,
}

_SF_CITY = ("san francisco",)
_EASTBAY_CITY = ("oakland", "berkeley", "emeryville", "alameda", "piedmont")

_ZIP_RE = re.compile(r"\b(9[0-9]{4})(?:-\d{4})?\b")
# Tolerates +1, dots, spaces, parens — and requires a separator before the
# exchange so a bare 10-digit id cannot be read as a phone number.
_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?(\d{3})\)?[\s.\-]\d{3}[\s.\-]\d{4}\b")


def region_for_zip(code: int) -> str:
    if code in _SF_ZIPS:
        return SF
    if any(code in r for r in _EASTBAY_ZIPS):
        return OAKLAND
    return BROADER


def region_for_city(name: str) -> str:
    low = (name or "").strip().lower()
    if any(c in low for c in _SF_CITY):
        return SF
    if any(c in low for c in _EASTBAY_CITY):
        return OAKLAND
    return BROADER if low else UNKNOWN


def _from_jsonld(html: str) -> tuple[str, str, str] | None:
    """(city, region, evidence) from any schema.org PostalAddress on the page."""
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for addr in _walk_addresses(data):
            city = str(addr.get("addressLocality") or "").strip()
            postal = str(addr.get("postalCode") or "").strip()
            if postal[:5].isdigit():
                return city, region_for_zip(int(postal[:5])), f"schema.org postalCode {postal}"
            if city:
                return city, region_for_city(city), f"schema.org addressLocality {city!r}"
    return None


def _walk_addresses(node, depth: int = 0):
    """Yield every PostalAddress-shaped dict, wherever it sits."""
    if depth > 8:
        return
    if isinstance(node, dict):
        if "addressLocality" in node or "postalCode" in node:
            yield node
        for value in node.values():
            yield from _walk_addresses(value, depth + 1)
    elif isinstance(node, list):
        for value in node[:50]:
            yield from _walk_addresses(value, depth + 1)


def _from_meta(html: str) -> tuple[str, str, str] | None:
    postal = re.search(
        r'<meta[^>]+(?:property|name)=["\'][^"\']*postal[_-]?code["\'][^>]+'
        r'content=["\']([^"\']+)', html, re.I)
    if postal and postal.group(1).strip()[:5].isdigit():
        code = postal.group(1).strip()[:5]
        return "", region_for_zip(int(code)), f"og postal-code {code}"
    locality = re.search(
        r'<meta[^>]+(?:property|name)=["\'][^"\']*locality["\'][^>]+'
        r'content=["\']([^"\']+)', html, re.I)
    if locality:
        city = locality.group(1).strip()
        return city, region_for_city(city), f"og locality {city!r}"
    return None


def _from_text_zip(text: str) -> tuple[str, str, str] | None:
    for raw in _ZIP_RE.findall(text):
        code = int(raw)
        if 94000 <= code <= 96200:      # Northern California only
            return "", region_for_zip(code), f"ZIP {raw} in page text"
    return None


def _from_phone(text: str) -> tuple[str, str, str] | None:
    for area in _PHONE_RE.findall(text):
        region = _AREA_CODES.get(area)
        if region:
            return "", region, f"area code {area}"
    return None


def detect(url: str, html: str | None = None) -> dict:
    """Where is this venue? Returns {city, region, evidence}.

    `html` may be supplied to avoid a second fetch; otherwise one browser-free
    GET is made. Never raises — an undetectable venue is `unknown`, which is a
    normal answer and simply means the operator sets `city` by hand.
    """
    if html is None:
        try:
            from event_harvester.sources.feed_probe import _fetch_page

            html = _fetch_page(url, timeout_s=TIMEOUT_S) or ""
        except Exception as e:  # noqa: BLE001 -- a third-party page
            logger.debug("venue location: fetch failed for %s: %s", url, e)
            html = ""
    if not html:
        return {"city": "", "region": UNKNOWN, "evidence": "page not fetched"}

    text = re.sub(r"<[^>]+>", " ", html)

    for finder, arg in (
        (_from_jsonld, html), (_from_meta, html),
        (_from_text_zip, text), (_from_phone, text),
    ):
        found = finder(arg)
        if found:
            city, region, evidence = found
            return {"city": city, "region": region, "evidence": evidence}

    return {"city": "", "region": UNKNOWN, "evidence": "no address, ZIP or phone found"}
