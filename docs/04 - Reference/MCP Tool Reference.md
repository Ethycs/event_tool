# MCP Tool Reference

**Status:** CURRENT · **Date:** 2026-07-30 (added the 9 site-authoring tools) · **Applies to:** `src/event_harvester/servers/mcp_server.py`, `ext/universal_controller/uc_browser/mcp/grok_tools.py`, `.mcp.json`

The `event-harvester` MCP server exposes **29 tools**: 6 harvest/approve/decline, **9 site-authoring** (added 2026-07-30), and 14 Grok/chat-session tools registered from `uc_browser` — plus 2 resources and 2 prompts. Index-based tools (`approve_event`, `decline_event`, `list_latest_events`) read `data/.mcp_events.json`; that cache is fully replaced on every `harvest_events` call, so indices from one harvest are only valid until the next.

## Harvest / review tools

### `config_summary`

No parameters.

**Returns:** `AppConfig` summary — `{version, days_back, sources: {discord, telegram, gmail, signal, web}, caps: {discord, telegram, gmail, signal, web, total, no_cap}, configured: {telegram, gmail, llm, ticktick}, llm: {backend, model}, ticktick_project}`.

**Mutates state:** No.

### `harvest_events`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `days` | `int \| None` | No | `None` | Overrides `cfg.days_back` for this run. Must be `> 0` (raises `ValueError` otherwise). |
| `only` | `str \| None` | No | `None` | Comma-separated source list, e.g. `"discord,gmail"`. Mutually exclusive with `skip`. |
| `skip` | `str \| None` | No | `None` | Comma-separated source list to exclude. Mutually exclusive with `only`. |
| `include_known` | `bool` | No | `False` | Include events already matched to an `"approved"`/`"declined"` fingerprint, not just `"new"` ones. |
| `load_path` | `str \| None` | No | `None` | Load raw messages from a JSON file instead of harvesting live sources. |
| `save_path` | `str \| None` | No | `None` | Save raw harvested messages to this JSON path. |
| `web_source` | `str \| None` | No | `None` | Limit web harvesting to one configured source name (from `data/web_sources.json`). |
| `no_cooldown` | `bool \| None` | No | `None` | Ignore per-web-source cooldowns for this run. `None` defers to `cfg.web.no_cooldown`. |
| `skip_analysis` | `bool \| None` | No | `None` | Return harvested/actionable message counts without running LLM extraction (no events produced). |
| `no_cap` | `bool` | No | `False` | Bypass per-source and total analysis caps. |
| `total_cap` | `int \| None` | No | `None` | Override the global analysis cap; `-1` means unlimited. Must be `>= -1`. |

**Returns:** `{ok, warnings, summary, stats, events}` on the normal path, or `{ok, warnings, stats, events: []}` when `skip_analysis=True` or no messages were found (`stats.analysis_skipped` flags the latter case). `events` is a list of `public_event` objects (`index, status, title, date, time, location, link, source, notes, priority`). `stats` includes `total_messages, actionable_messages, events, new_events, known_events, sources, analysis_skipped`, and (on the full path) `rejects` — a per-stage rejected-message count.

**Mutates state:** Yes — **`side-effecting`**. Overwrites `data/.mcp_events.json` in full, replacing all previous indices. May also write network/API traffic (Discord/Telegram/Gmail/Signal/web fetches) and, if `save_path`/`load_path` given, read/write that file.

### `list_latest_events`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `status` | `str` | No | `"all"` | One of `all, new, known, approved, declined`. Anything else returns `{"ok": False, "error": "..."}` without touching the cache. |

**Returns:** `{ok, generated_at, stats, events}` read straight from `data/.mcp_events.json`, filtered by `status` (`known` matches any status other than `"new"`).

**Mutates state:** No — read-only against the current cache. **`stateful`** in the sense that its output depends entirely on the last `harvest_events` call.

### `approve_event`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `index` | `int` | Yes | — | Position in the current cached `events` list (from the last `harvest_events`/`list_latest_events` result). |
| `dry_run` | `bool` | No | `False` | If `True`, records the approval fingerprint and returns a synthetic TickTick result without calling TickTick. |

**Returns (index out of range):** `{"ok": False, "error": "Event index N is out of range."}`.
**Returns (`dry_run=True`):** `{ok: True, dry_run: True, event, ticktick: {created: [title], updated: [], skipped: []}}`.
**Returns (TickTick not configured):** `{"ok": False, "error": "TickTick is not configured.", "event": ...}`.
**Returns (TickTick auth failure):** `{"ok": False, "error": "TickTick authentication failed.", "event": ...}`.
**Returns (normal path):** `{ok: True, event, ticktick: <result of create_ticktick_tasks>}`.

**Mutates state:** Yes — **`side-effecting`**, **`stateful`**. Sets `events[index]._status = "approved"` in the on-disk cache; saves an `"approved"` fingerprint (`event_match.save_fingerprint`) so future harvests recognize this event as known; on the non-dry-run path, creates/updates a real TickTick task via `outputs.ticktick.create_ticktick_tasks`.

### `decline_event`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `index` | `int` | Yes | — | Position in the current cached `events` list. |

**Returns:** `{"ok": False, "error": "..."}` if out of range, else `{ok: True, event}`.

**Mutates state:** Yes — **`side-effecting`**, **`stateful`**. Sets `events[index]._status = "declined"` in the cache and saves a `"declined"` fingerprint. No TickTick interaction.

### `generate_latest_report`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `output_path` | `str` | No | `"events_report.md"` | Path (relative to server CWD) to write the Markdown report. |

**Returns:** `{"ok": False, "error": "No cached events. Run harvest_events first."}` if the cache has no events, else `{ok: True, path, events: <count>}`.

**Mutates state:** Yes — **`side-effecting`**. Writes a Markdown file to disk; does not modify `data/.mcp_events.json`.

## Site-authoring tools — **added 2026-07-30**

The authoring half of web scraping. These are the only tools here that keep a browser open between calls; the replay half (`mode: "steps"`) never touches an LLM. Sessions are capped at 3 and reaped after 15 minutes idle.

| Tool | Parameters | Returns |
|---|---|---|
| `web_explore_open` | `url` (req), `native_chrome` | `session_id` + a first look: diagnosis, resolvable targets, visible event links |
| `web_explore_look` | `session_id` (req), `use_vlm` (`auto`\|`yes`\|`no`) | Diagnosis, which of the 5 target roles resolve (with selector + confidence), `event_links_visible`, `steps_so_far` |
| `web_explore_act` | `session_id`, `verb` (req); `target`, `text`, `url`, `what`, `seconds`, `ms`, `repeat`, `submit`, `optional` | Step result + a fresh look. **Records the step only if it succeeded** |
| `web_explore_undo` | `session_id` (req) | Drops the last recorded step. Does **not** rewind the page — stated rather than pretended |
| `web_explore_save` | `session_id`, `name` (req); `replace`, `category`, `scroll_seconds`, `expect_min_events`, `dry_run` | Crystallises into `data/web_sources.json`. Refuses to overwrite without `replace=true` |
| `web_explore_close` | `session_id` (req) | Closes the browser, returns the final route |
| `web_explore_list` | none | Open sessions; reaps idle ones as a side effect |
| `web_diagnose` | `url` (req), `use_vlm`, `native_chrome` | One-shot triage: open, diagnose, close. For "why is this source returning nothing" without committing to a session |
| `web_validate_steps` | `steps_json` (req) | Errors in a route, **without a browser**. Free and offline — use it on hand-edited `web_sources.json` entries |

`verb` vocabulary: `goto`, `dismiss`, `fill`, `click`, `scroll`, `wait`, `extract`.
`target` roles: `search`, `pagination`, `modal`, `feed`, `chat`, or `selector:<css>`.
Full normative spec: `05 - Standards/EH-STD-WEBSRC-v0 §3.2a`.

Typical authoring loop: `web_explore_open` → (`web_explore_look` → `web_explore_act`)* → `web_explore_save` → `web_explore_close`. Then `event-harvester web replay <name>` to confirm the saved route reproduces without the browser held open.

## Chat / Grok tools

These are registered by `register_grok_tools(server)` in `ext/universal_controller/uc_browser/mcp/grok_tools.py`, not defined in `mcp_server.py` itself. All of them (except `chat`, `list_chat_sessions`, `forget_chat_session`) drive a real Playwright browser session against grok.com through a process-singleton `GrokClient`; the first call in the process pays browser-startup cost, later calls reuse it. Every tool is wrapped so `GrokAuthRequired` and any other exception become `{"ok": False, "error": ..., "message": ...}` instead of a protocol error — none of them raise to the client.

### `chat`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `message` | `str` | Yes | — | Prompt text to send. |
| `session_id` | `str` | No | `"default"` | Continues the same Grok conversation across calls; a new id starts a fresh chat. Shared with the litellm `uc_handler` provider's session store. |
| `wait_for_response` | `bool` | No | `True` | `False` = fire-and-forget; returns as soon as the message is submitted (~1.6 s) with `response` empty. |
| `timeout_s` | `int` | No | `60` | Max seconds to wait for a reply when `wait_for_response=True`. |

**Returns:** `{ok, session_id, response, url, conversation_id}`. `response` has Grok's "Thought for Ns" prefix stripped.

**Mutates state:** Yes — **`side-effecting`**, **`stateful`**. Creates or continues a real grok.com conversation; updates the in-memory (not persisted) `session_id → url` map.

### `grok_send_message`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `message` | `str` | Yes | — | Prompt text. |
| `conversation_url` | `str \| None` | No | `None` | Existing conversation to post into; `None` starts a new one at grok.com. |
| `timeout_s` | `int` | No | `60` | Reply wait timeout. |

**Returns:** `{ok, response, url, conversation_id}`. **Mutates state:** Yes — **`side-effecting`**. Lower-level than `chat`: no session-id bookkeeping.

### `grok_read_conversation`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `conversation_url` | `str` | Yes | — | Conversation to read. |

**Returns:** `{ok, url, conversation_id, messages: [{role, text}, ...]}`. **Mutates state:** No.

### `grok_list_conversations`

No parameters. **Returns:** `{ok, conversations: [{id, title, url}, ...], count}`. **Mutates state:** No.

### `grok_delete_conversation`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `conversation_url` | `str` | Yes | — | Conversation URL or id to delete. |

**Returns:** `{ok, deleted}` (`deleted` reflects whether the sidebar row actually disappeared). **Mutates state:** Yes — **`side-effecting`**, permanently deletes the Grok conversation.

### `grok_new_chat`

No parameters. **Returns:** result of `get_grok_client().new_chat()` merged into `{ok: True, ...}` — the new-chat surface URL. **known rough edge:** Grok doesn't allocate a real conversation id until the first message is sent; follow with `grok_send_message` (no `conversation_url`) to materialize one. **Mutates state:** Marginal — opens a new-chat UI surface, no message sent yet.

### `grok_rename_conversation`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `conversation_url` | `str` | Yes | — | Conversation to rename. |
| `new_title` | `str` | Yes | — | New title. |

**Returns:** `{ok, renamed}`. **Mutates state:** Yes — **`side-effecting`**.

### `grok_archive_conversation`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `conversation_url` | `str` | Yes | — | Conversation to archive. |

**Returns:** `{ok, archived}` — `False` if Grok's UI doesn't expose an "Archive" menu item for this conversation (**known rough edge**, per the source docstring). **Mutates state:** Yes, when successful.

### `grok_regenerate`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `conversation_url` | `str` | Yes | — | Conversation whose last assistant reply to regenerate. |
| `timeout_s` | `int` | No | `60` | Reply wait timeout. |

**Returns:** `{ok, response}`. **Mutates state:** Yes — **`side-effecting`**, replaces the last assistant message in the real conversation.

### `grok_stop_generation`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `conversation_url` | `str` | Yes | — | Conversation to stop, if currently generating. |

**Returns:** `{ok, stopped}`. **Mutates state:** Yes, when a generation was in flight.

### `grok_get_models`

No parameters. **Returns:** `{ok, models: [...]}` — opens the model picker UI to read it. **Mutates state:** No (read-only UI interaction).

### `grok_switch_model`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `model_name` | `str` | Yes | — | Model to select in the picker. |

**Returns:** `{ok, switched}`. **Mutates state:** Yes — changes the active model for the browser session (affects subsequent `chat`/`grok_send_message` calls).

### `list_chat_sessions`

No parameters. **Returns:** `{ok, sessions: {session_id: url, ...}, count}` — snapshot of the in-memory session store shared with `chat`. **Mutates state:** No.

### `forget_chat_session`

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `session_id` | `str` | Yes | — | Session id to drop from the store. |

**Returns:** `{ok, forgotten, previous_url}`. Note: this does **not** delete the underlying Grok conversation — use `grok_delete_conversation` for that. **Mutates state:** Yes, but only the local in-memory map (process-lifetime only, not persisted to disk).

## Resources and prompts (not tools, but part of the same server)

| Kind | URI / name | Description |
|---|---|---|
| Resource | `event-harvester://config` | Live `config_summary` payload as JSON, recomputed on each read. |
| Resource | `event-harvester://latest-events` | Raw `data/.mcp_events.json` contents (internal event shape, including `_status` — richer than `list_latest_events`'s output). |
| Prompt | `review_latest_events` | Template instructing a client LLM to review `event-harvester://latest-events` and call `approve_event`/`decline_event`. |
| Prompt | `talk_to_grok` (param: `session_id`, default `"default"`) | Template inviting a client LLM to use the `chat` tool for a Grok-backed conversation. |

## Typical call sequence

```jsonc
// 1. Check what's configured before harvesting
{"tool": "config_summary", "arguments": {}}

// 2. Harvest and cache event candidates (indices below are only valid
//    until the NEXT harvest_events call)
{"tool": "harvest_events", "arguments": {"days": 14, "only": "discord,gmail"}}

// 3. Review the cached candidates (optionally re-filter by status)
{"tool": "list_latest_events", "arguments": {"status": "new"}}

// 4. Act on specific indices from step 2/3's response
{"tool": "approve_event", "arguments": {"index": 0}}
{"tool": "decline_event", "arguments": {"index": 1}}
```

## Client registration (`.mcp.json`)

```json
{
  "mcpServers": {
    "event-harvester": {
      "type": "stdio",
      "command": "pixi",
      "args": [
        "run",
        "event-harvester",
        "mcp"
      ],
      "env": {}
    }
  }
}
```

This registers the **stdio** transport (one subprocess per client). To point a client at a shared streamable-http instance instead — e.g. through the `gateway.py` aggregator — replace this with a `"url"` entry (see `gateway.py`'s own `config` dict for the shape: `{"event-harvester": {"url": "http://127.0.0.1:8765/mcp"}}`, or `http://127.0.0.1:9000/mcp` to go through the gateway).

## Verification

```powershell
pixi run python scripts/mcp_probe.py call config_summary
pixi run python scripts/mcp_probe.py call harvest_events '{"skip_analysis": true, "only": "discord"}'
pixi run python scripts/mcp_probe.py call list_latest_events '{"status":"all"}'
```

Each call round-trips through the real stdio server (handshake, `tools/call`, JSON-decoded result printed). The three-call sequence proves `harvest_events` writes a cache that `list_latest_events` can then read back, without needing live credentials for every source (`skip_analysis` + `only=discord` keeps the run cheap).
