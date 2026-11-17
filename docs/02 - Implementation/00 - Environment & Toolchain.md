# Environment & Toolchain

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** clean checkout → working `pixi` env on Windows (WSL notes inline)

## 1. Prerequisites

1. Install pixi (not in this repo — a host-level tool):
   ```powershell
   winget install prefix-dev.pixi
   ```
   `docs/04 - Reference/Pixi CLI Reference.md` has the full CLI surface; this doc only covers what this repo actually uses.
2. Install Google Chrome normally. The web-harvest path launches the **real Chrome binary** (`channel="chrome"` in Playwright) for `web login` and for `native_chrome` sources (Cloudflare-gated) — a bundled Chromium is not a substitute for those two paths, only for the standard (non-Cloudflare) `UCBrowser` sources.
3. **Windows-only:** `pyproject.toml` pins `[tool.pixi.workspace] platforms = ["win-64"]` — there is no `linux-64` platform in the lockfile. `pixi install` will not solve on native Linux. See `docs/02 - Implementation/02 - Deploy Playbook.md` §1 for the two options if you need this running on Linux (drop the Windows-only sources and add `linux-64`, or keep `discord`/`signal` on a separate Windows box).

## 2. Clone and initialize the submodule

```powershell
git clone --recurse-submodules <repo-url> event_tool
cd event_tool
git submodule update --init --recursive     # pulls ext/universal_controller
```

- `ext/universal_controller` is the Universal Controller (UC) browser-automation package — `.gitmodules` points it at `https://github.com/Ethycs/universal_controller.git`. It provides `UCBrowser`, the DOM-classification models under `ext/universal_controller/models/`, and its own `pyproject.toml`/`pixi.lock` for a standalone uc/grok-proxy environment (only needed if you run that proxy — see `docs/02 - Implementation/03 - UC Grok Proxy.md`).
- **unverified / repo-state gotcha:** on this checkout, `git submodule status` reports `ext/universal_controller` with a leading `-` (uninitialized) even though the directory is fully populated (including a built `extension/node_modules/`). Run `git submodule update --init --recursive` anyway on a fresh clone — don't trust `ls ext/universal_controller` alone as a sign the submodule is wired up.

## 3. Install the environment

```powershell
pixi install
```

- Installs the `default` environment (conda + PyPI) into `.pixi/envs/default/`.
- `event-harvester` itself is an editable PyPI dependency (`[tool.pixi.pypi-dependencies] event-harvester = { path = ".", editable = true }`), as is `universal_controller` (from `ext/universal_controller`) — no separate `pip install -e .` step.
- `torch` is pinned to the CUDA 12.6 wheel index (`[tool.pixi.pypi-options] extra-index-urls`) specifically for `sm_61` (Pascal) GPUs — see the comment block in `pyproject.toml` lines 56–74 before bumping this. On a machine without that GPU it still installs and falls back to CPU; it's just a larger download.
- The `dev` environment (`pytest`, `ruff`) is a separate pixi environment (`[tool.pixi.environments] dev = { features = ["dev"] }`), not part of `default`. `pixi install` alone does **not** provision it. `pixi run test` / `pixi run lint` / `pixi run format` auto-solve and install `dev` on first invocation because those tasks only exist in that environment — or install it explicitly:
  ```powershell
  pixi install -e dev
  ```

## 4. Playwright browser binaries

There is no pixi task for this — run the Playwright CLI directly inside the env:

```powershell
pixi run playwright install chromium
```

Needed for the web-harvest path (`sources/uc_browser.py`, `sources/web_session.py`) regardless of which web sources are enabled.

The web path also loads the Universal Controller Chrome extension from `ext/universal_controller/extension/`. Its bundle `dist/uc-extension.js` is gitignored, so a fresh checkout must build it once (Node ships in the pixi env):

```bash
pixi run bash -c 'cd ext/universal_controller/extension && npm ci && npx rollup -c'
```

Without it Chromium shows `Could not load javascript 'dist/uc-extension.js'` and the extension does not load (found 2026-11-17 on a fresh Linux setup).

## 5. First-run credentials scaffold

```powershell
Copy-Item .env.example .env
# then edit .env — see "01 - Credentials & First Run.md" for the per-service procedure
```

`.env` is gitignored (`.gitignore` line `.env`) — never commit it. Doc 2 covers every service's variables, first-run interaction, and where the resulting session/token files land.

## 6. Pixi tasks defined in this repo

Every task in `pyproject.toml`'s `[tool.pixi.tasks]` and `[tool.pixi.feature.dev.tasks]`:

| Task | Command | Environment | When you'd run it |
|---|---|---|---|
| `eh-service` | `event-harvester mcp --transport streamable-http --port 8765` | default | Start the persistent MCP backend that the gateway fronts (see `docs/02 - Implementation/02 - Deploy Playbook.md` §5 for the service-unit version) |
| `gateway` | `python gateway.py` | default | Start the central FastMCP gateway on `http://127.0.0.1:9000/mcp`, fronting `eh-service` (and optionally the UC/grok proxy) |
| `test` | `pytest -v` | dev | Run the full test suite (`pixi run test`) |
| `lint` | `ruff check src/ tests/` | dev | Lint check before committing |
| `format` | `ruff format src/ tests/` | dev | Auto-format before committing |

There is no pixi task named `web`, `watch`, `serve`, `mcp`, `harvest`, or `classifier` — those are **subcommands of the `event-harvester` console script**, not pixi tasks. Invoke them as `pixi run event-harvester <subcommand>` (see `README.md` "Three ways to use it" for the full subcommand list, and Doc 3 for the dev-loop-relevant ones).

## 7. Running anything in the environment

Always go through pixi — never invoke a bare `python`/`pip` from an activated shell that isn't pixi's:

```powershell
pixi run event-harvester --days 1 --dry-run       # the CLI entry point (project.scripts)
pixi run python scripts/mcp_probe.py               # ad-hoc scripts
pixi run python scripts/auth_telegram.py            # one-time Telegram auth (interactive — see Doc 2)
pixi run pytest tests/test_config.py -v             # equivalent to `pixi run test tests/test_config.py -v`
pixi shell                                           # or: drop into an activated shell; `exit` to leave
```

`pip` is available inside the pixi env for ad-hoc installs (`pixi run pip install ...`), but anything meant to persist belongs in `pyproject.toml` (`pixi add` / `pixi add --pypi`) so `pixi.lock` stays authoritative — don't hand-edit `pixi.lock` (200k+ lines; it's regenerated, not authored).

## 8. Windows vs WSL

- `sources/discord.py` (`find_discord_cache`) auto-detects two layouts: native Windows (`%APPDATA%/discord/Cache/Cache_Data`) and WSL (`Path("/mnt/c/Users").exists()` → scans `/mnt/c/Users/<you>/AppData/Roaming/discord/Cache/Cache_Data`). Override with `DISCORD_CACHE_PATH` if auto-detection picks the wrong user profile.
- **Windows-only sources:** `discord` (reads the Electron LevelDB cache directly, no bot token — see Doc 2) and `signal` (SQLCipher key extraction via DPAPI, Windows-specific in this codebase's current form) only work where the pixi env itself can run, i.e. **not** inside a WSL-only pixi install unless you're pointing at the Windows-side cache paths as above. `telegram`, `gmail`, and `web` are fully cross-platform.
- **WSL note:** running `pixi` itself inside WSL against this repo requires adding a `linux-64` platform to `[tool.pixi.workspace]` (not present today) — the simpler path on Windows is to run pixi natively in PowerShell and use the `/mnt/c` auto-detection above only for the Discord-cache edge case, not as a general WSL deployment strategy. Full cross-OS deployment tradeoffs are in `docs/02 - Implementation/02 - Deploy Playbook.md` §1.

## 9. Labelled data (DVC + lakeFS)

The classifier label sets — `data/labels.json`, `data/labels_all.json`, `data/labels_events.json` — and the Grok chat-history export `data/grok-export/` are not in git. Git holds only their `data/*.dvc` pointers (md5 + size, no content; `.gitignore` re-includes them past the blanket `data/*`). The data lives in lakeFS on central-dev, reached as S3, in the `event-harvester` repo on `main` (the label sets and the export share it; anyone holding that key can read the export's personal chats).

1. Install DVC as a host tool, outside the project env (it is not in `pyproject.toml`):
   ```bash
   pixi global install dvc --with dvc-s3     # exposes ~/.pixi/bin/dvc
   ln -s ~/.pixi/bin/dvc ~/.local/bin/dvc    # only if ~/.pixi/bin is not on PATH
   ```
2. Remote config is per-machine and holds the lakeFS key, so it goes in the gitignored `.dvc/config.local` (`.dvc/config` stays empty):
   ```bash
   dvc remote add -d --local lakefs s3://event-harvester/main/
   dvc remote modify --local lakefs endpointurl https://central-dev.lan:8000
   dvc remote modify --local lakefs ssl_verify /usr/local/share/ca-certificates/caddy-root.crt
   dvc remote modify --local lakefs access_key_id <lakeFS access key>
   dvc remote modify --local lakefs secret_access_key <lakeFS secret>
   ```
   `ssl_verify` points at central-dev's Caddy root CA; install that cert first.
3. Pull the data: `dvc pull`. After relabelling: `dvc add data/labels_all.json`, then `dvc push`, then commit the changed `.dvc` pointer.
4. Refresh the Grok export: run `scripts/export_grok_history.py` (writes to `~/grok-export`), then `rm -rf data/grok-export && cp -a ~/grok-export data/grok-export`, `dvc add data/grok-export`, `dvc push data/grok-export.dvc`, and commit the pointer. Name the target in `dvc push`: before its pointer is committed, a bare `dvc push` / `dvc status -c` skipped the new `.dvc` file and reported "up to date" (seen 2026-11-17).

Verify with `dvc status -c` → `Cache and remote 'lakefs' are in sync.` (Set up on smallblackbox on 2026-11-17: dvc 3.67.1 with dvc-s3 3.3.0.)

## Verification

```powershell
pixi run event-harvester --version
```

Success looks like:

```
event-harvester 0.1.0
```

For a deeper check that the env, editable installs, and submodule are all correctly wired:

```powershell
pixi run python scripts/mcp_probe.py
```

Success: it prints the MCP server's tool/resource list (`config_summary`, `harvest_events`, `list_latest_events`, `approve_event`, `decline_event`, `generate_latest_report`) without a stack trace — this exercises the full import graph (`event_harvester` editable install, `uc_browser` editable install from `ext/`, the MCP server module) end to end.
