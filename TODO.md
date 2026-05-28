# TODO

## Open

- Tighter web targeting per source (per-site selectors, search-bar hints) — partial via UC; still mode-dependent.
- Auto-remediation from VLM verdicts (Cloudflare → native_chrome retry, login_wall → trigger `web login`).
- Multi-image vision call (batch all source frames into one VLM prompt to cut cost).
- Cache VLM responses keyed by screenshot hash.
- Cleanup policy for old `data/.web_traces/*` run directories.
- Historical quality trend ("luma at 90 for 7 days → 30 today").

## Done (recent)

- MCP server with TickTick approve/decline tools (`event-harvester mcp`).
- Per-URL navigation trace (JSONL, `--debug-web-trace`).
- Screenshot capture per nav step + scroll-feed GIF (`--debug-web-screenshots`).
- VLM quality scoring (`quality_check` pipeline stage + Flet badges).
- OpenTelemetry tracing for harvest stages (`--debug-otel`).
- Async source fan-out (Discord/Telegram/Gmail/Signal/Web concurrent).
- Parallel web fetch (N UCBrowser instances).
- Named profiles (`data/profiles.json` + `--profile`).
- Bounded screenshot timeouts + per-session cap + circuit breaker.
- Per-source `scroll_seconds` across all web modes.
