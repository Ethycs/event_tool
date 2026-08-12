"""Classify an event's free-text location into a region for the page filter.

**Why this is not just a substring match on "San Francisco".** Measured across
27 located events on 2026-08-12, the same city arrives four different ways:

    'San Francisco, CA'                              a city name
    '484 5th Street, San Francisco, CA 94107'        a full postal address
    'Mother Bar, SF'                                 an abbreviation
    'El Rio'                                         a bare venue, no city at all

The last one is the interesting case and it is not rare: the `raw`-mode venue
sources are SF bars scraping their own homepages, and a bar does not write its
own city on its own site. A city-name matcher alone files those under "no idea",
which is exactly where the local queer nightlife ends up — the events most
likely to be wanted and least likely to be labelled.

So three signals, strongest first: **ZIP code**, then **city name**, then a
**venue table** for the places that publish neither. The venue table is the
weak, maintained one; it is small on purpose and only holds venues actually
seen without a city.

`broader` means "somewhere identifiable that is neither SF nor the near East
Bay" — Mountain View, San Jose, Sacramento. `unknown` means the string carried
no usable signal, and is kept as its own bucket rather than folded into
`broader`, because "we could not tell" and "definitely elsewhere" are different
answers and only one of them is a reason to improve this file.
"""

from __future__ import annotations

import re

SF = "sf"
OAKLAND = "oakland"
BROADER = "broader"
UNKNOWN = "unknown"

# Order is the display order, and also the order the page's chips appear in.
REGIONS = (SF, OAKLAND, BROADER, UNKNOWN)

LABELS = {
    SF: "San Francisco",
    OAKLAND: "Oakland",
    BROADER: "Broader",
    UNKNOWN: "Unplaced",
}

# What the chips mean, shown on hover. `oakland` covers the near East Bay
# rather than the city limits: an Emeryville warehouse party belongs with
# Oakland for anyone deciding where to go tonight, and a fourth chip for three
# events would be worse than a slightly wide one. Said out loud here and in the
# UI rather than left for someone to discover.
HINTS = {
    SF: "San Francisco proper",
    OAKLAND: "Oakland and the near East Bay — Berkeley, Emeryville, Alameda",
    BROADER: "Elsewhere — the Peninsula, South Bay, and further afield",
    UNKNOWN: "No location we could place",
}

# ZIP is the strongest signal: unambiguous, and present on most full addresses.
# 94102-94134 is San Francisco; 94601-94621 Oakland; 94702-94720 Berkeley;
# 94608 Emeryville and 94501/94502 Alameda fall inside the ranges below.
_ZIP_RE = re.compile(r"\b(9[0-9]{4})\b")
_SF_ZIPS = range(94102, 94135)
_EASTBAY_ZIPS = (range(94501, 94503), range(94601, 94622), range(94702, 94721))

_SF_NAMES = (
    "san francisco", "s.f.", "sf,", ", sf", " sf ", "sfo",
)
_EASTBAY_NAMES = ("oakland", "berkeley", "emeryville", "alameda,", "piedmont")

# Venues that publish no city because they do not need to — their own regulars
# know where they are. Only entries observed in real event data belong here.
# Places that are identifiably somewhere, just not here. Without these a
# string like "DoubleTree San Jose, TBD" falls through to `unknown`, which
# reads as "we could not tell" when in fact we could tell perfectly well.
_BROADER_NAMES = (
    "san jose", "mountain view", "palo alto", "santa clara", "sunnyvale",
    "cupertino", "menlo park", "redwood city", "san mateo", "stanford",
    "fremont", "milpitas", "hayward", "san leandro", "walnut creek",
    "sacramento", "san rafael", "sausalito", "marin", "napa", "santa cruz",
    "los angeles", "new york", "seattle", "london", "singapore", "las vegas",
)

_SF_VENUES = (
    "el rio", "the stud", "qbar", "q bar", "mother bar", "oasis", "jolene",
    "the cafe", "wild side west", "powerhouse", "the eagle", "sf eagle",
    "beaux", "midnight sun", "moby dick", "440 castro", "the mix",
    "hi tops", "badlands", "the edge", "lone star", "the bank",
    "club six", "mission bowling", "flywheel coffee", "great northern",
    "roxie theater", "dg717", "palace of fine arts", "hotel kabuki",
    "bottom of the hill", "rickshaw stop", "the independent",
)
_EASTBAY_VENUES = ("white horse", "port bar", "the new parish", "starline social")

# A location that is a videoconference is not a place. Kept out of `broader`
# so "somewhere else in the world" does not fill up with Zoom links.
_ONLINE = ("online", "zoom", "virtual", "livestream", "youtube", "google meet")


def _zip_region(text: str) -> str | None:
    for raw in _ZIP_RE.findall(text):
        code = int(raw)
        if code in _SF_ZIPS:
            return SF
        if any(code in r for r in _EASTBAY_ZIPS):
            return OAKLAND
        # A ZIP we recognise as a ZIP but not as local still tells us it is a
        # real, identifiable place — which is what `broader` means.
        return BROADER
    return None


def classify(location: str, source: str = "") -> str:
    """Which region bucket a location belongs in.

    ``source`` is the event's provenance string, consulted only as a last
    resort: a venue's own Instagram is strong evidence about where the venue
    is, and several SF bars post events with no location at all.
    """
    text = f" {(location or '').strip().lower()} "
    if not text.strip():
        return _from_source(source)

    by_zip = _zip_region(text)
    if by_zip:
        return by_zip

    if any(name in text for name in _EASTBAY_NAMES):
        return OAKLAND
    if any(name in text for name in _SF_NAMES):
        return SF
    # Named elsewhere-places are checked before the venue tables: "DoubleTree
    # San Jose" must not match an SF venue substring and be filed as local.
    if any(name in text for name in _BROADER_NAMES):
        return BROADER
    if any(v in text for v in _EASTBAY_VENUES):
        return OAKLAND
    if any(v in text for v in _SF_VENUES):
        return SF
    if any(word in text for word in _ONLINE):
        return UNKNOWN

    # A comma-separated tail that looks like a place ("Mountain View, CA") is
    # somewhere real; a bare venue name we do not know is not placeable.
    if re.search(r",\s*(ca|california)\b", text) or re.search(r",\s*[a-z]{2}\b", text):
        return BROADER
    return _from_source(source)


def _from_source(source: str) -> str:
    s = (source or "").lower()
    if any(v in s for v in _EASTBAY_VENUES):
        return OAKLAND
    if any(v in s for v in _SF_VENUES):
        return SF
    return UNKNOWN
