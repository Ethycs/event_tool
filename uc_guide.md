# Universal Controller + Playwright Integration Guide

## Overview

This project integrates [Universal Controller](https://github.com/Ethycs/universal_controller) as a Chrome extension that provides programmatic browser automation via Playwright. The full UC codebase (detection, actions, passive observer, LSH fingerprinting, signatures) is bundled into a single MV3 extension, with a Python API (`UCBrowser`) that orchestrates everything through `page.evaluate()`.

**Three detection modes:**
1. **Scan-diff-bind** (Cheat Engine style) — Snapshot DOM, perform action, diff, infer pattern
2. **Static three-signal detect** — Structural + phrasal + semantic + behavioral scoring
3. **Generic discovery** — Find inputs/buttons by scoring all visible elements (works without pattern detection)

**Three browser modes:**
- `UCBrowser()` — Installed Chrome + logged-in profile (best for sites needing auth)
- `UCBrowser(use_extension=True)` — Playwright Chromium + UC extension (enables all UC features)
- `UCBrowser(native_chrome=True)` — Real Chrome via CDP (bypasses Cloudflare)

## Architecture

```
UCBrowser (Python)
  └─ Playwright launch_persistent_context(--load-extension)
       └─ Chromium (headed, persistent profile)
            └─ UC Extension (world: "MAIN", Rollup bundle from submodule)
                 ├─ ValueScanner: DOM snapshots + diffing
                 ├─ Three-signal detector: structural + phrasal + semantic + behavioral
                 ├─ PassiveDetector: MutationObserver + event correlation
                 ├─ DOMLocalityHash: MinHash LSH structural fingerprinting
                 ├─ SignatureStore: per-domain pattern persistence
                 ├─ Actions: setText (React/Slate/ProseMirror), chatSend, formFill, etc.
                 ├─ Generic discovery: __UC_findInputs, __UC_findButtons
                 ├─ Trigram baseline: __UC_captureBaseline, __UC_extractResponse
                 ├─ MutationObserver: __UC_watchContainer (real-time streaming)
                 └─ All exposed on window.__UC_* → Python reads via page.evaluate()
```

## Project Structure

```
ext/
├── universal_controller/        # Git submodule (upstream UC repo, untouched)
│   ├── src/                     # UC source (ES modules)
│   ├── rollup.config.js         # Tampermonkey build (npm run build → dist/*.user.js)
│   └── package.json
└── uc_extension/                # Our MV3 Chrome extension wrapper
    ├── manifest.json            # MV3, world: "MAIN", <all_urls>
    ├── package.json             # Rollup build for extension
    ├── rollup.config.js         # Bundles UC source + entry point → dist/uc-extension.js
    ├── src/
    │   ├── extension-entry.js   # Entry point: creates controller, exposes window.__UC_*
    │   ├── storage-adapter.js   # GM_getValue/GM_setValue → localStorage shim
    │   └── background.js        # Minimal MV3 service worker
    ├── dist/
    │   └── uc-extension.js      # Built bundle (~5,700 lines, all UC modules)
    └── findings.md              # ChatGPT-specific DOM findings

src/event_harvester/sources/
├── uc_browser.py                # UCBrowser class — full Python API
└── web_fetch.py                 # Harvest pipeline integration

tests/
└── test_uc_chatgpt.py           # Incremental test script for ChatGPT
```

## Setup

```bash
# Clone with submodules
git clone --recurse-submodules <repo-url>

# Or if already cloned
git submodule update --init --recursive

# Install Python dependencies
pixi install

# Build the extension bundle
cd ext/uc_extension && npm install && npm run build
```

The Tampermonkey userscript build is independent:
```bash
cd ext/universal_controller && npm install && npm run build
# → dist/universal-controller.user.js
```

## Quick Start

### Chat with any AI (ChatGPT, Copilot, Gemini, etc.)

```python
from event_harvester.sources import UCBrowser

with UCBrowser(use_extension=True) as uc:
    page = uc.open("https://chatgpt.com", wait_ms=5000)
    response = uc.chat(page, "What is 2+2? Just the number.")
    print(response)  # "4"
```

The `chat()` method uses the full UC toolbox automatically:
1. `__UC_findInputs` — finds the chat input (scored by chat-likelihood)
2. `__UC_setText` — types using framework-aware methods (React, Slate, contenteditable)
3. `__UC_findButtons` — finds the send button (proximity + label scoring)
4. `__UC_captureBaseline` — trigram fingerprint of page text before send
5. `__UC_firstScan` — scan-diff baseline before send
6. `__UC_watchContainer` — MutationObserver on the conversation area
7. Send verification — checks if input cleared (postcondition)
8. `__UC_findNewContent` — scan-diff discovers the response container
9. Trigram set difference — filters response from boilerplate (nav, sidebar, footer)
10. `__UC_saveSignature` — persists working pattern for next visit

### Search event sites

```python
with UCBrowser(use_extension=True) as uc:
    page, results = uc.navigate_and_search("https://lu.ma/discover", "AI meetup SF")
    print(results)
```

### Scrape feeds with smart scrolling

```python
with UCBrowser(use_extension=True) as uc:
    page, items = uc.navigate_and_scrape(
        "https://www.eventbrite.com/d/ca--san-francisco/events/",
        scroll_seconds=20,
    )
    for item in items:
        print(item[:100])
```

### Use installed Chrome with login sessions

```python
# Default mode: uses installed Chrome + data/.chrome_profile
with UCBrowser() as uc:
    page = uc.open("https://www.instagram.com/")
    # Already logged in from previous web_login() session
    items = uc.scroll_feed(page, seconds=15)
```

### Step-by-step control

```python
with UCBrowser(use_extension=True) as uc:
    page = uc.open("https://example.com/app")

    # Detect all patterns
    patterns = uc.detect_all(page)

    # Handle obstacles
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    # Scan-diff workflow
    uc.first_scan(page)
    page.click("button.filter")
    diff = uc.next_scan(page)
    detected = uc.auto_detect(page)

    # Generic discovery (no pattern detection needed)
    inputs = uc.find_inputs(page)
    buttons = uc.find_buttons(page, inputs[0]["selector"])

    # Framework-aware text input
    result = uc.set_text(page, "#my-input", "hello")
    # result = {success: True, method: "execCommand"}

    # Passive detection (background monitoring)
    uc.start_passive(page)
    page.click("button")
    passive = uc.get_passive_results(page)

    # Structural fingerprinting (LSH)
    fw = uc.scan_framework(page)
    ctx = uc.get_llm_context(page, "chat")
    heap = uc.heap_scan(page, "chat")
```

## UCBrowser API Reference

### Constructor

```python
UCBrowser(
    headless=False,        # Headless mode (no extension support)
    channel="chrome",      # Browser channel ("chrome", "msedge", or None for Chromium)
    stealth=True,          # Apply playwright-stealth
    timeout_ms=30000,      # Default navigation timeout
    use_extension=False,   # Load UC extension (requires Playwright Chromium)
    native_chrome=False,   # Launch real Chrome via CDP (bypasses Cloudflare)
)
```

### Browser Modes

| Mode | Channel | Profile | Extension | Best for |
|---|---|---|---|---|
| `UCBrowser()` | Chrome | `data/.chrome_profile` | No | Logged-in scraping (Instagram, etc.) |
| `UCBrowser(use_extension=True)` | Chromium | `data/.uc_chromium_profile` | Yes | Chat, detection, all UC features |
| `UCBrowser(native_chrome=True)` | Chrome (CDP) | Default Chrome profile | User's own | Cloudflare-protected sites |
| `UCBrowser(headless=True)` | Chrome | State file | No | CI/headless scraping |

### Page Management

| Method | Returns | Description |
|---|---|---|
| `open(url, wait_ms=2000)` | Page | Open URL in new tab |
| `close()` | None | Shut down browser |

### Chat (Generic — works on any chat site)

| Method | Returns | Description |
|---|---|---|
| `chat(page, message, timeout_s=30)` | str or None | Full pipeline: find input → type → send → wait → extract response |
| `find_inputs(page)` | list[dict] | All interactive inputs, scored by chat-likelihood |
| `find_buttons(page, input_selector)` | list[dict] | Submit/send buttons near an input, scored |
| `set_text(page, selector, text)` | dict | Framework-aware text input (React, Slate, contenteditable) |
| `find_new_content(page)` | list[dict] | Elements with children-added or text-grew (after scan-diff) |

### Scan-Diff-Bind (Interaction-Based)

| Method | Returns | Description |
|---|---|---|
| `first_scan(page)` | dict or None | Baseline DOM snapshot |
| `next_scan(page)` | dict or None | Diff against last snapshot |
| `auto_detect(page)` | list[dict] | Infer patterns from last diff |
| `scan_action(page, action)` | list[dict] | scan → action → diff → detect in one call |

### Static Detection (Three-Signal)

| Method | Returns | Description |
|---|---|---|
| `detect(page, pattern_name, guarantee)` | list[dict] | Detect one pattern type |
| `detect_all(page, guarantee)` | dict | Detect all 8 pattern types |
| `get_patterns(page)` | dict | Read current patterns (no new detection) |

### Pattern Actions

| Method | Returns | Description |
|---|---|---|
| `search(page, query, submit=True)` | bool | Fill detected search bar |
| `scroll_feed(page, seconds, on_item)` | list[str] | Scroll feed, collect item texts |
| `get_feed_text(page)` | str | Text from detected feed container |
| `get_feed_items(page)` | list[str] | Individual feed item texts |
| `fill_form(page, fields)` | bool | Fill form fields by name/type |
| `submit_form(page)` | bool | Click form submit button |
| `dismiss_cookies(page)` | bool | Click cookie consent accept button |
| `close_modal(page)` | bool | Dismiss modal/dialog |
| `has_login_wall(page)` | bool | Check for blocking login wall |

### Bound UC Actions (requires detect + bind)

| Method | Returns | Description |
|---|---|---|
| `bind(page, pattern_name)` | dict or None | Detect and bind a pattern to create UC action API |
| `chat_send(page, text)` | bool | Send via bound chat API (async, framework-aware) |
| `chat_get_messages(page)` | list | Get visible messages from bound chat |
| `form_fill_uc(page, data)` | bool | UC priority-based fill (name > id > type > placeholder) |
| `dropdown_toggle(page)` | bool | Toggle bound dropdown |
| `dropdown_select(page, value)` | bool | Select dropdown option by text |
| `modal_close_uc(page)` | bool | Close via button + Escape fallback |

### Advanced / Diagnostics

| Method | Returns | Description |
|---|---|---|
| `start_passive(page)` | bool | Start background MutationObserver + event correlation |
| `stop_passive(page)` | bool | Stop passive detection |
| `get_passive_results(page)` | list | Patterns inferred by passive detection |
| `scan_framework(page)` | dict | Detect React/Vue/Angular/Svelte |
| `heap_scan(page, pattern_name)` | dict | Inspect React fiber, Vue instances, Redux stores |
| `get_llm_context(page, pattern_name)` | str | Extract DOM context formatted for LLM prompts |

### Signatures (Per-Domain Persistence)

| Method | Returns | Description |
|---|---|---|
| `save_signature(page, pattern_name)` | dict | Save working pattern for this domain |
| `load_signatures(page)` | list | Load saved signatures for current domain |
| `auto_bind_signatures(page)` | list | Auto-bind from saved signatures |

### Convenience

| Method | Returns | Description |
|---|---|---|
| `navigate_and_search(url, query)` | (Page, str) | Open → detect → clear obstacles → search → return results |
| `navigate_and_scrape(url, scroll_seconds)` | (Page, list) | Open → detect → clear obstacles → scroll → return items |

## How the Chat Pipeline Works

```
chat(page, "What is 2+2?")
  │
  ├─ __UC_findInputs()          → #prompt-textarea (score=7.6, contenteditable)
  ├─ __UC_captureBaseline()     → 324 trigrams of existing page text
  ├─ __UC_firstScan()           → 592 elements baselined
  │
  ├─ __UC_setText(selector, msg)  → execCommand (React contenteditable)
  ├─ __UC_findButtons(selector)   → #composer-submit-button (score=6.3)
  ├─ __UC_watchContainer(area)    → MutationObserver active
  ├─ page.click(button)           → Submit
  ├─ Verify: input cleared?       → Yes ✓
  │
  ├─ _wait_chat_response() loop (500ms ticks):
  │   ├─ Layer 1: __UC_getObserved()     → real-time MutationObserver data
  │   ├─ Layer 2: __UC_findNewContent()  → scan-diff container discovery
  │   ├─ Layer 3: trigram set difference → filter response from boilerplate
  │   └─ Stability: 3 identical polls or streaming indicators gone
  │
  ├─ Response: "4"
  └─ __UC_saveSignature("chat")  → persisted for next visit
```

## Trigram Set Difference (Response Extraction)

The key innovation for extracting clean response text from complex pages:

**Before send:** `__UC_captureBaseline()` computes a trigram set of all visible page text.

**After send:** For each candidate element in the response container, compute its trigram set and measure overlap with baseline:
```
new_ratio = |element_trigrams − baseline_trigrams| / |element_trigrams|
```

- Nav/sidebar: `new_ratio ≈ 0.05` (almost all existed before) → filtered out
- User's message: filtered by checking if text contains `sent_message`
- AI response: `new_ratio ≈ 0.95` (almost all new) → this is the response

## Test Results (March 2026)

| Site | Input | setText | Send | Response | Status |
|---|---|---|---|---|---|
| **ChatGPT** | `#prompt-textarea` (ce) | execCommand | `#composer-submit-button` | Clean response | **Working** |
| **Copilot** | `#userInput` | nativeSetter | `Submit message` btn | Clean response | **Working** |
| **Gemini** | `div[aria-label="Enter a prompt"]` (ce) | execCommand | `Send message` btn | Sends OK, extraction partial | **Partial** |
| **Perplexity** | `#ask-input` (ce) | directTextContent | `Submit` btn | Sends OK, response timeout | **Partial** |
| **EaseMate** | `textarea` (8.5 score) | nativeSetter | `Send Message` btn | Response with encoding issue | **Partial** |
| **Felo** | `#chat-input` | nativeSetter | Found btn | 401 Unauthorized | **Needs auth** |
| **Claude/DeepSeek/etc.** | — | — | — | — | **Login required** |

## Extension API Reference (window.__UC_*)

### State
```javascript
window.__UC                    // {version, ready, mode, timestamp, url, scan, diff, patterns}
```

### Scan-Diff
```javascript
__UC_firstScan()               // Baseline snapshot → {elements, timestamp}
__UC_nextScan()                // Diff → {changed, added, removed, increased, decreased}
__UC_autoDetect()              // Infer from diff → [{pattern, confidence, proof, selector}]
```

### Detection
```javascript
__UC_detect(name, guarantee)   // Three-signal detect → [{selector, confidence, evidence, ...}]
__UC_detectAll(guarantee)      // All 8 patterns at once
```

### Generic Discovery
```javascript
__UC_findInputs()              // All inputs scored → [{selector, score, tag, contentEditable, ...}]
__UC_findButtons(inputSel)     // Buttons near input → [{selector, score, label, type}]
__UC_setText(selector, text)   // Framework-aware → {success, method}
__UC_clickButton(selector)     // Click → true/false
```

### Trigram Extraction
```javascript
__UC_captureBaseline()         // Snapshot page trigrams → {trigrams, textLength}
__UC_extractResponse(minRatio) // Find new text → [{selector, text, newRatio, ...}]
```

### Real-Time Observation
```javascript
__UC_watchContainer(selector)  // Start MutationObserver → true/false
__UC_getObserved()             // Read observed mutations → [{type, text, tag, timestamp}]
__UC_stopWatching()            // Stop + return final observations
```

### Structural Fingerprinting (LSH)
```javascript
__UC_computeSignature(sel)     // MinHash → {fingerprint, features, minhash}
__UC_compareSig(sig1, sig2)    // Jaccard similarity → 0.0–1.0
__UC_indexSignature(key, sig)  // Add to LSH index
__UC_querySimilar(sig)         // Find similar indexed elements
```

### Actions (require bind)
```javascript
__UC_chatSend(text)            // Send chat message (async, framework-aware)
__UC_chatGetMessages()         // Get visible messages
__UC_chatOnMessage(callback)   // Real-time message observer
__UC_formFill(data)            // Fill form (priority: name > id > type > placeholder)
__UC_formSubmit()              // Submit form
__UC_dropdownToggle()          // Toggle dropdown
__UC_dropdownSelect(value)     // Select option by text
__UC_modalClose()              // Close modal (button + Escape fallback)
```

### Convenience
```javascript
__UC_dismiss()                 // Click cookie consent accept button
__UC_fillSearch(query)         // Fill detected search bar
__UC_getVisibleText()          // Text from detected feed or full page
__UC_bind(patternName)         // Detect + bind → {pattern, path}
__UC_unbind(patternName)       // Remove binding
```

### Advanced
```javascript
__UC_startPassive()            // Background event correlation
__UC_stopPassive()
__UC_getPassiveResults()       // Inferred patterns
__UC_getLLMContext(name)        // DOM context formatted for LLM
__UC_heapScan(name)            // React/Vue internals
__UC_scanFramework()           // Detect framework
__UC_findNewContent()          // Scan-diff: elements with children-added/text-grew
```

### Signatures
```javascript
__UC_saveSignature(name)       // Persist working pattern for domain
__UC_loadSignatures()          // Load saved for current domain
__UC_autoBindSignatures()      // Auto-bind from saved
__UC_getAllSignatures()         // All signatures across all domains
```

## Building

```bash
# Build the Chrome extension bundle
cd ext/uc_extension && npm run build
# → dist/uc-extension.js (IIFE bundle, ~5,700 lines)

# Watch mode for development
cd ext/uc_extension && npm run watch

# Build the Tampermonkey userscript (independent)
cd ext/universal_controller && npm run build
# → dist/universal-controller.user.js
```

## Debugging

### Check extension loaded
Open `chrome://extensions` in the Playwright browser. Look for "Universal Controller (Dev)".

### DevTools console
```javascript
window.__UC                         // Check ready state
window.__UC_detectAll()             // Run full detection
window.__UC_findInputs()            // See what inputs are found
window.__UC_findButtons("#myinput") // See buttons near input
window.__UC_captureBaseline()       // Take trigram snapshot
window.__UC_scanFramework()         // Detect React/Vue/etc.
```

### Common issues

| Problem | Cause | Fix |
|---|---|---|
| `window.__UC` undefined | Extension not loaded | Use `use_extension=True`; check manifest.json exists |
| Extension loads but detection empty | Detection not triggered | Call `detect_all()` — detection is not automatic |
| `launch_persistent_context` crashes | Corrupted profile | Delete `data/.uc_chromium_profile/` |
| `channel='chrome'` + extension | Branded Chrome blocks extensions | Use `use_extension=True` (auto-uses Chromium) |
| Chat sends but no response | Site needs auth, or response in Shadow DOM | Try `web_login()` first; check page text manually |
| Trigram filter returns wrong text | Response trigrams overlap with baseline | Check `__UC_extractResponse(0.3)` results in console |
