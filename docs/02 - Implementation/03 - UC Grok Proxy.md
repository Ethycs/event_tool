# Calling `uc-grok` via the litellm proxy
**Status:** CURRENT · **Date:** 2026-11-17 · **Applies to:** `litellm.config.yaml`, `uc_provider_loader.py`, `ext/universal_controller/uc_browser/sites/grok_fast.py` · **Depends on:** `docs/03 - Architecture/10 - LLM Layer & Interlock.md`

The litellm proxy fronts our browser-driven Grok client as an OpenAI-compatible
endpoint. Any OpenAI SDK, curl, Cline/Continue plugin, etc. that can point at a
custom `base_url` works unchanged.

The pathway is:

```
client → POST /v1/chat/completions  (model: "uc-grok")
       → litellm proxy router       (resolves uc-grok → uc/grok)
       → UCBrowserCustomLLM         (strips uc/, hits site="grok")
       → GrokFastClient.send        (Grok's own submit; reply read back over Grok's REST API)
       → only if nothing was sent: GrokClient.send fallback (DOM-driven)
```

There is **no OpenAI hop**. The `openai/` prefix is irrelevant here.

---

## 1. Start the proxy

### Linux: user service (smallblackbox)

`~/.config/systemd/user/uc-grok-proxy.service` runs the proxy on `127.0.0.1:4000`, enabled at login:

```bash
systemctl --user status uc-grok-proxy
systemctl --user restart uc-grok-proxy      # after code or cookie changes
journalctl --user -u uc-grok-proxy -f
```

The unit sets `DISPLAY=:0` (the browser is headed) and its own browser state:

- `UC_CHROME_PROFILE=data/.grok/.chrome_profile`. The browser profile actually used is the sibling `data/.grok/.uc_chromium_profile` (`browser.py` derives it from that path's parent). It is separate from the harvest's `data/.uc_chromium_profile`: a long-lived proxy holding the harvest's profile would make every web harvest fail to launch.
- `UC_STATE_FILE=data/.grok/state.json`: the Grok session cookies, injected at browser start (see "Signing in").

Anything else that needs the Grok session at the same time (e.g. `scripts/export_grok_history.py`) must use a different `UC_CHROME_PROFILE`; Chromium refuses a profile another process holds.

### Windows: by hand

From the repo root:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
pixi run -e dev litellm --config litellm.config.yaml --port 4000 --num_workers 1
```

The UTF-8 env vars stop the proxy's startup banner from crashing on
Windows `cp1252`. `--num_workers 1` is mandatory: the handler owns a
single Chrome profile and competing workers will corrupt it.

### Sanity check

```bash
curl http://localhost:4000/v1/models
```

You should see `uc-grok` and `grok-browser` in the response.

### Signing in

Grok (and the Google / X sign-in behind it) refuses to sign in inside the automated browser, and there is no `event-harvester web-login` subcommand. Sign in to grok.com in your normal Chrome, then copy its cookies into the state file:

```bash
pixi run python - <<'PY'
import json, os, rookiepy
from uc_browser.chrome_cookies import cookies_to_playwright
path = "data/.grok/state.json"
cookies = cookies_to_playwright(rookiepy.chrome(["grok.com", "x.ai"]))
with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump({"cookies": cookies, "origins": []}, f)
print(len(cookies), "cookies; sso present:", any(c["name"] == "sso" for c in cookies))
PY
systemctl --user restart uc-grok-proxy
```

The `sso` cookie is the session; without it Grok serves a signed-out page ("Sign up to continue"). The client's login-wall check does not recognise that page, so a signed-out proxy hangs instead of raising `GrokAuthRequired`.

---

## 2. OpenAI Python SDK

The model name is **`uc-grok`** (or the alias `grok-browser`). The API
key can be any string — the proxy doesn't verify it.

```python
from openai import OpenAI

client = OpenAI(api_key="anything", base_url="http://localhost:4000/v1")

resp = client.chat.completions.create(
    model="uc-grok",
    messages=[{"role": "user", "content": "Reply in one word: hi"}],
    timeout=300,  # generous: browser-driven generation is slower than API LLMs
)
print(resp.choices[0].message.content)
```

### Continue the same Grok chat across calls

Pass a `session_id` via `extra_body`. The handler maps it to a
conversation URL and reuses the same tab on subsequent calls.

```python
resp = client.chat.completions.create(
    model="uc-grok",
    messages=[{"role": "user", "content": "Remember the number 42."}],
    extra_body={"session_id": "demo-1"},
)
# A few seconds later, in the same session:
resp = client.chat.completions.create(
    model="uc-grok",
    messages=[{"role": "user", "content": "What number did I just give you?"}],
    extra_body={"session_id": "demo-1"},
)
print(resp.choices[0].message.content)   # → "42"
```

Each distinct `session_id` owns its own browser tab, so switching
between sessions costs no navigation.

### Pin to a specific existing chat

If you already know the Grok conversation URL, bypass the session
store and post directly:

```python
resp = client.chat.completions.create(
    model="uc-grok",
    messages=[{"role": "user", "content": "Continue."}],
    extra_body={"conversation_url": "https://grok.com/c/<uuid>"},
)
```

### Blind / fire-and-forget (~1.3 s)

Submit a prompt without waiting for the reply. `content` returns
empty; the conversation URL is still surfaced on `_hidden_params`.

```python
resp = client.chat.completions.create(
    model="uc-grok",
    messages=[{"role": "user", "content": "Just queue this."}],
    extra_body={"session_id": "demo-1", "wait_for_response": False},
)
# resp.choices[0].message.content == ""
```

### Read the conversation URL from a response

```python
url = resp.model_extra.get("hidden_params", {}).get("uc_conversation_url")
# or, on raw litellm responses:
url = resp._hidden_params.get("uc_conversation_url")
```

---

## 3. curl

```powershell
curl -s -X POST http://localhost:4000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{
    "model": "uc-grok",
    "messages": [{"role": "user", "content": "Reply in one word: hi"}]
  }'
```

With a session and a long timeout:

```powershell
curl -s -X POST http://localhost:4000/v1/chat/completions `
  -H "Content-Type: application/json" `
  --max-time 300 `
  -d '{
    "model": "uc-grok",
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "session_id": "demo-1"
  }'
```

Note: through the proxy, `extra_body` entries are accepted as
**top-level keys** on the JSON body too — the proxy flattens them. The
handler reads both shapes.

---

## 4. Streaming (`stream: true`)

Supported but **fake** — the handler emits the entire reply in one
content chunk followed by a `[DONE]` chunk. Token-level streaming
isn't available because `GrokClient.send` is anchor-locked. Use
streaming only when your client *requires* SSE (Cline, Open WebUI,
some Cursor flows); otherwise plain blocking is simpler.

---

## 5. Knobs

| Knob | Where | Effect |
| --- | --- | --- |
| `timeout` (per request) | client SDK / curl `--max-time` | Per-call ceiling. Bump to 300+ for long-form prompts. |
| `timeout` (per model) | `litellm.config.yaml` `litellm_params.timeout` | Default ceiling if client doesn't pass one. Currently 180s. |
| `request_timeout` | `litellm.config.yaml` `general_settings.request_timeout` | Proxy-wide HTTP request ceiling. Raise above 180 if long-form prompts hit a 182-ish wall. |
| `UC_GROK_TIMEOUT_S` | proxy environment (default 300) | Floor on how long the browser waits for Grok's reply; a lower request timeout is raised to it. |
| `session_id` | `extra_body` | Continue the same Grok chat. Each id owns its own tab. |
| `conversation_url` | `extra_body` | Pin to a specific Grok `/c/<uuid>` directly. |
| `wait_for_response: false` | `extra_body` | Fire-and-forget (~1.3 s, empty `content`). |

---

## 6. How the reply is read, and latency

Grok sends chats over a WebSocket (`wss://grok.com/ws/mgw/`, seen 2026-11-17), so there is no HTTP chat response to capture. After the submit is confirmed, `GrokFastClient` polls the REST read endpoints the page itself uses, every 0.5 s: `GET /rest/app-chat/conversations/<id>/response-node` for the new assistant response id, then `POST .../load-responses` until that response has `partial: false`. The reply comes back as Grok stored it, Markdown intact.

If the REST read fails 5 times running, it reads the reply from the page instead. That text is the rendered page text: Markdown formatting is lost, and the leading `Grok` label is stripped. It never re-sends a message that was already submitted.

| Scenario | Wall time |
| --- | --- |
| New chat, cold proxy (browser launch included) | 13 s for a one-word reply (measured 2026-11-17) |
| Blind (`wait_for_response: false`) | ~1.3 s |

Before 2026-11-17 the fast path waited for an HTTP chat response that no longer exists, so every request sat out the full `UC_GROK_TIMEOUT_S` (300 s) before reading the page. Clients with shorter timeouts saw a hang.

---

## 7. Concurrency: one request at a time

The underlying Playwright client uses the sync API, whose greenlet is
bound to the OS thread that first touched it. The provider runs every
browser call on one pinned worker thread (`uc-grok-pw`), so concurrent
requests queue behind each other rather than crash; they still run one
at a time.

True parallelism (N sessions in real flight) needs a migration to
`playwright.async_api`, which is separate work.

---

## 8. Troubleshooting

### `'coroutine' object is not an iterator` on `stream=True`

You're on an older build of the provider. Fake-streaming was added
when the `streaming()`/`astreaming()` methods landed. Pull latest.

### `GrokAuthRequired` on every call, or every call hangs

The proxy's browser isn't signed in to Grok. Refresh `data/.grok/state.json` from your normal Chrome as in "Signing in", then restart the service. To see what the browser sees, stop the service and open the Grok page with the same `UC_CHROME_PROFILE` / `UC_STATE_FILE`.

### Long prompt times out at ~182 s

Hitting the per-model `timeout` (180 s in `litellm.config.yaml`;
`general_settings.request_timeout` is not set there). Two fixes:

1. Pass `timeout=300` (or higher) from the client.
2. Raise the per-model `timeout` in `litellm.config.yaml`.

Worth checking the Grok tab too — if the stop button is still showing
when the request returns, generation was still in progress and the
client cut you off, not Grok.

### `[grok_fast] REST read ...` warnings in the log

The reply was read from the page instead of the REST API (see section 6). If it happens on every call, Grok has probably changed its read endpoints again: run a send with a request listener on the page and compare what the page calls with the paths in `grok_fast.py`.

### `ImportError: Could not import uc_handler ...`

The proxy resolves `custom_provider_map` entries as **file paths
relative to the config dir**, not Python imports. The config uses
`uc_provider_loader.uc_handler`, which resolves to
`./uc_provider_loader.py` next to `litellm.config.yaml`. That file is
a shim that re-exports `uc_handler` from the package. Keep it where
it is.

### `Cannot switch to a different thread`

A browser call ran outside the pinned `uc-grok-pw` worker (section 7);
call the handler through the proxy or `litellm.completion`, not from
your own threads.

---

## 9. Direct (non-proxy) use

If you want to skip the proxy and call the handler in-process:

```python
import litellm
from uc_browser.llm_providers.uc import register_uc_provider

register_uc_provider()  # wires "uc" into litellm.custom_provider_map

resp = litellm.completion(
    model="uc/grok",
    messages=[{"role": "user", "content": "hi"}],
    extra_body={"session_id": "demo-1"},
)
print(resp.choices[0].message.content)
```

Same handler, no HTTP hop. Useful for scripts and tests.
