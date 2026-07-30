# MCP Server

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/servers/mcp_server.py`, `src/event_harvester/servers/__init__.py`, `src/event_harvester/cli/commands/mcp.py`, `gateway.py`, `scripts/mcp_probe.py`, `.mcp.json`, `ext/universal_controller/uc_browser/mcp/grok_tools.py`

`event-harvester mcp` exposes the harvest/approve/decline workflow (and a set of browser-driven Grok chat tools) as a FastMCP server over stdio or streamable HTTP. Harvest results are cached to a JSON file on disk; `approve_event`/`decline_event` index into that same file by position, so the cache must not be regenerated between a `harvest_events` call and the approve/decline calls that follow it. A separate `gateway.py` at the repo root fronts this server (and any others) behind one proxied endpoint for editors that only want to register a single MCP URL.

## Domain

Two independent tool families share one FastMCP process (`build_server()` in `mcp_server.py:296`):

| Family | Registered in | Backs onto |
|---|---|---|
| Harvest/approve/decline, config, report | `mcp_server.py` directly (`@server.tool()` on the local `server` object) | `Pipeline`, the fingerprint store, `outputs/report.py` |
| `chat`, `list_chat_sessions`, `forget_chat_session`, `grok_*` (12 tools) | `ext/universal_controller/uc_browser/mcp/grok_tools.py:register_grok_tools(server)`, called from `mcp_server.py:518-520` | a process-singleton, browser-driven `GrokClient` (Playwright automation of grok.com) plus an in-memory session-id → conversation-URL store shared with the litellm `uc_handler` provider |

The Grok registration is wrapped in `try/except Exception` (`mcp_server.py:517-522`); if `uc_browser` isn't importable, the harvester tools still register and a warning is logged — the Grok tools silently disappear rather than crashing the server.

## Runs at

- **stdio** (default): `pixi run event-harvester mcp`. Spawned per-client as a subprocess (this is what `.mcp.json` and `scripts/mcp_probe.py` use). No network exposure; lifetime = the client session.
- **streamable-http**: `pixi run event-harvester mcp --transport streamable-http --port 8765` (or any `--host`/`--port`/`--path`). Runs as a long-lived service; `json_response=True` and `stateless_http=True` are set on the `FastMCP(...)` constructor (`mcp_server.py:327-340`), so each HTTP call is independent — there is no MCP-level session state held across requests (the on-disk cache is what actually carries state, see below).
- Transport is selected by `run_server_async(transport=...)` (`mcp_server.py:527-549`), which dispatches to `server.run_stdio_async()` or `server.run_streamable_http_async()`; any other value raises `ValueError`.
- `--allow-any-host` (CLI flag, `cli/parser.py:351-359`) disables FastMCP's DNS-rebinding `TransportSecuritySettings` check (`mcp_server.py:320-325`) — needed behind a reverse proxy/tunnel where the inbound `Host` header won't be loopback, otherwise FastMCP returns 421.

## Inputs

- `AppConfig` via `load_config()` — same config the CLI and Flet app use (env vars / config file). Loaded fresh on nearly every tool call (`config_summary`, `harvest_events`, `approve_event`, `decline_event` each call `load_config()` independently), so config edits take effect on the next call without a server restart.
- The on-disk event cache (below) for `list_latest_events`, `approve_event`, `decline_event`, `generate_latest_report`.
- Grok tools take a `session_id` (default `"default"`) that keys into the in-memory session store — **not** persisted to disk; it resets when the process restarts.

## Outputs

- Structured `dict[str, Any]` return values (FastMCP serializes these to the `tools/call` JSON-RPC result). No tool raises for expected failure paths — `approve_event`/`decline_event` return `{"ok": False, "error": ...}` for an out-of-range index; Grok tools use `_wrap_auth` (`grok_tools.py:34-52`) to turn `GrokAuthRequired`/any exception into `{"ok": False, "error": ..., "message": ...}` rather than propagating.
- Two resources (`event-harvester://config`, `event-harvester://latest-events`) return pretty-printed JSON strings.
- One prompt from the core server (`review_latest_events`) and one from the Grok module (`talk_to_grok`), both plain string templates for the calling client's LLM to consume — not something this server executes itself.

## Internal structure

### The on-disk event cache

- **Path:** `data/.mcp_events.json`, built from module-level constants `DATA_DIR = Path("data")` and `LATEST_EVENTS_FILE = DATA_DIR / ".mcp_events.json"` (`mcp_server.py:32-33`). **`unverified`/known rough edge:** this is a relative path resolved against the server process's current working directory, not the repo root or the package location. For stdio launches via `pixi run event-harvester mcp` (as in `.mcp.json`) the cwd is wherever the client spawns the subprocess from — normally the repo root, but nothing pins it. Launching from a different directory silently writes/reads a different cache file.
- **Format:** JSON object — `{generated_at, version, stats, source_counts, links, events}` (`_save_latest_events`, `mcp_server.py:133-150`). `events` is a list of the **internal** event dicts (see shape comparison below) plus a per-event `_status` key (`"new" | "approved" | "declined"`) once acted on.
- **Written by:** `run_event_pipeline()` (`mcp_server.py:197-293`), which is what `harvest_events` calls, on every run — including runs that find zero messages or that pass `skip_analysis=True` (written as an empty-events cache in that case). **`side-effecting`.**
- **Mutated in place by:** `_mark_cached_event(index, status)` (`mcp_server.py:153-160`), used by both `approve_event` and `decline_event`. It loads the whole cache, sets `events[index]["_status"] = status`, and rewrites the whole file. **`stateful`.**
- **Lifetime / invalidation:** the file persists across server restarts (it's just a file), but every `harvest_events` call **replaces `events` entirely** with a freshly re-indexed list. **Hard constraint:** `approve_event`/`decline_event`/`list_latest_events` reference events purely by list position (`index`), so:
  - Indices from one `harvest_events` response are only valid until the *next* `harvest_events` call.
  - `include_known=False` (the default) also changes which events land at which index — a run with `include_known=True` produces different index assignments than one without, even over identical source data.
  - Two concurrent `harvest_events` calls (e.g. two MCP clients against the same streamable-http server, which is `stateless_http=True`) race on this file with plain read/modify/write — no locking. **`BUG:` risk of lost updates / index drift under concurrent access; unverified how often this matters in practice given the tool is normally used interactively.**
- **What does NOT invalidate it:** approving/declining an event does not remove it from the cached list or renumber remaining entries — it only flips `_status` in place, so previously-returned indices for *other* events stay valid until the next harvest.

### `public_event` vs. the internal event dict

`_public_event(event, index)` (`mcp_server.py:107-120`) is the shape returned to MCP clients from `harvest_events`, `list_latest_events`, `approve_event`, and `decline_event`:

| Field | Source |
|---|---|
| `index` | position in the cached `events` list (see hard constraint above) |
| `status` | `_event_status(event)` — reads `event["_status"]` if present, else looks up the fingerprint store via `find_acted_fingerprint` (`event_match.py:267`) and falls back to `"new"` |
| `title`, `date`, `time`, `location`, `link`, `source`, `priority` | pass-through from the internal event dict |
| `notes` | `event.get("notes") or event.get("details")` — internal events may carry either key depending on which pipeline stage produced them |

The **internal** event dict (as produced by `Pipeline.run()` / `extract_events_llm`) is a superset: it also carries `_status` directly (written during the pipeline's fingerprint-match stage, `pipeline/orchestrator.py:206-207`) and whatever extraction fields aren't in the public shape (e.g. `details` as an alternate to `notes`). `_public_event` exists specifically to avoid leaking `_status`'s internal bookkeeping name and raw extraction noise to clients — clients only ever see the compact shape.

A third, report-specific shape exists: `_events_for_report()` (`mcp_server.py:85-104`), used only by `generate_latest_report`, which additionally splits `source` into `author`/`channel` (parsed from a `"@user in #channel"` string) and renames `priority`→`score` to match `outputs/report.py:generate_report`'s expected keys.

### `harvest_events` and `Pipeline`

`run_event_pipeline()` is a thin MCP-specific wrapper around the shared `Pipeline` class (`pipeline/orchestrator.py:77`), the same class the CLI `harvest` command and the Flet `serve` app use — per that module's docstring, this collapsed three previously-drifting ~100-line implementations into one. The wrapper's job (per `pipeline/orchestrator.py:8-11`) is purely what's specific to MCP:

1. `load_config()`, then apply per-call overrides (`days`, `no_cooldown`, `skip_analysis`, `no_cap`, `total_cap`) onto the loaded `AppConfig`.
2. Resolve `only`/`skip` source filters to `no_*` kwargs via `_source_kwargs` (raises `ValueError` if both are set, or if a source name isn't in `VALID_PLATFORMS`).
3. `pipeline.validate(no_kwargs=..., need_ticktick=False)` → warning strings (not errors — TickTick readiness isn't required to harvest, only to approve).
4. `pipeline.run(...)` → a `PipelineResult` (`all_messages`, `actionable`, `events`, `summary`, `rejects`, `source_counts`, `links`, `skipped_analysis`, `no_messages`).
5. Apply the `include_known` filter (drop non-`"new"` events unless the caller asked to include them).
6. `_save_latest_events(...)` — write the cache — then return `{ok, warnings, summary, stats, events}` with `events` mapped through `_public_event`.

`unlimited_caps` is set automatically whenever `only` is given, on the theory that a caller who explicitly scoped to specific sources shouldn't have that scope silently trimmed by the *other* sources' per-source caps.

### Resources

| URI | Returns | Notes |
|---|---|---|
| `event-harvester://config` | `json.dumps(_summarize_config(load_config()), indent=2)` | Same payload as the `config_summary` tool, exposed as a readable resource too. Recomputed on every read — always current, not cached. |
| `event-harvester://latest-events` | `json.dumps(_load_latest_events(), indent=2)` | The raw on-disk cache file, verbatim (internal event shape, including `_status`) — **not** run through `_public_event`. Clients reading this resource see more than clients calling `list_latest_events`. |

## Gateway (`gateway.py`, repo root)

`gateway.py` is a **single-endpoint aggregator**, not part of the event-harvester package itself. It uses FastMCP 3.x `create_proxy()` to front one or more backend MCP servers — currently just `event-harvester`, reached over its **streamable-http** URL (`EH_MCP_URL`, default `http://127.0.0.1:8765/mcp`) rather than stdio — behind one gateway endpoint (`http://127.0.0.1:9000/mcp` by default, `GATEWAY_HOST`/`GATEWAY_PORT` overrides). Per its module docstring, the intent is: any number of editors register the gateway's one URL instead of each backend separately, and the heavy harvester process stays warm as a persistent service shared across sessions instead of a fresh stdio subprocess per client. Additional backends (declared in the standard `mcpServers` config shape — the commented-out `github`/`files`/`remote` examples in the file) would be namespaced by their config key (e.g. `event-harvester_harvest_events`) once added; today only one backend is wired up. Running the gateway requires the harvester to already be running in streamable-http mode as a separate process — the gateway does not start it.

## Hard constraints

- Index-based tools (`approve_event`, `decline_event`, `list_latest_events`) are positional against `data/.mcp_events.json`'s `events` array. **The cache must not be regenerated (another `harvest_events` call) between reading an index and acting on it.**
- The cache path is CWD-relative (`data/.mcp_events.json`), not anchored to the repo or package root — see Internal structure above.
- `stateless_http=True` on the streamable-http transport means no MCP session state is held server-side beyond the process's Python globals (the Grok session-id map) and the on-disk cache — concurrent clients share both, unguarded by any lock.
- `approve_event` with `dry_run=False` requires `cfg.ticktick.is_configured`; without it the tool returns `{"ok": False, "error": "TickTick is not configured.", ...}` rather than raising.
- The Grok tool family requires `uc_browser` to import successfully and (at call time) a working Playwright-driven browser session against grok.com; auth failures surface as `{"ok": False, "error": "auth_required", ...}` via `_wrap_auth`, not exceptions.

## Failure modes

| Condition | Behavior |
|---|---|
| `mcp` SDK not installed | `build_server()` raises `RuntimeError` with an install hint (`mcp_server.py:311-318`) — this happens at server startup, not per-tool-call. |
| Unsupported `--transport` value reaches `run_server_async` | Raises `ValueError("Unsupported MCP transport: ...")`. (CLI argparse already restricts `--transport` to `{stdio, streamable-http}`, so this path is effectively unreachable via the CLI — `unverified` whether any other caller can hit it.) |
| `approve_event`/`decline_event` given an out-of-range `index` | Caught `IndexError` from `_mark_cached_event`, returned as `{"ok": False, "error": "Event index N is out of range."}` — not a protocol-level error. |
| `only` and `skip` both passed to `harvest_events` | `ValueError` inside `_source_kwargs`, propagates up through `run_event_pipeline` — **`unverified`** whether FastMCP surfaces this to the client as a tool error or as a protocol-level exception (not exercised in `tests/test_mcp_server.py`, which only tests the raise itself, not the MCP-wrapped behavior). |
| Unknown source name in `only`/`skip` | Same `ValueError` path, message lists valid sources from `VALID_PLATFORMS`. |
| `generate_latest_report` called with an empty cache | Returns `{"ok": False, "error": "No cached events. Run harvest_events first."}` rather than writing an empty report. |
| `uc_browser` import fails at server build time | Grok tools are skipped entirely (logged warning), core tools still register — **not** a startup failure. |
| Grok `GrokAuthRequired` or any other exception inside a Grok tool | Converted to `{"ok": False, "error": ..., "message": ...}` by `_wrap_auth`; the browser session is not automatically retried or re-authenticated. |

## Verification

```powershell
pixi run python scripts/mcp_probe.py
pixi run python scripts/mcp_probe.py call config_summary
```

Spawns the stdio server via `pixi run event-harvester mcp`, performs the MCP `initialize` handshake, lists all registered tools/resources (proves the Grok tool family and the two resources are actually wired into the same server object), then calls `config_summary` and prints its JSON result (proves the tool call round-trip works without touching `data/.mcp_events.json`). Cross-check the printed tool count/names against this document's table and `README.md`'s (shorter) list.
