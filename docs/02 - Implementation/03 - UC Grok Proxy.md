# Calling `uc-grok` via the litellm proxy
**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `litellm.config.yaml`, `uc_provider_loader.py` · **Depends on:** `docs/03 - Architecture/10 - LLM Layer & Interlock.md`

The litellm proxy fronts our browser-driven Grok client as an OpenAI-compatible
endpoint. Any OpenAI SDK, curl, Cline/Continue plugin, etc. that can point at a
custom `base_url` works unchanged.

The pathway is:

```
client → POST /v1/chat/completions  (model: "uc-grok")
       → litellm proxy router       (resolves uc-grok → uc/grok)
       → UCBrowserCustomLLM         (strips uc/, hits site="grok")
       → GrokFastClient.send        (React-onSubmit + page.expect_response)
       → on RuntimeError, GrokClient.send fallback (DOM-driven)
```

There is **no OpenAI hop**. The `openai/` prefix is irrelevant here.

---

## 1. Start the proxy

From the repo root:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
pixi run -e dev litellm --config litellm.config.yaml --port 4000 --num_workers 1
```

The UTF-8 env vars stop the proxy's startup banner from crashing on
Windows `cp1252`. `--num_workers 1` is mandatory: the handler owns a
single Chrome profile and competing workers will corrupt it.

Sanity check the proxy is up:

```powershell
curl http://localhost:4000/v1/models
```

You should see `uc-grok` and `grok-browser` in the response.

If you haven't logged into Grok in the persistent profile yet, the
first real call will raise `GrokAuthRequired`. Run this once
beforehand (a Chrome window opens — sign in, then close it):

```powershell
pixi run event-harvester web-login --urls https://grok.com
```

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
| `session_id` | `extra_body` | Continue the same Grok chat. Each id owns its own tab. |
| `conversation_url` | `extra_body` | Pin to a specific Grok `/c/<uuid>` directly. |
| `wait_for_response: false` | `extra_body` | Fire-and-forget (~1.3 s, empty `content`). |

---

## 6. Latency you should expect

| Scenario | Wall time |
| --- | --- |
| Cold start (first call in process) | ~6 s |
| Warm continue (same session) | ~1.8–3 s + Grok generation time |
| Blind (`wait_for_response: false`) | ~1.3 s |
| New chat from `grok.com/` root | ~6 s (navigation + chat creation) |

The fast path (React `onSubmit` + `page.expect_response`) is tried
first. On `RuntimeError` it falls back transparently to the DOM-driven
client (`GrokClient.send`), so callers always get an answer — but the
fallback adds ~60 s in the worst case.

---

## 7. Concurrency: hard limit of 1 in flight

The underlying Playwright client uses the sync API, whose greenlet is
**thread-bound to the first OS thread that touched it**. Concurrent
requests from the proxy can land on different `asyncio.to_thread`
workers and crash with:

```
greenlet.error: Cannot switch to a different thread
```

Until the dedicated-PW-thread pin lands, treat the proxy as
single-flight: send one request, wait for it, send the next. Don't
fan out 5 concurrent `acompletion` calls.

True parallelism (N sessions in real flight) needs a migration to
`playwright.async_api` — separate work item.

---

## 8. Troubleshooting

### `'coroutine' object is not an iterator` on `stream=True`

You're on an older build of the provider. Fake-streaming was added
when the `streaming()`/`astreaming()` methods landed. Pull latest.

### `GrokAuthRequired` on every call

The persistent profile at `data/.uc_chromium_profile/` isn't logged
into Grok. Run:

```powershell
pixi run event-harvester web-login --urls https://grok.com
```

### Long prompt times out at ~182 s

Hitting the proxy's `request_timeout` (currently 180 s) or the
default 60 s `expect_response` window in the fast path. Two fixes:

1. Pass `timeout=300` (or higher) from the client.
2. Raise the per-model `timeout` in `litellm.config.yaml`.

Worth checking the Grok tab too — if the stop button is still showing
when the request returns, generation was still in progress and the
client cut you off, not Grok.

### `ImportError: Could not import uc_handler ...`

The proxy resolves `custom_provider_map` entries as **file paths
relative to the config dir**, not Python imports. The config uses
`uc_provider_loader.uc_handler`, which resolves to
`./uc_provider_loader.py` next to `litellm.config.yaml`. That file is
a shim that re-exports `uc_handler` from the package. Keep it where
it is.

### `Cannot switch to a different thread`

See section 7 — fire one request at a time until the thread pin
lands.

---

## 9. Direct (non-proxy) use

If you want to skip the proxy and call the handler in-process:

```python
import litellm
from event_harvester.llm_providers.uc import register_uc_provider

register_uc_provider()  # wires "uc" into litellm.custom_provider_map

resp = litellm.completion(
    model="uc/grok",
    messages=[{"role": "user", "content": "hi"}],
    extra_body={"session_id": "demo-1"},
)
print(resp.choices[0].message.content)
```

Same handler, no HTTP hop. Useful for scripts and tests.
