"""The nav bar the served pages share, and the mount-point problem it created.

**Why this is opt-in.** The event list is a self-contained file that is meant to
be forwarded — that is stated in `05 - Outputs.md` and pinned by
`test_no_external_resources`. A forwarded copy has no origin, so a link to
`/admin` in it is a link to nothing: it would 404 against whatever site the
recipient happens to have open, or against `file:///admin`. So the nav is
rendered only by the caller that *serves* the page (`servers/site.py`), and a
report written by `--report` or `--build-only` carries no cross-page links.

This existed because `/admin` was built and then could not be found. It was
mounted, tested and working for a day with nothing anywhere pointing at it,
which is functionally the same as not having shipped it. A page with no
inbound link is not a feature.

## Sub-path mounts — 2026-08-12

The site is reached at **`central-dev.zt/harvester`** through a reverse proxy,
as well as directly on `:8800`. That breaks every root-relative URL: `/calendar`
resolves against the proxy's root, not the mount, so it lands on
`central-dev.zt/calendar` — a different application, or a 404.

The fix is that **no served page emits a root-relative URL at all**. Links and
`fetch()` targets are relative (`calendar`, `admin`, `admin/api/state`), which
resolve correctly under any mount depth, and correctly on `:8800` too. There is
no configured base URL and there must not be: the same bytes are reached as
`localhost`, as a LAN address, as a ZeroTier address and through the proxy, and
hard-coding any one of them breaks the other three.

Relative URLs have one failure case, and it is exactly the URL that was
reported: **`/harvester` with no trailing slash**. The browser's base for that
path is `/`, so `calendar` resolves to `/calendar` again. `base_script` closes
it by computing the mount root from `location.pathname` — each page knows its
own suffix, strips it, and inserts a `<base>` before the body is parsed.

That leaves the static hrefs correct *without* JavaScript whenever the path has
a trailing slash, and correct *with* it in every case — degradation rather than
dependence.
"""

from __future__ import annotations

from html import escape

# (page id, relative href, label, one-line description shown on hover).
# Order is the tab order. The hrefs are relative *by design* — see the module
# docstring. `.` rather than `` for the index, because an empty href re-loads
# the current URL rather than pointing at the mount root.
PAGES: tuple[tuple[str, str, str, str], ...] = (
    ("index", ".", "Events", "Everything found, newest harvest"),
    ("calendar", "calendar", "Calendar", "The same events on a month grid"),
    ("admin", "admin", "Admin", "Caps, keyword lists and profiles"),
)

# What each page must strip off `location.pathname` to find the mount root.
# The index has no name of its own when served, so it strips only the
# `index.html` a file-served copy would carry.
_SUFFIX = {
    "index": r"(?:index\.html)?",
    "calendar": r"(?:calendar(?:\.html)?)?",
    "admin": r"(?:admin)?",
}


def nav_html(active: str = "") -> str:
    """The nav bar, with ``active`` (a page id from `PAGES`) marked current.

    Returns an empty string for no page at all, so a caller can pass the flag
    straight through without branching around it.
    """
    if not active:
        return ""
    current = ' aria-current="page"'
    links = "\n    ".join(
        f'<a href="{href}" title="{escape(hint, quote=True)}"'
        f'{current if page == active else ""}>{escape(label)}</a>'
        for page, href, label, hint in PAGES
    )
    return f'<nav class="sitenav" aria-label="Pages">\n    {links}\n  </nav>'


def base_script(active: str = "") -> str:
    """An inline `<head>` script that pins the document base to the mount root.

    Belongs first in `<head>`, before anything with a relative URL. Returns an
    empty string when the page is not being served, since a forwarded file has
    no mount to find.

    It runs while `<head>` is still parsing, so the `<base>` is in place before
    any element that would use it. Relative `fetch()` calls resolve against it
    too, which is what keeps `/admin`'s API reachable under the proxy.
    """
    if active not in _SUFFIX:
        return ""
    return f"""<script>
// Find the mount root by removing this page's own name from the path, so the
// relative links below work at `/`, at `/harvester/`, and — the case that
// actually broke — at `/harvester` with no trailing slash, where the browser
// would otherwise take `/` as the base.
(function () {{
  try {{
    var p = location.pathname.replace(/{_SUFFIX[active]}$/, '');
    if (p.charAt(p.length - 1) !== '/') p += '/';
    var b = document.createElement('base');
    b.href = p;
    document.head.appendChild(b);
  }} catch (e) {{ /* the static hrefs are already relative; they still work */ }}
}})();
</script>"""


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
