"""Web command — manage web sources (list/add/test/login)."""

import logging

from event_harvester.display import BOLD, DIM, GREEN, RED, RESET

logger = logging.getLogger("event_harvester")


async def web_cmd(args, cfg) -> int:
    """Dispatch to the appropriate web subcommand."""
    sub = args.web_command
    if sub == "list":
        return _web_list(args, cfg)
    if sub == "add":
        return _web_add(args, cfg)
    if sub == "test":
        return _web_test(args, cfg)
    if sub == "login":
        return _web_login(args, cfg)
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
    """Run diagnostics on a URL without saving."""
    _run_url_diagnostics(args.url)
    return 0


def _web_add(args, cfg) -> int:
    """Run diagnostics and add the URL to data/web_sources.json."""
    detected_mode = _run_url_diagnostics(args.url)
    mode = args.mode or detected_mode
    _add_source_to_config(args.url, mode)
    return 0


def _web_login(args, cfg) -> int:
    """Open a browser to log into web sites and save session state."""
    from event_harvester.sources.web_session import web_login
    web_login()
    return 0


# ── Diagnostics + add helpers (moved from old cli.py) ────────────────


def _run_url_diagnostics(url: str) -> str | None:
    """Open a URL and run all detection modes. Returns the recommended mode string."""
    import concurrent.futures

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


def _add_source_to_config(url: str, detected_mode: str | None = None) -> None:
    """Add a web source to data/web_sources.json using the detected mode."""
    from urllib.parse import urlparse

    from event_harvester.sources.web_fetch import _WEB_SOURCES_FILE, _load_web_sources
    from event_harvester.utils import save_json

    sources = _load_web_sources()

    if any(s["url"] == url for s in sources):
        print(f"\n  {BOLD}Already configured{RESET} — {url} is already in web_sources.json")
        return

    name = urlparse(url).netloc.split(".")[0]
    if name in ("www", "business"):
        name = urlparse(url).netloc.split(".")[-2]

    mode = detected_mode or "calendar"
    source = {"url": url, "name": name, "mode": mode}
    if mode == "feed":
        source["api_pattern"] = r"api|graphql"
        source["scroll_seconds"] = 15
    elif mode == "search":
        source["query"] = "events"
        source["scroll_seconds"] = 10

    sources.append(source)
    save_json(_WEB_SOURCES_FILE, sources)
    print(f"\n  {GREEN}{BOLD}Added{RESET} {name} (mode={mode}) -> {_WEB_SOURCES_FILE}")
