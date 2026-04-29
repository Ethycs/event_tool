# Browser Configuration Attempt Log

This file is the canonical record of every browser launch configuration we have tried for this project, along with the method used and the outcome observed. Each entry has a stable `BC-NNN` ID. Before proposing a new approach, scan this log so we don't repeat configurations that have already been ruled out.

Each row in the summary table is also documented in detail below the table. New attempts: scaffold the next `BC-NNN` ID, fill in the table row, and add a Notes section.

---

## Summary Table

| ID | Date | Method | Outcome |
|---|---|---|---|
| [BC-001](#bc-001) | 2026-04-12 | Playwright `launch_persistent_context` + `channel="chrome"` + `data/.chrome_profile/` | **WORKS** for manual login + session persistence. Google blocks password sync. |
| [BC-002](#bc-002) | 2026-04-12 | subprocess Chrome `--remote-debugging-port=9222` + `--user-data-dir=$LOCALAPPDATA/Google/Chrome/User Data` | **FAIL.** Chrome refuses default profile for CDP. |
| [BC-003](#bc-003) | 2026-04-12 | subprocess Chrome + minimal auth-file copy | **FAIL.** Chrome crashes on incomplete profile. |
| [BC-004](#bc-004) | 2026-04-12 | subprocess Chrome + full `Default/` copy minus caches | **FAIL.** Crashes on stale SQLite journals/locks. |
| [BC-005](#bc-005) | 2026-04-12 | Playwright `launch_persistent_context` + real User Data + `--profile-directory=EventHarvester` | **FAIL.** Exit 21. Playwright args corrupt real profile. |
| [BC-006](#bc-006) | 2026-04-12 | `rookiepy.chrome()` cookie injection into Playwright Chromium | **FAIL.** 0 cookies returned (encryption + DB lock). |
| [BC-007](#bc-007) | 2026-04-12 | `web_login` (BC-001) + extension mode reads `.playwright_state.json` cookies | **WORKS** — validated by harness 2026-04-26 (all 5 phases PASS, 6.0s). Production path. |
| [BC-008](#bc-008) | 2026-04-12 | Playwright `launch_persistent_context` + `channel="chrome"` + KeePassXC extension | **FAIL.** Exit 21. Branded Chrome blocks `--load-extension`. |
| [BC-009](#bc-009) | 2026-04-12 | Playwright `launch_persistent_context` + bundled Chromium + extension + `ignore_default_args=["--disable-extensions"]` | **FAIL.** `0x80000003` crash. `--remote-debugging-pipe` conflicts with extension load. |
| [BC-010](#bc-010) | 2026-04-12 | subprocess Playwright Chromium + `--remote-debugging-port=9223` + extension + `connect_over_cdp` | **WORKS** — validated by harness 2026-04-26 (all 6 phases PASS, 9.4s). User confirmed KeePassXC extension is visible in toolbar and content scripts inject on login forms. Has `--no-sandbox` which Google's OAuth detects and blocks. |
| [BC-011](#bc-011) | 2026-04-28 | BC-010 minus `--no-sandbox` | **WORKS — full end-to-end.** lu.ma signin loads, Google auth blocker bypassed, KeePassXC extension associates with database, password saved. **This is the working KeePassXC path for sites with Google SSO.** |

Status legend: **WORKS** (passes auth_test or login flow), **PARTIAL** (some phases pass), **FAIL** (does not start, does not connect, or crashes), **IN PROGRESS** (not yet run end-to-end).

---

## Detail Notes

### BC-001

- **Goal:** Original `web_login` flow — open a real Chrome window for the user to manually sign in and persist sessions.
- **Method:** `p.chromium.launch_persistent_context("data/.chrome_profile", channel="chrome", args=["--disable-blink-features=AutomationControlled", *extension_args()])`
- **Outcome:** Browser opens; manual login succeeds; cookies/storage persist in `data/.chrome_profile/` and are exported to `data/.playwright_state.json`. **Google blocks password manager sync** because Playwright sets internal `--enable-automation`-equivalent flags that Google's sync service detects.
- **Lesson:** Persistent context with `channel="chrome"` is reliable for manual auth, but Google detects automation regardless of `--disable-blink-features`. Password sync cannot be coaxed into working in this mode.

### BC-002

- **Goal:** Bypass automation detection by using subprocess Chrome with the user's real profile directly.
- **Method:** `subprocess.Popen(chrome.exe, --remote-debugging-port=9222, --user-data-dir=$LOCALAPPDATA/Google/Chrome/User Data)`
- **Outcome:** Chrome refuses to start, prints: *"DevTools remote debugging requires a non-default data directory. Specify this using --user-data-dir."* This is a Chrome security policy.
- **Lesson:** CDP debugging cannot be enabled against the default profile. Any subprocess+CDP path requires a separate `--user-data-dir`.

### BC-003

- **Goal:** Combine the subprocess+CDP approach (BC-002) with a minimal copy of auth files into a non-default profile dir.
- **Method:** Copy `Local State` + `Default/{Login Data, Cookies, Web Data, Preferences}` only to `data/.native_chrome_profile/`, then `subprocess.Popen` with that dir.
- **Outcome:** Chrome starts but crashes immediately. Profile is in an inconsistent state — references files that don't exist (managed_storage, History, Bookmarks, etc.).
- **Lesson:** Chrome profile is not a flat collection of independent files. Copying only "auth" files leaves dangling references that crash the browser.

### BC-004

- **Goal:** Like BC-003 but copy the entire `Default/` directory minus large/cache directories to keep state consistent.
- **Method:** Copy all top-level files + full `Default/` minus `Cache`, `Code Cache`, `GPUCache`, `DawnGraphiteCache`, `DawnWebGPUCache`, `Service Worker`, `blob_storage`, `File System`, `IndexedDB`, etc.
- **Outcome:** Still crashes. Likely because copy happens while Chrome holds locks on SQLite journal files, or because ephemeral state (Sessions, Tabs) references rolled-up cache content.
- **Lesson:** A live Chrome profile cannot be safely cloned while Chrome is running. We'd need to fully terminate Chrome, copy, then run — but that breaks the user's running browser.

### BC-005

- **Goal:** Use Playwright `launch_persistent_context` against the real `User Data` directory but with a custom `--profile-directory=EventHarvester` so we don't conflict with the user's `Default` profile.
- **Method:** `launch_persistent_context(user_data_dir=$LOCALAPPDATA/Google/Chrome/User Data, channel="chrome", args=[..., "--profile-directory=EventHarvester", "--load-extension=..."])`
- **Outcome:** Chrome exits with code 21 within seconds of launch.
- **Lesson:** Playwright always injects `--disable-extensions`, `--enable-automation`, `--disable-sync`, `--no-sandbox`, etc. These flags conflict with `--load-extension` and corrupt the real `User Data` directory's profile metadata. The "profile-directory inside User Data" idea is incompatible with Playwright's launch args.

### BC-006

- **Goal:** Sidestep the launch problem entirely — keep using Playwright's isolated profile (BC-001) but **inject** the user's Chrome cookies via `rookiepy` so authenticated sites work.
- **Method:** `get_chrome_cookies()` (rookiepy with UAC elevation) → `cookies_to_playwright()` → `context.add_cookies()` at startup.
- **Outcome:** Returns **0 cookies** even with elevation. Chrome v130+ uses app-bound encryption that requires DPAPI access *as the Chrome process*. The cookie DB is also locked while Chrome is running.
- **Lesson:** Live Chrome cookie extraction is not viable on Windows with Chrome v130+. This route is closed unless we kill Chrome before extraction (intrusive) or use a non-Windows mechanism.

### BC-007

- **Goal:** Workaround for the auth gap — accept that `web_login` (BC-001) saves cookies to `.playwright_state.json`, and have the fetch-time UCBrowser load those cookies into the extension-mode Chromium context.
- **Method:** In `UCBrowser.start()` for the extension path, read `data/.playwright_state.json`, extract `cookies`, call `self._context.add_cookies(cookies)`.
- **Outcome:** Works. Sessions logged in during `web_login` carry through to fetch-time scraping. Limited to whatever the user manually logged into during `web login`, not the user's full Chrome session.
- **Lesson:** This is the **current production path** for non-Cloudflare web sources. Authenticated browsing works as long as the user runs `web login` once.

### BC-008

- **Goal:** Run the KeePassXC-Browser extension in a Playwright session so KeePassXC can auto-fill passwords during `web_login` (or fetch). First attempt: same launch shape as BC-001 with the extension added.
- **Method:** `launch_persistent_context(channel="chrome", args=["--load-extension=...", "--disable-extensions-except=..."])`
- **Outcome:** Chrome exits code 21 immediately.
- **Lesson:** Branded Chrome (channel `"chrome"`) removed the command-line flags Playwright relies on for sideloaded extensions. Playwright's docs confirm: extensions only work with bundled Chromium (`channel=None` or `channel="chromium"`).

### BC-009

- **Goal:** Switch to bundled Chromium per Playwright's documented MV3 pattern.
- **Method:** `launch_persistent_context(channel=None, args=[...extension args], ignore_default_args=["--disable-extensions"])` — explicitly removing `--disable-extensions` from Playwright's defaults.
- **Outcome:** Chromium crashes during startup with exit code `0x80000003` (`STATUS_BREAKPOINT`). The launch logs show all of Playwright's defaults plus `--remote-debugging-pipe`. The pipe-based CDP transport conflicts with `--load-extension` in Chromium 1208 (the version shipped with our installed Playwright).
- **Lesson:** Playwright uses pipe-mode CDP by default and we cannot override that via `args`. To run with extensions in bundled Chromium, we must launch the binary ourselves and use port-mode CDP.

### BC-011

- **Goal:** Same as BC-010 but remove `--no-sandbox` from the launch args. Google's OAuth iframe detects `--no-sandbox` and refuses to render the sign-in window in BC-010, even though the rest of the page loads.
- **Method:** Subprocess Playwright Chromium on port 9224, KeePassXC extension via `--load-extension`, native messaging registered under Chromium key, profile at `data/.keepassxc_test_profile_011`. Args: `--no-first-run`, `--no-default-browser-check`. **No `--no-sandbox`.**
- **Outcome (2026-04-28):** Full end-to-end success. lu.ma signin page loaded. User signed in (Google OAuth iframe rendered correctly). KeePassXC extension prompted for association, user associated, KeePassXC saved the lu.ma password into the database. Profile now has both session cookie and KPXC association token.
- **Lesson:** `--no-sandbox` is the single biggest signal Google uses to detect automation-controlled browsers. Removing it costs nothing on Windows (Chromium runs fine without it via the bundled binary's default sandbox setup) and unblocks Google sign-in flows. **This is the production-ready path for KeePassXC + Google SSO sites.**

### BC-010

- **Goal:** Manually launch Playwright's bundled Chromium with `--remote-debugging-port=9223` and `--load-extension=...`, then `connect_over_cdp` from Playwright. This gives us full control over launch args while still getting Playwright's high-level page API.
- **Method:**
  ```python
  proc = subprocess.Popen([
      ".../ms-playwright/chromium-1208/chrome-win64/chrome.exe",
      "--remote-debugging-port=9223",
      "--user-data-dir=...",
      "--load-extension=...",
      "--disable-extensions-except=...",
      "--no-first-run", "--no-default-browser-check", "--no-sandbox",
      "about:blank",
  ])
  browser = p.chromium.connect_over_cdp("http://localhost:9223")
  ```
  Plus `_ensure_chromium_native_messaging()` to register `org.keepassxc.keepassxc_browser` under `HKCU\Software\Chromium\NativeMessagingHosts`.
- **Outcome (in progress):** Manual subprocess launch verified — Chromium starts, all 70 sub-processes appear in `tasklist`. Playwright `connect_over_cdp` not yet validated end-to-end. Native messaging registration succeeds. Service worker registration not yet observed.
- **Lesson (so far):** This is the most promising path. Decoupling the launch from Playwright sidesteps the pipe-mode conflict and the implicit-args corruption. Next: confirm `context.service_workers` populates and that KeePassXC native messaging actually wires up.

---

## How to add a new attempt

1. Pick the next `BC-NNN` ID.
2. Add a row to the Summary Table.
3. Add a Detail Notes section with **Goal**, **Method**, **Outcome**, **Lesson**.
4. If automation-runnable, add a corresponding `BrowserConfig` preset to `scripts/browser_configs.py` with the same ID.
5. Run `pixi run python scripts/browser_test.py --run BC-NNN` to record the result in `data/browser_test_results.jsonl`.
