"""Web command — manage web sources (list/add/test/probe/instagram/replay/login)."""

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
        if sub == "instagram":
            return _web_instagram(args, cfg)
        if sub == "replay":
            return _web_replay(args, cfg)
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
        if s.get("accounts"):
            extras.append(f"{len(s['accounts'])} accounts")
        extras_str = f"  {DIM}[{', '.join(extras)}]{RESET}" if extras else ""

        print(f"  {BOLD}{name}{RESET}  {DIM}({mode}){RESET}{extras_str}")
        print(f"    {url}\n")
    return 0


def _web_test(args, cfg) -> int:
    """Diagnose a URL without saving. Logs the attempt all the same."""
    return _diagnose_and_log(args.url, add=False,
                             no_probe=getattr(args, "no_probe", False))


def _web_add(args, cfg) -> int:
    """Diagnose a URL and add it to data/web_sources.json.

    A discovered feed replaces the URL being added — adding
    ``https://example.com`` when the events live at
    ``https://example.com/events/?ical=1`` should store the feed, not the
    homepage that merely links to it.
    """
    return _diagnose_and_log(args.url, add=True, force=getattr(args, "force", False),
                             mode_override=args.mode,
                             no_probe=getattr(args, "no_probe", False))


#: Walls a real browser cannot clear, so `native_chrome` is not the fix and the
#: source should not be added. A Cloudflare challenge is deliberately NOT here:
#: native_chrome clears it (that is how erobay works), so a CF site stays
#: addable, with native_chrome set.
_TERMINAL_BLOCK_PREFIXES = ("forbidden-", "rate-limited-", "server-error-", "fetch-failed")
_TERMINAL_BLOCKS = frozenset({"datadome", "perimeterx", "px-captcha",
                              "incapsula", "akamai", "imperva"})


def _is_terminal_block(reason: str | None) -> bool:
    if not reason:
        return False
    return reason in _TERMINAL_BLOCKS or reason.startswith(_TERMINAL_BLOCK_PREFIXES)


def _diagnose_and_log(
    url: str, *, add: bool, force: bool = False,
    mode_override: str | None = None, no_probe: bool = False,
) -> int:
    """Auto-diagnose a candidate URL, record the attempt, then (for ``add``)
    save it — unless a terminal wall or an empty result argues against it.

    The flow the blueprint's steps 1–6 used to be done by hand: probe for an
    edge wall (names DataDome vs a clearable Cloudflare challenge), then the
    ICS feed probe, then the browser pass. Every run leaves a line in
    ``data/.source_attempts.json`` before it exits, so "why is this source
    empty" is answered once, not re-diagnosed each time. A terminal wall or a
    `raw`/nothing-found result is logged and **not** added unless ``--force``.
    """
    from event_harvester.sources.onboard import probe_block, record_attempt

    name = _suggest_name(url)
    print(f"  {BOLD}Diagnosing{RESET} {url}")
    rec: dict = {"url": url, "name": name, "action": "add" if add else "test"}

    # 1. Edge-block probe — cheap, browser-free, and it names the wall.
    block = probe_block(url)
    rec["status"] = block["status"]
    rec["server"] = block["server"]
    reason = block["reason"]
    mode = None
    links = feed = None
    feed_url = None

    if block["blocked"]:
        print(f"\n  {RED}BLOCKED{RESET} — {reason} "
              f"(HTTP {block['status']}, server {block['server'] or '?'})")
        if reason == "datadome":
            print(f"  {DIM}DataDome blocks automation, native_chrome included — "
                  f"the site's own API is the only way in.{RESET}")
        elif reason == "cloudflare-challenge":
            print(f"  {DIM}A Cloudflare challenge — clearable with native_chrome; "
                  f"adding it that way.{RESET}")
            mode = mode_override or "calendar"
    else:
        # 2. Not walled — the ICS feed probe, then the browser pass.
        if not mode_override and not no_probe:
            found = _probe_and_report(url)
            if found:
                mode, feed_url, links = "ics", found.url, found.future_events
        if mode is None:
            diag = _run_url_diagnostics(url)
            mode = mode_override or diag["mode"]
            links, feed = diag["event_links"], diag["feed_items"]
        if mode == "raw" and not links and not feed:
            reason = "empty"
            print(f"\n  {YELLOW}Nothing structured found{RESET} — a `raw` page with "
                  f"no event links or feed items.")

    needs_native = reason == "cloudflare-challenge"
    addable = reason in (None, "cloudflare-challenge") or force

    saved = False
    if add and addable:
        if mode == "ics" and feed_url:
            _add_source_to_config(feed_url, "ics", source_page=url)
        else:
            _add_source_to_config(url, mode or mode_override or ("calendar" if needs_native else "raw"),
                                  native_chrome=needs_native)
        saved = True
    elif add:
        print(f"\n  {YELLOW}Not added{RESET} — {reason}. Re-run with "
              f"{BOLD}--force{RESET} to add it anyway.")

    rec.update(blocked=block["blocked"], reason=reason, mode=mode,
               event_links=links, feed_items=feed, saved=saved)
    if feed_url:
        rec["feed_url"] = feed_url
    record_attempt(rec)
    print(f"  {DIM}Logged → data/.source_attempts.json "
          f"({'saved' if saved else 'not saved'}).{RESET}\n")
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


def _web_instagram(args, cfg) -> int:
    """Read venues' Instagram handles off their own websites.

    The sibling of ``web probe``, for the segment that probe cannot reach.
    Feed discovery converts a venue that runs a calendar platform; measured
    on this source list, that was 1 of 27. The other 26 are mostly bars whose
    real calendar is an Instagram account — and the account is linked from
    their own footer, so it can be read rather than guessed.

    Guessing is the thing to avoid: Oasis is ``@theoasissf``, not
    ``@sfoasis``, and a wrong handle produces a source that silently harvests
    someone else's posts. Hence ``--apply`` adopts only *confident* matches
    (the handle shares ≥5 characters with the venue's name or domain) and
    prints the rest for a human.
    """
    from event_harvester.sources.instagram import discover_handles
    from event_harvester.sources.web_fetch import _load_web_sources

    sources = _load_web_sources()
    targets: list[tuple[str, str]] = []  # (url, venue name)

    category = getattr(args, "category", None)
    if getattr(args, "urls", None):
        targets = [(u, "") for u in args.urls]
    else:
        for src in sources:
            url = src.get("url", "")
            # An ICS feed URL is a file, not a venue page — scan the page it
            # was discovered from when we recorded one.
            page_url = src.get("source_page") or url
            if src.get("mode") == "instagram" or not page_url.startswith("http"):
                continue
            if category and src.get("category") != category:
                continue
            targets.append((page_url, src.get("name", "")))
        if not targets:
            print(f"{DIM}No configured sources with a website to scan.{RESET}")
            return 0
        scope = f" in category {category!r}" if category else ""
        print(f"\n{BOLD}Scanning {len(targets)} configured source(s){scope} "
              f"for Instagram accounts{RESET}")
        print(f"{DIM}One request each, no browser.{RESET}\n")

    found: list[tuple[str, object]] = []
    with tracer.start_as_current_span("cli.web.instagram.scan") as span:
        span.set_attribute("scan.url_count", len(targets))
        for url, name in targets:
            label = name or url
            prefix = f"  {BOLD}{label:18}{RESET}"
            cands = discover_handles(
                url, names=(name,) if name else (),
                timeout_s=getattr(args, "timeout", 15) or 15,
            )
            confident = [c for c in cands if c.confident]
            if confident:
                best = confident[0]
                print(f"{prefix}  {GREEN}@{best.handle}{RESET}  {DIM}(match {best.affinity}, "
                      f"linked {best.count}x){RESET}")
                found.append((name, best))
            elif cands:
                others = ", ".join(f"@{c.handle}" for c in cands[:3])
                print(f"{prefix}  {YELLOW}unsure{RESET}  {DIM}{others}{RESET}")
            else:
                print(f"{prefix}  {DIM}none{RESET}")
        span.set_attribute("scan.hit_count", len(found))

    print(f"\n{BOLD}{len(found)}/{len(targets)}{RESET} site(s) name an Instagram account "
          f"clearly enough to adopt.")
    if not found:
        return 0
    if not getattr(args, "apply", False):
        print(f"{DIM}Re-run with --apply to add them to the instagram source.{RESET}\n")
        return 0

    return _apply_instagram_handles(found, getattr(args, "source", "instagram"))


def _apply_instagram_handles(found, source_name: str) -> int:
    """Merge discovered handles into the instagram source's ``accounts`` list."""
    from event_harvester.sources.web_fetch import _WEB_SOURCES_FILE, _load_web_sources
    from event_harvester.utils import save_json

    sources = _load_web_sources()
    src = next((s for s in sources if s.get("name") == source_name), None)
    if src is None:
        print(f"{RED}No source named {source_name!r}.{RESET} "
              f"Add one with mode 'instagram' first, or pass --source.")
        return 1
    if src.get("mode") != "instagram":
        print(f"{YELLOW}{source_name} is mode={src.get('mode')!r}, not 'instagram'.{RESET} "
              f"Switching it would change what that source harvests — not doing it for you.")
        return 1

    accounts = list(src.get("accounts") or [])
    existing = {
        (a.get("handle") if isinstance(a, dict) else str(a)).lstrip("@").lower()
        for a in accounts
    }
    added = 0
    for venue, cand in found:
        if cand.handle.lower() in existing:
            continue
        entry = {"handle": cand.handle, "name": venue} if venue else {"handle": cand.handle}
        accounts.append(entry)
        existing.add(cand.handle.lower())
        added += 1
        suffix = f"  {DIM}({venue}){RESET}" if venue else ""
        print(f"  {GREEN}added{RESET} @{cand.handle}{suffix}")

    if not added:
        print(f"{DIM}Nothing new — every confident handle is already configured.{RESET}\n")
        return 0

    src["accounts"] = accounts
    save_json(_WEB_SOURCES_FILE, sources)
    print(f"\n{GREEN}{BOLD}Updated{RESET} {source_name}: {len(accounts)} account(s) "
          f"in {_WEB_SOURCES_FILE}\n")
    return 0


def _web_replay(args, cfg) -> int:
    """Replay one configured source's route, printing every step as it runs.

    The debugging surface for `mode: steps`. A harvest reports "meetup: 0
    messages"; this reports which step stopped producing and what the page
    looked like when it did.
    """
    from event_harvester.sources.uc_browser import WebBrowser, native_browser
    from event_harvester.sources.web_fetch import _load_web_sources
    from event_harvester.sources.web_steps import replay, validate_steps
    from event_harvester.sources.web_utils import BOT_CHECK_WAIT_S

    src = next((s for s in _load_web_sources() if s.get("name") == args.name), None)
    if src is None:
        print(f"{RED}No source named {args.name!r}.{RESET} See: event-harvester web list")
        return 1

    steps = src.get("steps") or []
    if not steps:
        print(f"{YELLOW}{args.name} has no steps{RESET} (mode={src.get('mode')!r}).")
        print(f"{DIM}Record one with the web_explore_* MCP tools.{RESET}")
        return 1

    errors = validate_steps(steps)
    if errors:
        print(f"\n{RED}Route is invalid — not running it:{RESET}")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"\n{BOLD}Replaying {args.name}{RESET} {DIM}({len(steps)} steps){RESET}\n")
    cfg_llm = cfg.llm if getattr(args, "diagnose", False) else None

    browser = (
        native_browser(src["url"], int(src.get("bot_check_wait_s", BOT_CHECK_WAIT_S)))
        if src.get("native_chrome") else WebBrowser()
    )
    with browser as uc:
        page = uc.open(src["url"], source_name=args.name)
        result = replay(uc, page, steps, cfg_llm=cfg_llm)
        for step_result in result.results:
            mark = f"{GREEN}ok{RESET}" if step_result.ok else f"{RED}FAILED{RESET}"
            print(f"  {step_result.index:>2}. {step_result.verb:<8} {mark}  "
                  f"{DIM}{step_result.detail} ({step_result.elapsed_ms}ms){RESET}")
        try:
            page.close()
        except Exception as e:
            logger.debug("replay: page close failed: %s", e)

    print(f"\n  {BOLD}{len(result.links)}{RESET} event link(s), "
          f"{BOLD}{len(result.texts)}{RESET} text block(s)")

    if result.diagnosis:
        diag = result.diagnosis
        print(f"\n  {YELLOW}Why it stopped:{RESET} {diag.get('kind')}")
        for hint in diag.get("hints", [])[:3]:
            step = hint.get("suggested_step")
            suffix = f"  {DIM}→ try {step}{RESET}" if step else ""
            print(f"    - {hint.get('kind')} ({hint.get('confidence', 0):.0%}): "
                  f"{hint.get('evidence')}{suffix}")
        if diag.get("screenshot"):
            print(f"    {DIM}screenshot: {diag['screenshot']}{RESET}")

    print()
    return 0 if result.ok else 1


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


def _run_url_diagnostics(url: str) -> dict:
    """Open a URL and run all detection modes.

    Returns ``{"mode", "event_links", "feed_items"}``. The counts let the add
    flow tell a thin page (``raw``, nothing found) from a real source and log
    the difference; ``mode`` is None when a Cloudflare challenge blocked it.
    """
    import concurrent.futures

    # iCalendar feeds are not pages -- handle before touching a browser.
    if _is_ics_url(url):
        return {"mode": _diagnose_ics(url), "event_links": 0, "feed_items": 0}

    from event_harvester.sources.uc_browser import WebBrowser
    from event_harvester.sources.web_utils import (
        _extract_event_links,
        _extract_text,
        _wait_cloudflare,
        is_bot_check,
    )

    result = {"mode": None, "event_links": 0, "feed_items": 0}

    def _do_diagnostics():
        with WebBrowser() as uc:
            print(f"  Testing: {url}\n")
            page = uc.open(url, wait_ms=3000)
            html = _wait_cloudflare(page, headless=False)

            if is_bot_check(html):
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

            result["mode"] = mode
            result["event_links"] = len(event_links)
            result["feed_items"] = len(feed_items)

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

    return result


def _suggest_name(url: str) -> str:
    """House-style name for a URL: the venue, not the platform host."""
    from urllib.parse import urlparse

    netloc = urlparse(url if "://" in url else "https://" + url).netloc
    name = netloc.split(".")[0]
    if name in ("www", "business", "cdn", "calendar", "tockify"):
        parts = netloc.split(".")
        name = parts[-2] if len(parts) >= 2 else name
    return name


def _add_source_to_config(
    url: str, detected_mode: str | None = None, *, source_page: str | None = None,
    native_chrome: bool = False,
) -> None:
    """Add a web source to data/web_sources.json using the detected mode.

    ``source_page`` is the human-facing page a feed URL was discovered from.
    It is stored so the entry still says which venue it belongs to when the
    feed URL is an opaque CDN path (``cdn.dnalounge.com/calendar/...``).

    ``native_chrome`` marks a source whose wall only a real browser clears (a
    Cloudflare challenge, like erobay); it also gets a cooldown, since the
    native-Chrome path is serial and slow.
    """
    from event_harvester.sources.web_fetch import _WEB_SOURCES_FILE, _load_web_sources
    from event_harvester.utils import save_json

    sources = _load_web_sources()

    if any(s["url"] == url for s in sources):
        print(f"\n  {BOLD}Already configured{RESET} — {url} is already in web_sources.json")
        return

    # Name from the page the user actually typed, not the feed host.
    name = _suggest_name(source_page or url)

    mode = detected_mode or "calendar"
    source: dict = {"url": url, "name": name, "mode": mode}
    if source_page and source_page != url:
        source["source_page"] = source_page
    if mode == "feed":
        source["api_pattern"] = r"api|graphql"
        source["scroll_seconds"] = 15
    elif mode == "search":
        source["query"] = "events"
        source["scroll_seconds"] = 10
    if native_chrome:
        source["native_chrome"] = True
        source["cooldown_hours"] = 6

    sources.append(source)
    save_json(_WEB_SOURCES_FILE, sources)
    extra = " native_chrome" if native_chrome else ""
    print(f"\n  {GREEN}{BOLD}Added{RESET} {name} (mode={mode}{extra}) -> {_WEB_SOURCES_FILE}")
