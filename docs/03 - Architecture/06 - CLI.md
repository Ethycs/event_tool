# CLI

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/cli/dispatch.py`, `src/event_harvester/cli/parser.py`, `src/event_harvester/cli/parse_helpers.py`, `src/event_harvester/cli/__main__.py`, `src/event_harvester/cli/__init__.py`, `src/event_harvester/cli/commands/*.py`, `src/event_harvester/watch.py`, `src/event_harvester/__main__.py`, `pyproject.toml`

The CLI is a thin argparse dispatcher over seven async command handlers; only `harvest`, `serve`, and the MCP `harvest_events` tool actually run the shared `Pipeline` class, while `watch`, `web`, `recruiters`, and `classifier` either own their I/O outright or call `pipeline/harvest.py:harvest_messages` directly. A bare invocation is silently rewritten to `harvest ...` before argparse ever sees it — the only pre-parse transformation the CLI performs. There is no generic "namespace → `AppConfig`" mapping layer: `AppConfig` is built once from environment variables in `dispatch.main()`, and each command module hand-writes its own subset of `args.X → cfg.Y` assignments.

## Hard constraints

1. `--only` and `--skip` are mutually exclusive wherever both appear (`parser.py:_add_platforms_filter`, an `argparse` mutex group); `-v`/`-q` are mutually exclusive everywhere (`parser.py:_add_root_flags`).
2. Bare invocation (`event-harvester` with no args, or with leading flags and no recognized subcommand token) defaults to `harvest` — implemented in `dispatch.py:_normalize_argv`, which runs **before** `build_parser().parse_args()`.
3. `classifier eval --labels FILE` has no default; omitting `--labels` is a `SystemExit` from argparse, not a runtime error.
4. `web`, `recruiters`, and `classifier` each require a second-level subcommand (`web_command` / `recruiters_command` / `classifier_command`, all `required=True`); omitting one is `SystemExit`, not a fallback.
5. Only 3 of 7 top-level commands ever construct a `Pipeline`: `harvest` (`commands/harvest.py`), `serve` (`commands/serve.py`), and the MCP `harvest_events` tool (`servers/mcp_server.py:233`). `recruiters grade` and `classifier train` call `harvest_messages` directly, skipping `Pipeline`'s filter → extract → match sequence and its rejects/fingerprint bookkeeping. `known rough edge`: `pipeline/__init__.py`'s docstring claims Pipeline is "the single source of truth" for three entry points, but two more CLI subcommands still duplicate the harvest call outside it.

## Entry points

| Entry point | Resolves to | Notes |
|---|---|---|
| `event-harvester` console script (`pyproject.toml:32`, `[project.scripts]`) | `event_harvester.cli:main_sync` (`cli/__init__.py:8`) | What `pixi run event-harvester ...` and the `eh-service` pixi task (`pyproject.toml:87`) invoke. |
| `python -m event_harvester` | `src/event_harvester/__main__.py:4` → `event_harvester.cli.main_sync()` | `BUG:` called unconditionally at module scope — **not** guarded by `if __name__ == "__main__":`. Any code path that does `import event_harvester.__main__` (as opposed to running it via `-m`) launches the full CLI as an import side effect. |
| `python -m event_harvester.cli` | `cli/__main__.py:6` → `main_sync()` under `if __name__ == "__main__":` | Standard guarded form; the safe one of the two `-m`-style entries. |
| pixi task `eh-service` (`pyproject.toml:87`) | `event-harvester mcp --transport streamable-http --port 8765` | Fixed-args wrapper; the gateway's backend. |

## Dispatch layer — `cli/dispatch.py`

- **Domain:** argv normalization, logging setup, OpenTelemetry bootstrap, config load, command routing, top-level exception handling and exit codes.
- **Runs at:** process start, before any command-specific logic; owns `main()` / `main_sync()`.
- **Inputs:** `sys.argv[1:]`; environment (indirectly, via `config.load_config()` and `OTEL_EXPORTER_OTLP_ENDPOINT` read directly at `dispatch.py:107`).
- **Outputs:** process exit code (0 success, 1 user error, 2 system error, 130 on `KeyboardInterrupt`); stderr logging stream; stdout/stderr reconfigured to UTF-8.
- **Internal structure:**
  - `_normalize_argv` (`dispatch.py:44-63`): empty argv → `["harvest"]`; `argv[0]` a known command or `-h`/`--help`/`--version` → unchanged; anything else → `["harvest"] + argv`. This is the *only* argv rewrite anywhere in the CLI.
  - `build_parser()` is called fresh on every invocation — no caching, no module-level parser singleton.
  - `_setup_logging` (`dispatch.py:66-87`) reconfigures `sys.stdout`/`sys.stderr` to `encoding="utf-8", errors="replace"` (a Windows cp1252-terminal workaround) then calls `logging.basicConfig` with a single `StreamHandler(sys.stderr)` and a fixed `"  %(message)s"` format — no timestamps, no logger names, regardless of verbosity.
  - OTel init (`dispatch.py:98-110`) is wrapped in its own try/except; a failure here only logs a warning via `logger.warning`, never aborts the run.
  - `cfg = load_config()` (`dispatch.py:112`) builds `AppConfig` from environment variables **only**, before any argparse value is consulted. Each command handler then mutates `cfg` in place from `args` — see each command's own section below for exactly which fields it touches. `undocumented`: adding a CLI flag that should affect `cfg` means editing the relevant command module; there is no shared translation table.
  - `_HANDLERS` (`dispatch.py:33-41`) maps `args.command` → async handler; `KNOWN_COMMANDS` (`dispatch.py:29-31`, used only by `_normalize_argv`) must be kept in sync by hand with both `_HANDLERS` and the subparser names registered in `parser.py`. `known rough edge`: no single source of truth ties the three lists together.
  - Top-level try/except in `main()` (`dispatch.py:119-126`): `KeyboardInterrupt` → prints `"\nStopped."`, returns 130; any other `Exception` → `logger.exception(...)`, returns 2. Handlers are otherwise trusted to return their own exit code; `handler(args, cfg) or 0` means a handler returning `0` and one returning `None` are indistinguishable — `unverified` whether any handler relies on returning a falsy-but-meaningful code that this would silently coerce to 0.
  - `main_sync()` (`dispatch.py:129-139`) wraps `asyncio.run(main())`, catches `KeyboardInterrupt` a second time, and always calls `event_harvester.llm.shutdown_local()` in a `finally` block before `sys.exit(exit_code)`.
- **Hard constraints:** #2, #5 above.
- **Failure modes:** argparse errors (bad flag value, missing required subcommand) raise `SystemExit` **before** `main()`'s try/except is reachable — argparse prints its own usage text and exits 2 directly, bypassing the logging setup entirely (since `_setup_logging` runs after `parser.parse_args()`). `KeyboardInterrupt` mid-run exits 130. Any other exception during a handler is caught, logged with a full traceback, and exits 2; output the handler already printed to stdout is not rolled back.

### Argv normalization examples

| Input argv | Normalized to | Why |
|---|---|---|
| `[]` | `["harvest"]` | bare invocation |
| `["--days", "14"]` | `["harvest", "--days", "14"]` | flags-only, no known subcommand token |
| `["harvest", "--days", "14"]` | unchanged | already explicit |
| `["watch"]` | unchanged | known command |
| `["-h"]`, `["--help"]`, `["--version"]` | unchanged | passed straight to the top-level parser |
| `["web", "list"]` | unchanged | known command |

## Parser layer — `cli/parser.py` + `cli/parse_helpers.py`

- **Domain:** declarative argparse tree — 1 top-level parser, 7 subparsers, 3 of which (`web`, `recruiters`, `classifier`) have their own required nested subparsers.
- **Runs at:** once per invocation, inside `dispatch.main()`, immediately after `_normalize_argv`.
- **Inputs:** the normalized argv list.
- **Outputs:** an `argparse.Namespace` with a `command` attribute (plus `web_command` / `recruiters_command` / `classifier_command` for the three nested subcommands).
- **Internal structure:**
  - Shared flag builders factor out repeated flags: `_add_days_back`, `_add_platforms_filter`, `_add_profile_flag`, `_add_debug_flags`, `_add_root_flags`. Each is called directly on every subparser that needs it (not via argparse `parents=`) specifically to avoid double registration, per the module docstring (`parser.py:14-16`).
  - `_add_root_flags` (verbose/quiet mutex) is registered on **both** the top-level parser and `p_harvest` (`parser.py:173, 186`) — every other subparser gets it exactly once (confirmed present on `watch`, `web` × 4, `recruiters` × 2, `classifier` × 2, `serve`, `mcp`). `undocumented`: the double registration on `harvest` specifically is unexplained and untested by `tests/test_cli_parser.py`; it has no observable effect since both registrations write the same `verbose`/`quiet` destinations.
  - `parse_helpers.py` supplies two `type=` functions argparse calls directly (`parse_cap_arg`, `parse_platform_csv`) plus two post-parse helpers command modules call manually (`resolve_platforms`, `apply_caps_to_config`). Validation lives inside the `type=` functions, so bad input raises `argparse.ArgumentTypeError` → argparse prints a usage error and exits 2 **before any command module code runs**.
  - `VALID_PLATFORMS = {discord, telegram, gmail, signal, web}` and `VALID_CAP_KEYS = VALID_PLATFORMS | {total}` (`parse_helpers.py:11-12`) are the sole source of truth for `--only`/`--skip` and `--cap` key validation.
  - `resolve_platforms(only, skip) -> {"no_discord": bool, ...}` (`parse_helpers.py:77-89`): `only` given → only those platforms active; `skip` given → everything except those; neither → all active. This dict is what `harvest`/`watch`/`classifier train` splat into `harvest_messages(**...)`, and what `serve` translates into `cfg.sources.*` booleans.
  - `BUG:` `parse_cap_arg` rejects negative values (`raise ArgumentTypeError` when `n < 0`, `parse_helpers.py:51-54`), but `CapConfig.no_cap` and the cap-bypass check in `pipeline/analysis.py:290-291` (`caps.total < 0`) both treat a negative cap as a legitimate "unlimited" sentinel, and `README.md:201` documents `--cap web=-1` as the way to get unlimited web results. That exact invocation raises `SystemExit` at parse time on the CLI — the `-1` sentinel is reachable only via env vars (e.g. `CAP_WEB=-1`) or `data/profiles.json`, never via `--cap`. Full drift entry in the Reference doc.
- **Hard constraints:** #1, #3, #4 above.
- **Failure modes:** `ArgumentTypeError` / `SystemExit(2)` from argparse; no validation beyond what each `type=` function performs.

## Command modules

| Subcommand | Purpose | Entry point | Uses `Pipeline`? | Owns its own I/O |
|---|---|---|---|---|
| `harvest` | harvest → filter → extract → match, print results, sync TickTick, write reports | `commands/harvest.py:harvest_cmd` | Yes | Terminal printing, `data/rejects.ini` writer, Markdown/Obsidian reports, TickTick sync |
| `watch` | Poll Discord + Telegram forever, print new messages live | `commands/watch.py:watch_cmd` → `watch.py:watch_mode` | No | Entirely — its own poll loop and `seen` set; no LLM, TickTick, or `Pipeline` involvement |
| `web {list,add,test,login}` | Manage `data/web_sources.json`; Playwright diagnostics | `commands/web.py:web_cmd` | No | Entirely — all four subcommands ignore the `cfg` argument completely |
| `recruiters {grade,reparse}` | Grade Gmail recruiter emails; act on a saved report | `commands/recruiters.py:recruiters_cmd` | No — `grade` calls `harvest_messages` directly | Gmail body fetch, LLM grading, auto-trash, Obsidian report |
| `classifier {train,eval}` | Train/evaluate the sklearn actionable-vs-noise classifier | `commands/classifier.py:classifier_cmd` | No — `train` calls `harvest_messages` directly when not loading labels | Label file I/O, training/eval artifacts |
| `serve` | Launch the Flet review UI; runs `Pipeline` as a background task per click | `commands/serve.py:serve_cmd` | Yes | Flet app lifecycle (`ft.app_async`, blocks on port 8550), `NavQualityScorer` quality-check stage |
| `mcp` | Run the MCP server (stdio or streamable-http) | `commands/mcp.py:mcp_cmd` | Indirectly — `harvest_events` tool builds its own `Pipeline` in `servers/mcp_server.py:233` | Entire MCP protocol surface lives in `servers/mcp_server.py`, not the CLI layer |

### `harvest` — `commands/harvest.py`

- **Domain:** the primary one-shot pipeline run: harvest, filter, LLM-extract events, print, report, sync TickTick.
- **Runs at:** `harvest_cmd(args, cfg)`, invoked by `_HANDLERS["harvest"]`.
- **Inputs:** all `harvest`-scoped flags (see Reference doc for the full table); `cfg` as built by `load_config()`.
- **Outputs:** stdout (grouped event listing + TickTick sync summary), optional `--report FILE` Markdown, optional Obsidian vault write, optional `data/rejects.ini`, TickTick tasks (unless `--no-sync`/`--dry-run`).
- **Internal structure:** mutates `cfg` from args in this order — `cfg.days_back` ← `args.days`; profile applied (`profiles.apply_profile_by_name`) **before** `--cap`/`--web-source` so explicit flags win; `apply_caps_to_config`; `cfg.caps.group_by_source`; `cfg.web.debug_trace`/`debug_screenshots`; `cfg.skip_analyze` ← `args.no_analyze`. Then builds `Pipeline(cfg)`, calls `pipeline.validate(...)`, then `pipeline.run(...)`. `known rough edge`: `args.dry_run` is threaded straight into `create_ticktick_tasks(dry_run=args.dry_run)` and into the `need_ticktick` validation check, but it is **never** assigned to `cfg.dry_run` — that field exists (`config.py:213`, default from `DRY_RUN` env var) and is read by the Flet UI (`ui/app.py:246,274,283,930,1304`), so a CLI `--dry-run` run and `cfg.dry_run` can silently disagree if anything else in the call graph starts consulting `cfg.dry_run` instead of a threaded parameter.
- **Hard constraints:** `--only` implies `unlimited_caps=True` in `pipeline.run(...)` (`harvest.py:83`) — per-source caps are skipped entirely when `--only` narrows the source set, logged via `"--only set; ignoring per-source caps"`.
- **Failure modes:** `pipeline.validate()` warnings are logged, not fatal; `get_ticktick_client(cfg.ticktick)` returning `None` (missing credentials) returns exit code 1 from `harvest_cmd` after messages/events have already been printed.

### `watch` — `commands/watch.py` + `watch.py`

- **Domain:** continuous polling loop over the Discord LevelDB cache and Telegram MTProto, printing new messages as they appear. No LLM extraction, no TickTick, no `Pipeline`.
- **Runs at:** `watch_cmd` resolves platform flags then calls `watch_mode(cfg, interval, no_telegram, no_discord)`, which runs until interrupted.
- **Inputs:** `--interval` (default 30s), `--only`/`--skip`, `--profile`, the three `--debug-*` flags (accepted but their only effect is setting `cfg.web.debug_trace`/`debug_screenshots`, which watch mode never reads since it never touches the web source).
- **Outputs:** live `print_message(msg)` lines to stdout; no files, no return value beyond exit code 0 (reached only via `KeyboardInterrupt`).
- **Internal structure — the daemon loop:**
  - `_MAX_SEEN_IDS = 50_000` (`watch.py:19`), module-level memory cap.
  - **State kept between iterations:** a single `seen: set[str]` of message IDs, created once per `watch_mode()` call and never persisted to disk. Restarting the process resets it entirely.
  - **Seed phase** (`watch.py:42-52`, runs once before the loop): reads messages from the last 1 hour (`seed_cutoff = now - 1h`) from Discord cache and Telegram, adding their IDs to `seen` **without printing them** — a one-time "don't spam recent history on startup" step.
  - **Loop body** (`watch.py:54-80`), `while True`:
    1. `await asyncio.sleep(interval)` runs **first** — the first poll fires `interval` seconds after the seed phase, not immediately.
    2. `cutoff = now - 24h` is recomputed every iteration — much wider than the 1h seed window. `unverified` why 24h specifically; new-message detection relies entirely on `seen` membership, not on the cutoff narrowing to "since last poll", so this just re-scans a fixed 24h window each time.
    3. Reads Discord/Telegram messages newer than `cutoff`; any ID not in `seen` is added to both `seen` and a `new` list.
    4. Prints `new`, sorted by timestamp.
    5. Trims `seen` if it exceeds `_MAX_SEEN_IDS`: `to_remove = list(seen)[:excess]`. `known rough edge`: the surrounding comment says "trim stale IDs", but a Python `set` has no defined iteration order — `list(seen)[:excess]` does not remove the oldest entries, it removes an arbitrary subset. At sustained volume above 50k distinct IDs, this can evict a recently-seen ID before an old one, allowing a duplicate reprint.
  - **Telegram connection:** one persistent client for the whole session (`create_persistent_client`, not reopened per poll); disconnected in a `finally` block.
  - **Shutdown:** no explicit signal handling in `watch.py`. The only exit path is `KeyboardInterrupt` propagating out of `asyncio.sleep`, through `watch_mode`'s `finally` (Telegram disconnect happens first), then caught by `dispatch.main()` which prints `"\nStopped."` and returns 130.
  - `known rough edge`: the "unsupported platform" warning (`commands/watch.py:29-37`) only fires when `args.only is not None`. Using the default (no filter) or `--skip <something>` never warns, even though `watch_mode` silently ignores gmail/signal/web regardless — e.g. `watch --skip web` leaves `no_gmail`/`no_signal` both `False` in `platform_kwargs`, but neither is ever consulted by `watch_mode`, and no warning is printed because `args.only` is `None`.
- **Hard constraints:** `--interval` has no min/max validation — any int (including 0 or negative) is accepted by `type=int`; `unverified` what `asyncio.sleep()` does with a negative value beyond "treated as 0" per asyncio's documented behavior.
- **Failure modes:** an exception from `read_discord_messages`/`read_telegram_messages` is not caught inside the loop — it propagates out of `watch_mode` entirely (ending the whole watch session, not just that poll), through the `finally` disconnect, into `dispatch.main()`'s generic handler (exit 2).

### `web {list,add,test,login}` — `commands/web.py`

- **Domain:** inspect and manage `data/web_sources.json`; run Playwright-based diagnostics to recommend a fetch `mode` for a URL.
- **Runs at:** `web_cmd` dispatches on `args.web_command`.
- **Inputs:** `list` — none; `add`/`test` — positional `url`, `add` also takes `--mode {calendar,feed,search,raw}`; `login` — none.
- **Outputs:** `list` prints configured sources; `test` prints diagnostics only; `add` prints diagnostics **and** appends to `data/web_sources.json` via `utils.save_json`; `login` opens a browser and saves session state (`sources/web_session.py:web_login`).
- **Internal structure:** `_is_ics_url`/`_diagnose_ics` short-circuit iCalendar URLs (`webcal://`, `webcals://`, or a trailing `.ics`) to a direct fetch-and-parse path that never opens a browser — added specifically because Playwright cannot navigate `webcal://` (`Page.goto: net::ERR_ABORTED`, per the docstring at `web.py:96-107`). Non-ICS URLs go through `_run_url_diagnostics`, which opens a `UCBrowser` inside a single-worker `ThreadPoolExecutor` (Playwright's sync API needs its own thread inside the async CLI), scores calendar/search/feed signals, and recommends a mode using a fixed decision order: ≥3 event links → `calendar`; else a search bar detected → `search`; else ≥3 feed items → `feed`; else `raw`.
- **Hard constraints:** none beyond argparse's positional `url` requirement.
- **Failure modes:** a Cloudflare "just a moment" challenge is detected and reported (`"BLOCKED"`) rather than raised; `_web_add` silently no-ops (prints "Already configured") if the URL is already present, rather than erroring.

### `recruiters {grade,reparse}` — `commands/recruiters.py`

- **Domain:** grade Gmail recruiter emails by quality (LLM-assisted) and optionally act on a saved report.
- **Runs at:** `recruiters_cmd` dispatches on `args.recruiters_command`.
- **Inputs:** `grade` — `--days`, `--auto-trash`, `--obsidian`, `--no-analyze`; `reparse` — positional `file`.
- **Outputs:** `grade` prints graded emails (`print_recruiter_grades`), optionally trashes low scorers via Gmail API, optionally writes an Obsidian report; `reparse` runs an interactive open/reply/trash loop over a saved report (`outputs/obsidian.py:reparse_recruiter_report`).
- **Internal structure:** `_recruiters_grade` calls `pipeline/harvest.py:harvest_messages` **directly** (not `Pipeline`) with every source disabled except Gmail (`no_discord=True, no_telegram=True, no_gmail=False, no_signal=True, no_web=True`), then hands off to `_run_recruiter_grading` — the same helper `harvest.py:harvest_cmd` calls when `--grade-recruiters` is passed, so the grading logic itself is shared even though the harvest call is not.
- **Hard constraints:** none beyond the nested-subcommand requirement.
- **Failure modes:** `_recruiters_grade` returns 0 with a "No Gmail messages found" print if the Gmail fetch is empty; `validate_config` warnings (missing Gmail creds, missing LLM) are logged, not fatal.

### `classifier {train,eval}` — `commands/classifier.py`

- **Domain:** train and evaluate the sklearn actionable-vs-noise classifier used ahead of LLM extraction.
- **Runs at:** `classifier_cmd` dispatches on `args.classifier_command`.
- **Inputs:** `train` — `--in-labels`, `--out-labels`, `--days`, `--only`, `--skip`; `eval` — `--labels` (required), `--out-samples`.
- **Outputs:** `train` writes `--out-labels` JSON (if given) and trains/persists the classifier via `pipeline/classifier.py:train`; `eval` runs `training/eval_classifier.py:run_eval` and optionally writes `--out-samples` review artifacts.
- **Internal structure:** `train` either loads a pre-labeled JSON file (`--in-labels`) or calls `harvest_messages` directly (bypassing `Pipeline`) and LLM-labels the result via `training/label.py:label_messages`.
- **Hard constraints:** `eval` requires `--labels` (argparse `required=True`).
- **Failure modes:** a bad `--in-labels` path/JSON returns exit code 1 with a logged error, without attempting to harvest as a fallback.

### `serve` — `commands/serve.py`

- **Domain:** launch the Flet review UI; the pipeline runs as a background task inside the Flet event loop per user-triggered run.
- **Runs at:** `serve_cmd` seeds `cfg` from CLI args once, then blocks in `ft.app_async(target=app.main, view=ft.AppView.WEB_BROWSER, port=8550)` — subsequent runs are driven by UI interaction, not new CLI invocations.
- **Inputs:** `--days`, `--only`/`--skip`, `--profile`, the three `--debug-*` flags.
- **Outputs:** a running web UI at `http://127.0.0.1:8550`; no CLI-side stdout beyond Flet's own startup log.
- **Internal structure:** `run_pipeline(reporter, app)` (defined inline in `serve_cmd`) wraps `Pipeline(cfg, reporter=reporter).run(...)`; after `Pipeline` finishes, an extra `quality_check` stage runs **only** when `cfg.web.debug_screenshots` is true **and** an LLM/vision model is configured (`serve.py:109-113`) — this stage is serve-specific and intentionally not part of `Pipeline` itself, per the module docstring.
- **Hard constraints:** none beyond the shared harvest-arg constraints.
- **Failure modes:** quality-check scoring failures for an individual source are caught and logged (`logger.warning`) without failing the pipeline run (`serve.py:131-135`).

### `mcp` — `commands/mcp.py`

- **Domain:** run the MCP server exposing Event Harvester tools/resources to MCP clients (stdio or streamable-http).
- **Runs at:** `mcp_cmd` awaits `servers/mcp_server.py:run_server_async(...)` until the client disconnects or the process stops.
- **Inputs:** `--transport {stdio,streamable-http}` (default `stdio`), `--host`, `--port`, `--path`, `--allow-any-host`.
- **Outputs:** the running MCP server; log level derived from `-v`/`-q` (`DEBUG`/`WARNING`/`INFO`).
- **Internal structure:** the CLI layer is a pure pass-through — all tool/resource logic (including its own `Pipeline` construction for `harvest_events`) lives in `servers/mcp_server.py`, outside the scope of this doc.
- **Hard constraints:** none beyond the `--transport` choice restriction.
- **Failure modes:** not covered here — see the MCP server's own documentation, if any exists.

## Verification

```powershell
pixi run pytest tests/test_cli_parser.py -v
```

This exercises `build_parser()` routing for all 7 subcommands (and 3 nested ones), the `--cap`/`--only`/`--skip` `type=` validators, `resolve_platforms`/`apply_caps_to_config`, and `dispatch._normalize_argv` — confirming the parser tree and argv-normalization claims above match the actual code (it does not exercise `watch_mode`'s loop body, `web`'s Playwright diagnostics, or any command handler's `cfg` mutation, none of which are unit-tested as of this writing).
