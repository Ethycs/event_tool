# Universal Controller + Playwright Integration Guide

## Overview

This project integrates [Universal Controller](https://github.com/Ethycs/universal_controller) as a Chrome extension that provides two modes of UI pattern detection, both driven by Playwright:

1. **Scan-diff-bind** (Cheat Engine style) — Take a DOM snapshot, perform an action, take another snapshot, diff them to infer what pattern the UI implements
2. **Static three-signal detect** — Score DOM elements using structural + phrasal + semantic + behavioral signals without requiring interaction

Detection is **not automatic** — Playwright orchestrates when and how detection runs.

## Architecture

```
UCBrowser (Python)
  └─ Playwright launch_persistent_context(--load-extension)
       └─ Chrome (headed, persistent profile)
            └─ UC Extension content script (world: "MAIN")
                 ├─ ValueScanner: DOM snapshots + diffing
                 ├─ Three-signal detector: structural + phrasal + semantic + behavioral
                 ├─ Writes results to window.__UC on demand
                 └─ Exposes: __UC_firstScan(), __UC_nextScan(), __UC_detect(), etc.
                      └─ Python calls via page.evaluate()
```

**Key constraint:** Chrome extensions require headed mode. Headless runs fall back to plain Playwright (no UC detection).

## Project Structure

```
ext/
├── universal_controller/    # Git submodule (upstream UC repo)
└── uc_extension/            # Our MV3 Chrome extension wrapper
    ├── manifest.json        # Manifest V3, world: "MAIN", <all_urls>
    └── content.js           # ValueScanner + detection + window.__UC API

src/event_harvester/sources/
├── uc_browser.py            # UCBrowser class — high-level Python API
└── web_fetch.py             # Modified to load extension + use UC patterns
```

## Setup

```bash
# Clone with submodules
git clone --recurse-submodules <repo-url>

# Or if already cloned
git submodule update --init --recursive

# Install dependencies
pixi install
```

No extension build step needed — Chrome loads `ext/uc_extension/` directly as an unpacked extension.

## Quick Start

### Static detect — no interaction needed

The simplest path. Three-signal scoring finds patterns by examining the DOM:

```python
from event_harvester.sources import UCBrowser

with UCBrowser() as uc:
    page = uc.open("https://lu.ma/discover")

    # Detect all patterns at once
    patterns = uc.detect_all(page)
    # patterns = {search: [...], feed: [...], form: [...], ...}

    # Or detect a single type
    search_hits = uc.detect(page, "search")

    # Use what was found
    uc.dismiss_cookies(page)
    uc.search(page, "AI meetup San Francisco")
    text = uc.get_feed_text(page)
    print(text)
```

### Scan-diff-bind — interaction-based, higher confidence

The Cheat Engine workflow. Snapshots the DOM before and after an action to infer what changed:

```python
with UCBrowser() as uc:
    page = uc.open("https://example.com/app")

    # One-liner: scan → action → diff → detect
    patterns = uc.scan_action(page, lambda p: p.click("button.filter"))
    # patterns = [{pattern: "dropdown", confidence: 0.7, proof: "became-visible + expanded/listbox"}]

    # Or step by step:
    uc.first_scan(page)                # 1. baseline snapshot
    page.fill("input.search", "test")  # 2. perform an action
    page.press("input.search", "Enter")
    diff = uc.next_scan(page)          # 3. diff: {changed: 5, added: 12, removed: 0}
    detected = uc.auto_detect(page)    # 4. infer patterns from diff
```

### High-level convenience methods

```python
with UCBrowser() as uc:
    # Search: open → detect → dismiss cookies → close modals → search → return results
    page, results = uc.navigate_and_search("https://lu.ma/discover", "hackathon SF")
    print(results)

    # Scrape: open → detect → dismiss cookies → scroll feed → return items
    page, items = uc.navigate_and_scrape(
        "https://www.eventbrite.com/d/ca--san-francisco/events/",
        scroll_seconds=20,
    )
    for item in items:
        print(item[:100])
```

### Forms

```python
with UCBrowser() as uc:
    page = uc.open("https://example.com/register")
    uc.fill_form(page, {"email": "user@example.com", "name": "Test User"})
    uc.submit_form(page)
```

### Low-level: direct page.evaluate()

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        "data/.chrome_profile",
        headless=False,
        args=[
            "--load-extension=ext/uc_extension",
            "--disable-extensions-except=ext/uc_extension",
        ],
    )
    page = context.new_page()
    page.goto("https://lu.ma/discover")
    page.wait_for_function("window.__UC && window.__UC.ready")

    # Static detect
    page.evaluate('window.__UC_detect("search")')
    page.evaluate('window.__UC_detectAll()')

    # Scan-diff workflow
    page.evaluate("window.__UC_firstScan()")
    page.click("button.filter")     # action
    page.evaluate("window.__UC_nextScan()")
    page.evaluate("window.__UC_autoDetect()")

    # Read results
    uc = page.evaluate("window.__UC")
    print(uc["patterns"])

    # Actions
    page.evaluate("window.__UC_dismiss()")
    page.evaluate("window.__UC_fillSearch('events this weekend')")
    text = page.evaluate("window.__UC_getVisibleText()")
```

## UCBrowser API Reference

### Constructor

```python
UCBrowser(
    headless=False,    # Must be False for extension support
    channel="chrome",  # Browser channel
    stealth=True,      # Apply playwright-stealth
    timeout_ms=30000,  # Default navigation timeout
)
```

### Page Management

| Method | Returns | Description |
|---|---|---|
| `start()` | None | Launch browser (called automatically by `__enter__`) |
| `close()` | None | Shut down browser |
| `open(url, wait_ms=2000)` | Page | Open URL in new tab |

### Scan-Diff-Bind (Interaction-Based)

| Method | Returns | Description |
|---|---|---|
| `first_scan(page)` | dict or None | Baseline DOM snapshot ({elements, timestamp}) |
| `next_scan(page)` | dict or None | Diff against baseline ({changed, added, removed, ...}) |
| `auto_detect(page)` | list[dict] | Infer patterns from last diff |
| `scan_action(page, action)` | list[dict] | Convenience: scan → action → diff → detect |

### Static Detection (Three-Signal)

| Method | Returns | Description |
|---|---|---|
| `detect(page, pattern_name, guarantee)` | list[dict] | Detect one pattern type |
| `detect_all(page, guarantee)` | dict | Detect all pattern types |
| `get_patterns(page)` | dict | Read current patterns (no new detection) |

### Actions

| Method | Returns | Description |
|---|---|---|
| `search(page, query, submit=True)` | bool | Fill detected search bar, optionally press Enter |
| `scroll_feed(page, seconds=15, on_item=None)` | list[str] | Scroll feed, collect item texts |
| `get_feed_text(page)` | str | Text from detected feed (or full page) |
| `get_feed_items(page)` | list[str] | Individual feed item texts |
| `fill_form(page, fields)` | bool | Fill detected form fields by name/type |
| `submit_form(page)` | bool | Click form submit button |
| `dismiss_cookies(page)` | bool | Click cookie consent accept button |
| `close_modal(page)` | bool | Dismiss detected modal/dialog |
| `has_login_wall(page)` | bool | Check for blocking login wall |

### Convenience Methods

| Method | Returns | Description |
|---|---|---|
| `navigate_and_search(url, query)` | (Page, str) | Open → detect → clear obstacles → search → return results |
| `navigate_and_scrape(url, scroll_seconds)` | (Page, list) | Open → detect → clear obstacles → scroll → return items |

## How Detection Works

### Mode 1: Scan-Diff-Bind (Cheat Engine Style)

This is UC's core paradigm. It works by observing what changes when you perform an action:

```
first_scan()  →  DOM snapshot A (all element values, dimensions, ARIA state, etc.)
     ↓
(user/Playwright performs action)
     ↓
next_scan()   →  DOM snapshot B  →  diff(A, B)
     ↓
autoDetect()  →  Infer patterns from changes:
                   • input-cleared + children-added → chat
                   • became-visible + fixed/dialog → modal
                   • became-visible + aria-expanded → dropdown
```

**When to use:** When you're exploring an unknown site and want to understand what UI patterns are present. Perform actions (click buttons, type in inputs) and see what UC detects.

### Mode 2: Three-Signal Static Detect

Scores DOM elements without requiring interaction:

```
detect("search")  →  For each candidate element:
                       ├─ Structural (25%): CSS selector match, rule checking
                       ├─ Phrasal (30%): keyword scoring (strong/medium/placeholder/button/negative)
                       ├─ Semantic (15%): ARIA roles
                       └─ Behavioral (30%): component presence (has input? has button? is scrollable?)
                       = confidence score (0.0 – 1.0)
```

Guarantee levels control the confidence threshold:
- `STRUCTURAL` (0.2) — lowest bar, most candidates
- `SEMANTIC` (0.35)
- `BEHAVIORAL` (0.5) — default, good balance
- `VERIFIED` (0.7) — highest confidence only

**When to use:** When you know what pattern you're looking for (e.g., "find the search bar on this page").

### Pattern types

| Pattern | Key signals | Scan-diff detects via |
|---|---|---|
| `search` | `input[type="search"]`, `role="search"`, placeholder keywords | input-filled events |
| `feed` | Repeated children, scrollable container, `role="feed"` | children-added on scroll |
| `form` | `<form>` tag, multiple inputs, submit button | input-filled/cleared events |
| `modal` | `role="dialog"`, `aria-modal`, fixed positioning | became-visible events |
| `dropdown` | `aria-haspopup`, `aria-expanded` | expanded/collapsed events |
| `cookie` | class contains "cookie/consent/gdpr", accept button | — (use static detect) |
| `login` | Password input, class contains "login/signin" | — (use static detect) |
| `chat` | `role="log"`, `aria-live`, input + scrollable container | input-cleared + children-added |

### window.__UC schema

```javascript
{
  version: "0.2.0",
  ready: true,               // true once extension is loaded (NOT after detection)
  mode: "detected",          // null → "scan" → "diffed" → "detected"
  timestamp: 1711800000000,
  url: "https://...",
  scan: {                    // populated after firstScan()
    elements: 1234,
    timestamp: 1711800000000
  },
  diff: {                    // populated after nextScan()
    changed: 5, added: 12, removed: 0, increased: 3, decreased: 1
  },
  patterns: {                // populated after detect/detectAll/autoDetect
    search: [{
      selector: "input#search",
      confidence: 0.85,
      input_selector: "input#search",  // direct input element
      placeholder: "Search...",
      form_action: "/search",
      evidence: { structural: 0.8, phrasal: 0.7, semantic: 1, behavioral: 1 }
    }],
    feed: [{
      selector: "div.feed",
      confidence: 0.78,
      item_count: 12,
      item_selector: "div.feed > div",
      scrollable: true,
      evidence: { ... }
    }],
    // ... other pattern types
  }
}
```

## Integration with Existing Harvest Pipeline

`web_fetch.py` loads the extension in all `launch_persistent_context` calls and triggers `detectAll()` after each page load:

- **`web_login()`** — extension active during manual login for debugging
- **`fetch_event_pages()`** — calls `__UC_detectAll()`, auto-dismisses cookies, uses feed text extraction
- **`fetch_feeds()`** — detects feed containers for targeted scrolling

Fallback is automatic: if extension isn't present or detection times out, existing behavior is preserved.

## Debugging

### Check extension is loaded
Navigate to `chrome://extensions` — look for "Universal Controller (Dev)".

### Check detection in DevTools console
```javascript
window.__UC                           // extension state (ready should be true)
window.__UC_detectAll()               // run detection, see what's found
window.__UC.patterns                  // read results

// Scan-diff workflow
window.__UC_firstScan()               // take baseline
// ... perform an action ...
window.__UC_nextScan()                // see diff
window.__UC_autoDetect()              // infer patterns

// Actions
window.__UC_dismiss()                 // click cookie accept
window.__UC_fillSearch("test")        // fill search bar
```

### Common issues

| Problem | Cause | Fix |
|---|---|---|
| `window.__UC` is undefined | Extension not loaded | Check `ext/uc_extension/manifest.json` exists; must be headed mode |
| `patterns` is empty | Detection not triggered | Call `__UC_detectAll()` or `uc.detect_all(page)` — detection is not automatic |
| Search bar not found | Low confidence | Try `__UC_detect("search", "STRUCTURAL")` for a lower threshold |
| Scan-diff shows nothing | No action between scans | Perform a visible action between `firstScan()` and `nextScan()` |
| Extension not loaded in headless | Expected | Extensions require headed mode; headless uses plain Playwright fallback |

## Extending Detection

Edit `ext/uc_extension/content.js` to add selectors or adjust scoring:

```javascript
// In PATTERNS object — add CSS selectors:
search: {
  selectors: [
    // ... existing ...
    '[data-testid="search-input"]',  // site-specific
  ],
  rules: { "has-input": 3, "search-type": 3 },
},

// In PHRASAL object — add keyword signals:
search: {
  strong: ["search", "explore events"],  // +0.35 each
  medium: ["find", "look up", "filter"], // +0.15 each
  placeholders: ["search", "search..."], // +0.25 each
  buttons: ["search", "find", "go"],     // +0.20 each
  negative: ["message", "chat"],         // -0.25 each
},
```

Changes take effect on next page load — no build step needed.
