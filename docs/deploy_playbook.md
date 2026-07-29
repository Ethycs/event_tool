# event_tool — Redeploy Playbook

A reproducible procedure for standing up (or re-standing-up) **event_harvester**
on a server, with **all secrets sourced from Infisical** and **browser session
state provisioned as files**. Written so an operator/agent on `CENTRAL-DEV` can
execute it end-to-end.

> **Scope of this run**
> - **Cookie/session host:** `jobhelp@SMALLBLACKBOX` — the service account that
>   runs the harvester and needs your logged-in browser sessions.
> - **Control host:** `infra@CENTRAL-DEV` — where this playbook is executed from.
> - **Secrets:** Infisical (machine identity, non-interactive).

---

## 0. Parameters

Set these once at the top of the run. Real values for the *connection* secrets
(SSH keys, hosts) come from Infisical — see §2 — not from this file.

| Var | Meaning | Example |
|-----|---------|---------|
| `EH_HOST` | harvester service host | `SMALLBLACKBOX` |
| `EH_USER` | harvester service account | `jobhelp` |
| `EH_HOME` | checkout dir on the host | `/home/jobhelp/event_tool` |
| `INFISICAL_PROJECT_ID` | Infisical project | `<uuid>` |
| `INFISICAL_ENV` | Infisical environment slug | `prod` |
| `INFISICAL_PATH` | secret folder | `/event-harvester` |
| `EH_TARGET_OS` | `linux` or `windows` (SMALLBLACKBOX) | `linux` |
| `SRC_HOST` | machine holding the live browser profile | this workstation |

---

## 1. Prerequisites & platform decision

**Decide `EH_TARGET_OS` first — it changes three things below.**

`pyproject.toml` pins `platforms = ["win-64"]`, and these sources are
**Windows-only**: `discord` (Electron LevelDB cache + `pywin32`), `signal`
(SQLCipher via Windows paths). `telegram`, `gmail`, and `web` are
cross-platform.

- **SMALLBLACKBOX is Windows** → full source set works; use the profile-copy
  cookie path (§3a) and the Windows service recipe (§5-win).
- **SMALLBLACKBOX is Linux** → you must **either**
  - add `linux-64` to `[tool.pixi.workspace].platforms` and drop the
    Windows-only deps, **and** run the harvester with `--skip discord,signal`; **or**
  - keep those sources on a separate Windows box.
  Use the `storage_state` cookie path (§3b) and the systemd recipe (§5-linux).

Host baseline (both OSes):

```bash
# pixi (https://pixi.sh)
curl -fsSL https://pixi.sh/install.sh | bash          # linux/mac
# winget install prefix-dev.pixi                       # windows

# Chrome/Chromium — required for the web/uc path (native-chrome + UC extension)
#   linux:   apt-get install -y chromium  (or google-chrome-stable)
#   windows: install Google Chrome normally

# infisical CLI (https://infisical.com/docs/cli/overview)
curl -1sLf 'https://dl.cloudsmith.io/public/infisical/infisical-cli/setup.deb.sh' | sudo -E bash
sudo apt-get install -y infisical
```

---

## 2. Secrets via Infisical (non-interactive)

### 2.1 Authenticate with a machine identity

Create a **Universal Auth** machine identity in Infisical scoped to
`INFISICAL_PATH`, then on the host export its bootstrap creds. These two values
are the *only* secret material that touches disk, and only on the control/service
host — keep the file `0600` and owned by the service user.

```bash
# /etc/event-harvester/infisical.env   (chmod 600, owner jobhelp)
INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=<machine-identity-client-id>
INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=<machine-identity-client-secret>
```

With those two vars in the environment, `infisical run` and `infisical secrets`
authenticate automatically — no `infisical login`, no human in the loop.

### 2.2 Pin project defaults

Create `.infisical.json` at the repo root so commands stay short:

```json
{
  "workspaceId": "<INFISICAL_PROJECT_ID>",
  "defaultEnvironment": "prod"
}
```

### 2.3 Secret inventory

Store these under `INFISICAL_PATH`. **Secrets** (must be in Infisical);
**config** (may be plain env or Infisical). Session *files* are handled in §3,
not here.

| Key | Kind | Notes |
|-----|------|-------|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_PHONE` | secret | from my.telegram.org |
| `OPENAI_API_KEY` *or* `ANTHROPIC_API_KEY` *or* `OPENROUTER_API_KEY` | secret | pick per `LLM_MODEL` |
| `TICKTICK_CLIENT_ID` / `TICKTICK_CLIENT_SECRET` | secret | dev app creds |
| `TICKTICK_USERNAME` / `TICKTICK_PASSWORD` | secret | login |
| `GMAIL_CREDENTIALS_JSON` | secret | base64 of `credentials.json` |
| `GMAIL_TOKEN_JSON` | secret | base64 of `token.json` (post-OAuth) |
| `TELEGRAM_SESSION_B64` | secret | base64 of `harvest_session.session` (post-OTP) |
| `LLM_BACKEND` / `LLM_MODEL` | config | e.g. `litellm` / `gpt-4o-mini` |
| `TICKTICK_REDIRECT_URI` / `TICKTICK_PROJECT` | config | |
| `DAYS_BACK` | config | default `7` |

> Gmail & Telegram are file-based at runtime but small, so we carry them as
> base64 secrets and materialize them in §3. The **Chrome profile is too large
> and OS-bound** to live in Infisical — it is provisioned by file copy in §3a/§3b.

### 2.4 The run wrapper

Every harvester process is launched through `infisical run` so secrets are
injected as env vars and never written to a `.env`:

```bash
infisical run --projectId "$INFISICAL_PROJECT_ID" --env "$INFISICAL_ENV" \
  --path "$INFISICAL_PATH" -- \
  pixi run event-harvester <subcommand> [args]
```

---

## 3. Provision browser session state (the "move the cookies" step)

The harvester scrapes gated web sources using **your logged-in browser
sessions**. Those live in `data/.chrome_profile/` (persistent Chrome profile)
and/or `data/.playwright_state.json` (portable `storage_state`), written by
`event-harvester web login` on your workstation ([web_session.py](../src/event_harvester/sources/web_session.py)).

Choose **3a** or **3b** by `EH_TARGET_OS` vs. `SRC_HOST` OS.

### 3a. Same-OS → full profile copy  *(your selected artifact)*

Only valid when `SRC_HOST` and `SMALLBLACKBOX` are the **same OS** (cookie
encryption keys are OS/account-bound; a cross-OS copy arrives undecryptable).

```bash
# From SRC_HOST. Stop Chrome first so the profile isn't locked.
rsync -az --delete \
  "data/.chrome_profile/" \
  "${EH_USER}@${EH_HOST}:${EH_HOME}/data/.chrome_profile/"

# Lock it down on the target
ssh "${EH_USER}@${EH_HOST}" "chmod -R go-rwx '${EH_HOME}/data/.chrome_profile'"
```

> **Windows→Linux will not work here** — the copied cookies are DPAPI-encrypted
> and won't decrypt under Linux `os_crypt`. Use 3b instead.

### 3b. Cross-OS → portable storage_state

`data/.playwright_state.json` stores cookies as plaintext JSON, so it is
OS-portable. Regenerate it fresh on `SRC_HOST` if stale:

```bash
# On SRC_HOST: log in interactively, saves data/.playwright_state.json
pixi run event-harvester web login
```

```bash
# Ship just the one file (treat it as a live credential — it contains session tokens)
scp data/.playwright_state.json \
    "${EH_USER}@${EH_HOST}:${EH_HOME}/data/.playwright_state.json"
ssh "${EH_USER}@${EH_HOST}" "chmod 600 '${EH_HOME}/data/.playwright_state.json'"
```

### 3c. Materialize the file-based auth carried in Infisical

On `SMALLBLACKBOX`, after checkout:

```bash
infisical secrets get GMAIL_CREDENTIALS_JSON --plain | base64 -d > credentials.json
infisical secrets get GMAIL_TOKEN_JSON       --plain | base64 -d > token.json
infisical secrets get TELEGRAM_SESSION_B64   --plain | base64 -d > harvest_session.session
chmod 600 credentials.json token.json harvest_session.session
```

---

## 4. Deploy the code

```bash
ssh "${EH_USER}@${EH_HOST}"

git clone --recurse-submodules https://github.com/Ethycs/event_tool "$EH_HOME"
cd "$EH_HOME"
git submodule update --init --recursive     # pulls ext/universal_controller

pixi install                                 # conda + PyPI into .pixi/envs/
pixi run playwright install chromium         # browser binaries for the web path
```

`ext/universal_controller` is wired as an editable dep (`uc_browser` package,
used by the web/cookie path) via `[tool.pixi.pypi-dependencies]`, so `pixi
install` covers it. It also ships its own standalone pixi env for the uc/grok
proxy — only needed if you run that proxy (see [uc_grok_proxy.md](uc_grok_proxy.md)).

---

## 5. Run as a service

### 5-linux. systemd

`/etc/systemd/system/event-harvester.service` — the persistent MCP backend:

```ini
[Unit]
Description=event-harvester MCP service
After=network-online.target

[Service]
User=jobhelp
WorkingDirectory=/home/jobhelp/event_tool
EnvironmentFile=/etc/event-harvester/infisical.env
ExecStart=/usr/bin/infisical run --env=prod --path=/event-harvester -- \
  pixi run event-harvester mcp --transport streamable-http --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/event-harvester-gateway.service` — the FastMCP gateway
([gateway.py](../gateway.py)):

```ini
[Unit]
Description=event-harvester gateway
After=event-harvester.service
Requires=event-harvester.service

[Service]
User=jobhelp
WorkingDirectory=/home/jobhelp/event_tool
Environment=EH_MCP_URL=http://127.0.0.1:8765/mcp
Environment=GATEWAY_HOST=127.0.0.1
Environment=GATEWAY_PORT=9000
ExecStart=/usr/bin/pixi run gateway
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now event-harvester event-harvester-gateway
```

> Optional units, same pattern: the Flet control UI
> (`event-harvester serve`, port 8550) and the poller (`event-harvester watch`).

### 5-win. Windows service

Use NSSM or Task Scheduler. Wrap the same command; supply the two
`INFISICAL_UNIVERSAL_AUTH_*` vars via the service's environment:

```powershell
nssm install event-harvester "pixi" "run event-harvester mcp --transport streamable-http --port 8765"
nssm set     event-harvester AppDirectory "C:\event_tool"
# Prefix with `infisical run -- ` by wrapping in a .cmd, or set env from infisical export.
```

---

## 6. Bootstrap the interactive credentials (first deploy only)

Two integrations need a one-time interactive step; after that they are files
(carried in Infisical per §2.3/§3c on later deploys):

- **Telegram OTP** — first `harvest` prompts for the SMS/app code, then caches
  `harvest_session.session`. Run once on a TTY:
  ```bash
  infisical run --env=prod --path=/event-harvester -- \
    pixi run python scripts/auth_telegram.py
  # then capture it for future deploys:
  base64 -w0 harvest_session.session   # → store as TELEGRAM_SESSION_B64
  ```
- **TickTick OAuth** — opens a browser for consent on first task creation.
  On a headless host, do the consent on a machine with a browser, then copy the
  cached token to the host (or run once with X-forwarding).

---

## 7. Verify

```bash
# MCP tools/resources reachable?
infisical run --env=prod --path=/event-harvester -- \
  pixi run python scripts/mcp_probe.py

# Credential readiness + enabled sources, no live harvest:
infisical run --env=prod --path=/event-harvester -- \
  pixi run python scripts/mcp_probe.py call config_summary

# Gateway up?
curl -s http://127.0.0.1:9000/mcp -H 'accept: text/event-stream' | head

# Smoke a real (dry) harvest — skip Windows-only sources if on Linux:
infisical run --env=prod --path=/event-harvester -- \
  pixi run event-harvester --days 1 --dry-run --skip discord,signal
```

`config_summary` should show the enabled sources and every credential as ready.
A green `--dry-run` with non-zero web/telegram/gmail counts confirms the cookies
and secrets are wired correctly.

---

## 8. Redeploy (the steady-state loop)

```bash
ssh "${EH_USER}@${EH_HOST}"
cd "$EH_HOME"
git pull
git submodule update --init --recursive
pixi install
sudo systemctl restart event-harvester event-harvester-gateway
# re-run §7 verify
```

Session state (`data/.chrome_profile` / `.playwright_state.json`,
`harvest_session.session`, `token.json`) persists across redeploys and only
needs refreshing when a login expires — re-run §3 for the affected artifact.

---

## 9. Rollback

```bash
cd "$EH_HOME"
git log --oneline -n 10
git checkout <last-good-sha>
pixi install
sudo systemctl restart event-harvester event-harvester-gateway
```

If a cookie refresh broke scraping, restore the prior artifact and restart — the
session files are the only stateful, non-git-tracked inputs.

---

## Appendix B — Windows→Windows operator runbook (run by hand)

SMALLBLACKBOX is Windows, so §3a's `rsync` doesn't apply. Run this from the
workstation holding the live profile (`SRC_HOST`), in **PowerShell**, with Chrome
**closed** so the profile isn't locked. Rename the Infisical `--path` and key
names to match your vault. These steps handle live session credentials — run
them yourself; keep the private keys out of any shared logs.

```powershell
# 0. Release profile locks (close Chrome) and confirm OpenSSH Server is up on SMALLBLACKBOX
Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force

# 1. Authenticate Infisical non-interactively (machine identity)
$env:INFISICAL_UNIVERSAL_AUTH_CLIENT_ID     = '<client-id>'
$env:INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET = '<client-secret>'

# 2. Pull SMALLBLACKBOX connection details from Infisical  (adjust key names/path)
$SB_HOST = (infisical secrets get SMALLBLACKBOX_HOST --plain --env=prod --path=/deploy)
$SB_USER = 'jobhelp'
$SB_DEST = 'C:/event_tool/data'
$keyPath = Join-Path $env:TEMP 'sb_key'
infisical secrets get SMALLBLACKBOX_SSH_KEY --plain --env=prod --path=/deploy |
  Set-Content -NoNewline -Encoding ascii $keyPath
icacls $keyPath /inheritance:r /grant:r "$($env:USERNAME):R" | Out-Null   # OpenSSH requires tight perms

# 3. Package the FULL profile (your selected artifact) and ship it
$zip = Join-Path $env:TEMP 'chrome_profile.zip'
Compress-Archive -Path 'data\.chrome_profile\*' -DestinationPath $zip -Force
scp -i $keyPath $zip "${SB_USER}@${SB_HOST}:$SB_DEST/chrome_profile.zip"
ssh -i $keyPath "${SB_USER}@${SB_HOST}" `
  "powershell -NoProfile -Command \"Expand-Archive -Force '$SB_DEST/chrome_profile.zip' '$SB_DEST/.chrome_profile'; Remove-Item '$SB_DEST/chrome_profile.zip'\""

# 4. Clean up local key + archive
Remove-Item $keyPath, $zip -Force
```

> The full profile carries volatile caches (`Cache`, `Code Cache`, `GPUCache`,
> `ShaderCache`) that aren't needed for auth — the session state lives in
> `Default\Network\Cookies`, `Default\Local Storage`, and `Default\Login Data`.
> To slim the transfer, add `-Path` exclusions or copy only `Default\` minus the
> cache dirs. Cookies still decrypt because SMALLBLACKBOX is the same OS family.

Then connect to CENTRAL-DEV and run this playbook there:

```powershell
$CD_HOST = (infisical secrets get CENTRALDEV_HOST --plain --env=prod --path=/deploy)
$cdKey   = Join-Path $env:TEMP 'cd_key'
infisical secrets get CENTRALDEV_SSH_KEY --plain --env=prod --path=/deploy |
  Set-Content -NoNewline -Encoding ascii $cdKey
icacls $cdKey /inheritance:r /grant:r "$($env:USERNAME):R" | Out-Null
ssh -i $cdKey infra@$CD_HOST      # → execute docs/deploy_playbook.md from here
Remove-Item $cdKey -Force
```

---

## Appendix — what is and isn't a secret

- **In Infisical:** all API keys/creds, plus base64 of the small auth *files*
  (`GMAIL_*`, `TELEGRAM_SESSION_B64`).
- **Provisioned by file copy (not Infisical):** the Chrome profile / Playwright
  `storage_state` — too large and/or OS-bound. Treat these as live credentials
  in transit: they carry active session tokens.
- **Never committed:** `.env`, `data/.chrome_profile/`, `data/.playwright_state.json`,
  `harvest_session.session`, `token.json`, `credentials.json`.
