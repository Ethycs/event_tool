"""The nav bar the served pages share.

**Why this is opt-in.** The event list is a self-contained file that is meant to
be forwarded — that is stated in `05 - Outputs.md` and pinned by
`test_no_external_resources`. A forwarded copy has no origin, so a link to
`/admin` in it is a link to nothing: it would 404 against whatever site the
recipient happens to have open, or against `file:///admin`. So the nav is
rendered only by the caller that *serves* the page (`servers/site.py`), and a
report written by `--report` or `--build-only` carries no cross-page links.

The links are root-relative rather than absolute for the same reason the site
has no configured base URL: it is reached by several names — `localhost`, a
LAN address, a ZeroTier address — and hard-coding any one of them would break
the other two.

This existed because `/admin` was built and then could not be found. It was
mounted, tested and working for a day with nothing anywhere pointing at it,
which is functionally the same as not having shipped it. A page with no
inbound link is not a feature.
"""

from __future__ import annotations

from html import escape

# (path, label, one-line description shown on hover). Order is the tab order.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("/", "Events", "Everything found, newest harvest"),
    ("/calendar", "Calendar", "The same events on a month grid"),
    ("/admin", "Admin", "Caps, keyword lists and profiles"),
)


def nav_html(active: str = "") -> str:
    """The nav bar, with ``active`` (a path from `PAGES`) marked current.

    Returns an empty string for no page at all, so a caller can pass the flag
    straight through without branching around it.
    """
    if not active:
        return ""
    current = ' aria-current="page"'
    links = "\n    ".join(
        f'<a href="{path}" title="{escape(hint, quote=True)}"'
        f'{current if path == active else ""}>{escape(label)}</a>'
        for path, label, hint in PAGES
    )
    return f'<nav class="sitenav" aria-label="Pages">\n    {links}\n  </nav>'


# Kept with the markup it styles so a page cannot pick up one without the
# other. Colours come from the host page's custom properties; both pages define
# `--fg`, `--muted`, `--line` and `--accent`.
NAV_STYLE = """
.sitenav { display: flex; gap: 2px; margin: 0 0 10px; flex-wrap: wrap; }
.sitenav a {
  color: var(--muted); text-decoration: none; font-weight: 600; font-size: 14px;
  padding: 6px 12px; border-radius: 8px; border: 1px solid transparent;
}
.sitenav a:hover { color: var(--fg); background: var(--chip); }
.sitenav a[aria-current="page"] {
  color: var(--fg); background: var(--chip); border-color: var(--line);
}
"""
