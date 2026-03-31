# UC Extension Improvements: Generic Input/Button Discovery

## Problem
UC's static detection fails on ChatGPT because it relies on pattern-specific selectors (e.g. `[role="log"]`, `[class*="chat"]`). ChatGPT uses `#prompt-textarea` (contenteditable), `[data-testid="send-button"]`, and no standard chat classes.

The scan-diff engine already detects changes generically (`input-cleared`, `children-added`, `text-grew`) but there's no high-level API that says "find me anything I can type into" without knowing what pattern it is.

## What Already Works
- `setText()` in `actions/text-input.js` handles contenteditable (Slate, ProseMirror, etc.)
- `chatSend()` in `actions/chat-api.js` uses MutationObserver to wait for value propagation
- `chatGetMessages()` walks the DOM to extract text leaf nodes
- `ValueScanner.detectPattern()` infers chat from `input-cleared + children-added`

## What's Missing

### 1. Generic Input Discovery (`__UC_findInputs`)
A function that finds ALL interactive inputs on the page without knowing the pattern type. Score by:
- Tag type: contenteditable (+3), textarea (+2), input (+1)
- Size: larger inputs are more likely primary
- Position: lower on page = more likely chat (vs header search)
- Placeholder text: "message", "ask", "type" → chat-like; "search", "find" → search-like
- Visibility: skip `display:none`, `aria-hidden`, `inert`

Returns array of `{selector, tag, contentEditable, placeholder, score, rect}`.

### 2. Generic Button Discovery (`__UC_findButtons`)
Given an input selector, find nearby submit/send buttons. Walk up DOM tree through `form`, `fieldset`, `[class*="composer"]`, parent chain. Score by:
- Label: "send"/"submit"/"post" (+4), "search"/"find" (+2), "cancel"/"close" (-3)
- `type="submit"` (+2)
- Proximity to input (Manhattan distance)
- Skip: toggle, menu, attach, upload buttons

Returns array of `{selector, label, type, score}`.

### 3. Framework-Aware Text Set (`__UC_setText`)
Wrap `setText()` from `text-input.js` as a window API. The function already handles:
- `execCommand('insertText')` (ProseMirror, TipTap, Slate)
- `beforeInput` + `InputEvent` (modern editors)
- Clipboard paste simulation
- Direct DOM manipulation (fallback)
- Proper `focus()` + `selectAll()` before insertion

### 4. Response Detection via Scan-Diff
After typing + submitting, use `nextScan()` diff to find where the response appeared:
- `children-added` on a scrollable container = response container
- `text-grew` on a text block = response content
- Poll until text stops growing (2 consecutive identical polls = stable)
- Check for streaming indicators: `[class*="streaming"]`, `[class*="typing"]`, `[data-testid*="stop"]`

## ChatGPT-Specific Findings
- Input: `#prompt-textarea` (contenteditable div, NOT textarea/input)
- Send button: `[data-testid="send-button"]`
- Response container: `[data-message-author-role="assistant"]`
- Stop button (streaming): `[data-testid*="stop"]`
- `setText()` should work because it tries execCommand first (ChatGPT likely uses a custom editor)

## Recommended Implementation Order
1. Add `__UC_findInputs()` — generic input scanner
2. Add `__UC_findButtons(inputSelector)` — proximity-based button finder
3. Add `__UC_setText(selector, text)` — expose setText as window API
4. Add `__UC_clickButton(selector)` — simple click helper
5. Test on: ChatGPT, Claude, Slack web, Discord web, Google search

## Architecture Note
These functions should live in `extension-entry.js` (not in the UC submodule) since they're Playwright-facing convenience APIs. The submodule's detection engine stays generic; the entry point exposes the right surface for our Python orchestrator.
