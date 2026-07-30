"""Web command — manage web sources (list/add/test/probe/login)."""

import logging

from event_harvester.observability.tracing import get_tracer
from event_harvester.ui.display import BOLD, DIM, GREEN, RED, RESET, YELLOW

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.cli.commands.web")


async def web_cmd(args, cfg) -> int:
    """Dispatch to the appropriate web subcommand."""
    sub = args.web_command
    with tracer.start_as_current_span(f"cli.web.{sub}") as span:
        span.set_attribute("cli.subcommand", sub)
        if sub == "list":
            return _web_list(args, cfg)
        if sub == "add":
            return _web_add(args, cfg)
        if sub == "test":
            return _web_test(args, cfg)
        if sub == "probe":
            return _web_probe(args, cfg)
        if sub == "login":
            return _web_login(args, cfg)
        span.set_attribute("error.message", f"unknown subcommand {sub}")
        logger.error("Unknown web subcommand: %s", sub)
        return 1


# ── Subcommand handlers ──────────────────────────────────────────────


def _web_list(args, cfg) -> int:
    """Print configured web sources from data/web_sources.json."""
    from event_harvester.sources.web_fetch import _load_web_sources

    sources = _load_web_sources()
    if not sources:
        print(f"{DIM}No web sources configured.{RESET}")
        print(
            "Add one with: "
            f"{BOLD}event-harvester web add URL{RESET}",
        )
        return 0

    print(f"\n{BOLD}Configured web sources ({len(sources)}){RESET}\n")
    for s in sources:
        name = s.get("name", "?")
        mode = s.get("mode", "?")
        url = s.get("url", "?")
        extras = []
        if s.get("native_chrome"):
            extras.append("native_chrome")
        if s.get("api_pattern"):
            extras.append(f"api={s['api_pattern']}")
        if s.get("query"):
            extras.append(f"query={s['query']!r}")
        if s.get("scroll_seconds"):
            extras.append(f"scroll={s['scroll_seconds']}s")
        extras_str = f"  {DIM}[{', '.join(extras)}]{RESET}" if extras else ""

        print(f"  {BOLD}{name}{RESET}  {DIM}({mode}){RESET}{extras_str}")
        print(f"    {url}\n")
    return 0


def _web_test(args, cfg) -> int:
    """Run diagnostics on a URL without saving.

    Tries the browser-free feed probe first: if the site publishes iCalendar,
    that is the answer and spinning up Playwright to confirm it would only
    cost a few seconds to reach a worse recommendation.
    """
    if not getattr(args, "no_probe", False):
        feed = _probe_and_report(args.url)
        if feed:
            print(f"\n  {GREEN}Recommended mode: ics{RESET} — use {BOLD}{feed.url}{RESET}\n")
            print(f"  {DIM}Re-run with --no-probe to force browser diagnostics.{RESET}\n")
            return 0

    _run_url_diagnostics(args.url)
    return 0


def _web_add(args, cfg) -> int:
    """Run diagnostics and add the URL to data/web_sources.json.

    A discovered feed replaces the URL being added — adding
    ``https://example.com`` when the events live at
    ``https://example.com/events/?ical=1`` should store the feed, not the
    homepage that merely links to it.
    """
    if not args.mode and not getattr(args, "no_probe", False):
        feed = _probe_and_report(args.url)
        if feed:
            _add_source_to_config(feed.url, "ics", source_page=args.url)
            return 0

    detected_mode = _run_url_diagnostics(args.url)
    mode = args.mode or detected_mode
    _add_source_to_config(args.url, mode)
    return 0


def _probe_and_report(url: str, *, timeout_s: int = 10):
    """Run the feed probe and print what it found. Returns the best candidate or None."""
    from event_harvester.sources.feed_probe import probe_feeds

    print(f"  {DIM}Probing for a calendar feed (no browser)...{RESET}")
    found = probe_feeds(url, timeout_s=timeout_s)
    if not found:
        print(f"  {DIM}No iCalendar feed found — falling back to the browser.{RESET}\n")
        return None

    best = found[0]
    plat = f" via {best.platform}" if best.platform else ""
    print(
        f"  {GREEN}{BOLD}Feed found{RESET} ({best.origin}{plat}): {best.url}\n"
        f"    {best.events} event(s), {best.future_events} upcoming"
    )
    for extra in found[1:4]:
        print(f"    {DIM}also: {extra.url} ({extra.future_events} upcoming){RESET}")
    return best


def _web_probe(args, cfg) -> int:
    """Probe URLs — or every configured non-ICS source — for calendar feeds.

    This is the bulk path. ``web add`` handles one site at a time; this
    answers "which of the venues I already have could stop using a browser?"
    and, with ``--apply``, rewrites them in place.
    """
    from event_harvester.sources.feed_probe import probe_feeds

    urls: list[str]
    sources_by_url: dict[str, dict] = {}

    if getattr(args, "urls", None):
        urls = list(args.urls)
    else:
        from event_harvester.sources.web_fetch import _load_web_sources

        sources = _load_web_sources()
        sources_by_url = {s["url"]: s for s in sources}
        urls = [s["url"] for s in sources if s.get("mode") != "ics"]
        if not urls:
            print(f"{DIM}No non-ICS sources configured to probe.{RESET}")
            return 0
        print(f"\n{BOLD}Probing {len(urls)} configured source(s) for calendar feeds{RESET}")
        print(f"{DIM}Sources already in ics mode are skipped.{RESET}\n")

    hits: list[tuple[str, object]] = []
    with tracer.start_as_current_span("cli.web.probe.batch") as span:
        span.set_attribute("probe.url_count", len(urls))
        for url in urls:
            src = sources_by_url.get(url, {})
            label = src.get("name") or url
            mode = src.get("mode")
            prefix = f"  {BOLD}{label:18}{RESET}" + (f" {DIM}({mode}){RESET}" if mode else "")
            found = probe_feeds(url, timeout_s=getattr(args, "timeout", 10) or 10)
            if found:
                best = found[0]
                plat = f", {best.platform}" if best.platform else ""
                print(
                    f"{prefix}  {GREEN}FEED{RESET}  {best.url}\n"
                    f"    {DIM}{best.future_events} upcoming of {best.events}"
                    f" ({best.origin}{plat}){RESET}"
                )
                hits.append((url, best))
            else:
                print(f"{prefix}  {DIM}none{RESET}")
        span.set_attribute("probe.hit_count", len(hits))

    print(f"\n{BOLD}{len(hits)}/{len(urls)}{RESET} source(s) have a usable feed.")

    if not hits:
        return 0
    if not getattr(args, "apply", False):
        print(f"{DIM}Re-run with --apply to switch these to ics mode.{RESET}\n")
        return 0
    if not sources_by_url:
        print(f"{YELLOW}--apply only works when probing configured sources "
              f"(omit the URL arguments).{RESET}\n")
        return 0

    return _apply_feed_hits(hits, sources_by_url)


def _apply_feed_hits(hits, sources_by_url: dict[str, dict]) -> int:
    """Rewrite probed sources to ics mode, preserving name/category/cooldown."""
    from event_harvester.sources.web_fetch import _WEB_SOURCES_FILE, _load_web_sources
    from event_harvester.utils import save_json

    sources = _load_web_sources()
    by_url = {s["url"]: s for s in sources}
    changed = 0
    skipped = 0

    with tracer.start_as_current_span("cli.web.probe.apply") as span:
        for old_url, best in hits:
            src = by_url.get(old_url)
            if src is None:
                continue
            # A feed with nothing upcoming is not evidence the feed works —
            # it is equally consistent with a stale export nobody updates.
            # Switching a browser source to it would silently zero the venue.
            if best.future_events < 1:
                print(
                    f"  {YELLOW}skipped{RESET} {src.get('name', '?')} — feed has 0 upcoming "
                    f"events ({best.url})"
                )
                skipped += 1
                continue
            # Browser-only knobs are meaningless for a direct feed fetch; drop
            # them rather than leave dead config behind.
            for key in ("scroll_seconds", "api_pattern", "native_chrome", "api_only", "query"):
                src.pop(key, None)
            src["url"] = best.url
            src["mode"] = "ics"
            if old_url != best.url:
                src["source_page"] = old_url
            changed += 1
            print(f"  {GREEN}switched{RESET} {src.get('name', '?')} -> ics  {best.url}")
        span.set_attribute("apply.changed_count", changed)
        span.set_attribute("apply.skipped_count", skipped)

    if changed:
        save_json(_WEB_SOURCES_FILE, sources)
        print(f"\n{GREEN}{BOLD}Updated{RESET} {changed} source(s) in {_WEB_SOURCES_FILE}")
    if skipped:
        print(f"{DIM}{skipped} source(s) left unchanged — see above.{RESET}")
    print()
    return 0


def _web_login(args, cfg) -> int:
    """Open a browser to log into web sites and save session state."""
    from event_harvester.sources.web_session import web_login
    web_login()
    return 0


# ── Diagnostics + add helpers (moved from old cli.py) ────────────────


def _is_ics_url(url: str) -> bool:
    """True for iCalendar feeds, which are not browsable pages.

    Mirrors the shortcut in ``web_utils._detect_mode``. Kept in sync by hand;
    both exist because detection happens on two different paths -- the harvest
    path and this diagnostic path.
    """
    return url.startswith(("webcal://", "webcals://")) or url.rstrip("/").endswith(".ics")


def _diagnose_ics(url: str) -> str | None:
    """Diagnostics for an iCalendar feed -- fetch and parse, no browser.

    ``web test``/``web add`` used to hand every URL straight to Playwright,
    which cannot navigate ``webcal://`` at all::

        Page.goto: net::ERR_ABORTED at webcal://cdn.dnalounge.com/...

    The harvest path already branched to ICS before opening a page; this
    path did not, so adding an ICS source failed with a browser error that
    said nothing about the real reason.
    """
    from event_harvester.sources.web_modes.ics import _do_ics_inner

    print(f"  Testing: {url}\n")
    print(f"  {DIM}Detected iCalendar feed — fetching directly, no browser.{RESET}")

    msgs = _do_ics_inner({"url": url, "name": "ics-test"})
    if not msgs:
        # _do_ics_inner logs the underlying reason and returns [] rather than
        # raising, so there is nothing to catch here -- report and bail.
        print(f"  {RED}No events parsed{RESET} — see the log line above for why.\n")
        return None

    print(f"  Parsed {len(msgs)} event(s)\n")
    for m in msgs[:5]:
        first_line = (m.get("content") or "").strip().splitlines()[:1]
        print(f"    {DIM}{m.get('timestamp', '?')[:16]}{RESET}  "
              f"{(first_line[0] if first_line else '')[:58]}")
    if len(msgs) > 5:
        print(f"    {DIM}... and {len(msgs) - 5} more{RESET}")

    print(f"\n  {GREEN}Recommended mode: ics{RESET}\n")
    return "ics"


def _run_url_diagnostics(url: str) -> str | None:
    """Open a URL and run all detection modes. Returns the recommended mode string."""
    import concurrent.futures

    # iCalendar feeds are not pages -- handle before touching a browser.
    if _is_ics_url(url):
        return _diagnose_ics(url)

    from event_harvester.sources.uc_browser import UCBrowser
    from event_harvester.sources.web_fetch import (
        _extract_event_links,
        _extract_text,
        _wait_cloudflare,
    )

    detected_mode = [None]  # mutable container for thread result

    def _do_diagnostics():
        with UCBrowser() as uc:
            print(f"  Testing: {url}\n")
            page = uc.open(url, wait_ms=3000)
            html = _wait_cloudflare(page, headless=False)

            if "just a moment" in html.lower():
                print(f"  {RED}BLOCKED{RESET} — Cloudflare challenge not resolved.")
                print("  Recommendation: use native_chrome=True\n")
                page.close()
                return

            text = _extract_text(html)
            print(f"  Raw text: {len(text)} chars")

            event_links = _extract_event_links(page)
            date_links = [lnk for lnk in event_links if lnk.get("date_hint")]
            inline_links = [lnk for lnk in event_links if lnk.get("inline")]
            fetch_links = [lnk for lnk in event_links if not lnk.get("inline")]

            print(
                f"  Event links: {len(event_links)} total "
                f"({len(date_links)} with dates, "
                f"{len(inline_links)} inline, "
                f"{len(fetch_links)} fetchable)"
            )

            patterns = uc.detect_all(page)
            if patterns:
                for ptype, hits in patterns.items():
                    if hits:
                        best = hits[0].get("confidence", 0)
                        print(f"  UC {ptype}: {len(hits)} hit(s), best={best:.2f}")

            feed_items = uc.get_feed_items(page)
            feed_items = [t for t in feed_items if len(t) > 30]
            print(f"  Feed items: {len(feed_items)}")

            search_hits = uc.detect(page, "search")
            has_search = len(search_hits) > 0

            print(f"\n  {BOLD}Recommendation:{RESET}")
            if len(event_links) >= 3:
                mode = "calendar"
                print(f"  {GREEN}mode: calendar{RESET} — {len(event_links)} event links found")
                if inline_links:
                    print(f"    {len(inline_links)} inline (no fetch needed)")
                if fetch_links:
                    print(f"    {len(fetch_links)} detail pages to fetch")
            elif has_search:
                mode = "search"
                ph = search_hits[0].get("placeholder", "")
                print(
                    f"  {GREEN}mode: search{RESET} — search bar detected"
                    + (f' (placeholder: "{ph}")' if ph else "")
                )
            elif len(feed_items) >= 3:
                mode = "feed"
                print(f"  {GREEN}mode: feed{RESET} — {len(feed_items)} feed items")
            else:
                mode = "raw"
                print("  mode: raw — no structured content detected")

            detected_mode[0] = mode

            print(f"\n  {BOLD}Sample events:{RESET}")
            if event_links:
                for lnk in event_links[:5]:
                    dh = lnk.get("date_hint", "")
                    tag = " [inline]" if lnk.get("inline") else ""
                    print(f"    {dh:12s} {lnk['text'][:70]}{tag}")
            elif feed_items:
                for item in feed_items[:5]:
                    print(f"    {item[:80]}")
            else:
                print(f"    {text[:200]}")

            print(f"\n  {BOLD}Suggested config:{RESET}")
            from urllib.parse import urlparse
            name = urlparse(url).netloc.split(".")[0]
            if name in ("www", "business"):
                name = urlparse(url).netloc.split(".")[-2]
            cfg_str = f'    {{"url": "{url}", "name": "{name}", "mode": "{mode}"'
            if mode == "feed":
                cfg_str += ', "api_pattern": r"api|graphql", "scroll_seconds": 15'
            if mode == "search":
                cfg_str += ', "query": "events", "scroll_seconds": 10'
            cfg_str += "}"
            print(f"  {cfg_str}")

            page.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_do_diagnostics).result()

    return detected_mode[0]


def _add_source_to_config(
    url: str, detected_mode: str | None = None, *, source_page: str | None = None
) -> None:
    """Add a web source to data/web_sources.json using the detected mode.

    ``source_page`` is the human-facing page a feed URL was discovered from.
    It is stored so the entry still says which venue it belongs to when the
    feed URL is an opaque CDN path (``cdn.dnalounge.com/calendar/...``).
    """
    from urllib.parse import urlparse

    from event_harvester.sources.web_fetch import _WEB_SOURCES_FILE, _load_web_sources
    from event_harvester.utils import save_json

    sources = _load_web_sources()

    if any(s["url"] == url for s in sources):
        print(f"\n  {BOLD}Already configured{RESET} — {url} is already in web_sources.json")
        return

    # Name from the page the user actually typed, not the feed host: a feed on
    # tockify.com or calendar.google.com would otherwise name every venue
    # after the platform.
    name_source = source_page or url
    name = urlparse(name_source).netloc.split(".")[0]
    if name in ("www", "business", "cdn", "calendar", "tockify"):
        parts = urlparse(name_source).netloc.split(".")
        name = parts[-2] if len(parts) >= 2 else name

    mode = detected_mode or "calendar"
    source = {"url": url, "name": name, "mode": mode}
    if source_page and source_page != url:
        source["source_page"] = source_page
    if mode == "feed":
        source["api_pattern"] = r"api|graphql"
        source["scroll_seconds"] = 15
    elif mode == "search":
        source["query"] = "events"
        source["scroll_seconds"] = 10

    sources.append(source)
    save_json(_WEB_SOURCES_FILE, sources)
    print(f"\n  {GREEN}{BOLD}Added{RESET} {name} (mode={mode}) -> {_WEB_SOURCES_FILE}")
