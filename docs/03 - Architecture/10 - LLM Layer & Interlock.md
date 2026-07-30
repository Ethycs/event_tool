# LLM Layer & Interlock

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/llm.py`, `src/event_harvester/interlock.py`, `src/event_harvester/_interlock_worker.py`, `uc_provider_loader.py`, `litellm.config.yaml` · **Depends on:** `docs/02 - Implementation/03 - UC Grok Proxy.md`

`event_harvester.llm` is the single call point analysis code uses for chat and vision completions; it dispatches to either `litellm` (cloud APIs, plus the custom browser-driven `uc/grok` provider) or a local RyzenAI NPU/GPU/CPU backend spawned in its own subprocess. The two backends have almost nothing in common operationally: the litellm path is a thin, retry-free pass-through, while the local path is a hand-rolled JSON-line IPC protocol to a worker process with a bounded read timeout (since 2026-07-30) and no cross-process lock. "Interlock" in this codebase names that local-backend subprocess wrapper only — it has no relationship to the browser session's concurrency control, despite the similar-sounding concern.

## Hard constraints

1. **Only one browser-driven (`uc/grok`) call runs at a time — but `interlock.py` has nothing to do with enforcing it.** `Grep` confirms `interlock.py` and `_interlock_worker.py` never import or reference `uc_browser`/Grok. The real single-flight mechanism is in `ext/universal_controller/uc_browser/llm_providers/uc.py:UCBrowserCustomLLM.__init__` — a `ThreadPoolExecutor(max_workers=1)` that pins Playwright's sync-API greenlet to one worker thread for the process lifetime — plus the operational rule in `litellm.config.yaml` (`general_settings.worker_concurrency: 1`, and the comment instructing `--num_workers 1` on the CLI as "belt-and-braces"). See "The `uc/` provider" below.
2. **The local NPU/GPU/CPU backend has no cross-process lock at all.** `interlock.py` provides in-process singleton reuse only (`llm.py:_get_local_session`, keyed by `f"{model_path}:{device}"`); two independent OS processes both running `backend="local"` can each spawn their own `ryzenai-python` worker against the same hardware with nothing in this codebase to stop them.
3. **`cfg.api_key` is never forwarded to litellm.** Neither `_complete_litellm` nor `vision_completion` pass `api_key` in the `litellm.completion(**kwargs)` call — `unverified` how litellm resolves credentials at that point, but by omission it must fall back to litellm's own environment-variable auto-detection per provider prefix in the model string. `cfg.api_key` is read in this module *only* to sniff `sk-ant-` vs. anything else for vision-model auto-selection (`llm.py:_resolve_vision_model`).
4. **`RyzenSession` reads are bounded, writes are not.** *Changed 2026-07-30.* `_recv()` waits at most `io_timeout_s` (default 600s) for a reply and then kills the worker. `_send()` still has no timeout, but a blocking write to a pipe requires the OS buffer to be full, which needs a worker that is reading nothing — the case `_recv` now covers.

## `llm.py` — chat and vision wrappers

**Domain:** single entry point (`chat_completion`, `vision_completion`) that `analysis.py` and `nav_quality.NavQualityScorer` call instead of importing `litellm` directly.
**Runs at:** synchronous, in the caller's thread/process — no async wrapper, no queueing.
**Inputs:** OpenAI-style `messages` list, an `LLMConfig` (from `event_harvester.config`), optional `max_tokens`/`response_format`/image paths.
**Outputs:** the assistant's text content (`str`).

### Backend dispatch

`chat_completion()` (`llm.py:53`) branches purely on `cfg.backend`:

```python
if cfg.backend == "local":
    return _complete_local(messages, cfg, max_tokens=max_tokens)
return _complete_litellm(messages, cfg, max_tokens=max_tokens, response_format=response_format)
```

`cfg.backend` defaults to `"litellm"` (`config.py:33`), overridable via `LLM_BACKEND`.

### Model selection — literal defaults from the code

| Setting | Source | Literal default | Notes |
| --- | --- | --- | --- |
| `LLMConfig.model` | env `LLM_MODEL` (`config.py:284`) | `"gpt-4o-mini"` | Used verbatim as `litellm_model` (`config.py:66`, `.litellm_model` property just returns `self.model` — no provider-prefixing logic). |
| `LLMConfig.api_key` | `OPENAI_API_KEY` else `ANTHROPIC_API_KEY` (`config.py:250`) | `""` | Only consumed by `_resolve_vision_model`'s prefix check — **not** passed to `litellm.completion()`. |
| `LLMConfig.backend` | env `LLM_BACKEND` | `"litellm"` | `"litellm"` or `"local"`. |
| `LLMConfig.vision_model` | env `VLM_MODEL` (`config.py:290`) | `""` (empty ⇒ auto-pick) | Explicit override always wins in `_resolve_vision_model`. |

`unverified`/important correction to the brief this doc was commissioned under: **there is no OpenAI-key-present → X / Anthropic-key-present → Y fallback for chat completions.** That auto-pick-by-key-prefix logic exists **only** in `_resolve_vision_model` (`llm.py:150-161`), for the vision path:

```python
if cfg.api_key.startswith("sk-ant-"):
    return "claude-sonnet-4-7"
return "gpt-4o-mini"
```

For chat, `cfg.model` is used as-is regardless of which key is present. **known rough edge:** if a deployment sets only `ANTHROPIC_API_KEY` and leaves `LLM_MODEL` unset, `chat_completion()` will still target `gpt-4o-mini` (an OpenAI-shaped model string) with no Anthropic-formatted model string in play — this will fail at the litellm layer unless the operator also sets `LLM_MODEL=anthropic/claude-...`. Vision requests from the same config would correctly auto-route to Claude; chat requests would not. This asymmetry is not documented anywhere else in the repo.

The `_resolve_vision_model` docstring says `vision_model` "may not exist on `LLMConfig` yet ... added by another agent" and guards with `getattr`. **dead code? / stale comment:** `vision_model` is a real field on `LLMConfig` (`config.py:44`) as of this read; the `getattr` defensiveness is now unnecessary but harmless.

### Reasoning-effort tuning (chat only)

`_complete_litellm` (`llm.py:80`) adds `reasoning_effort` based on substring match on the model string:

| Model contains | `reasoning_effort` |
| --- | --- |
| `"gpt-5.4"` | `"none"` |
| `"gpt-5"`, `"o1-"`, or `"o3-"` (and not caught by the branch above) | `"minimal"` |
| anything else | not set |

Comment in the code: "GPT-5.4 supports `none`; GPT-5 supports `minimal`; others may vary" — `unverified` beyond that comment.

### Retry/timeout behavior — there mostly isn't any

Neither `_complete_litellm` nor `vision_completion` set `timeout`, `num_retries`, or any litellm retry/backoff parameter. `kwargs` passed to `litellm.completion()` are limited to `model`, `messages`, `max_tokens`, optionally `response_format` and `reasoning_effort` (chat) or just `model`/`messages`/`max_tokens` (vision). Whatever timeout/retry behavior results is **litellm's own internal default** — `unverified` what that default is; this document does not assert a number because the wrapper code doesn't set one.

The **only** retry logic in this module is in `_complete_local` (`llm.py:126`): if `sess.generate()` raises, it logs a warning, calls `shutdown_local()`, creates a fresh `RyzenSession`, and retries exactly once. If the retry also fails, the exception propagates uncaught.

`_complete_litellm` does inspect the response after the fact: if `content` is empty and `finish_reason == "length"`, it logs a warning that a reasoning model burned its token budget on chain-of-thought (`llm.py:116-121`) — this is diagnostic only, not a retry.

### Vision wrapper specifics

`vision_completion()` (`llm.py:164`):
- Builds an OpenAI-shaped multimodal `content` list: one `image_url` (base64 `data:image/png;base64,...`) entry per existing image path, followed by one `text` entry with `prompt`.
- Missing image files are logged (`logger.warning`) and skipped — not fatal (verified by `tests/test_llm_vision.py:test_vision_completion_skips_missing_files`).
- Logs a warning (not an error, not a hard cap) if more than 5 images are passed — **cost-bearing**: nothing stops the call from going out with, say, 20 images; the 5-image ceiling is enforced elsewhere by the caller (`LLMConfig.vision_max_images_per_run`, `config.py:49`, described as a "hard ceiling" used by `nav_quality.NavQualityScorer`, not by this function itself).
- `max_tokens` default is `1024` (vs. `2048` for chat).
- Model resolution goes through `_resolve_vision_model` (see table above).

`tests/test_llm_vision.py` exercises: content-list shape, missing-file skip + warning log, explicit `vision_model` override, `sk-ant-` → `claude-sonnet-4-7` fallback, default → `gpt-4o-mini` fallback, response text passthrough, and the >5-image warning. All six scenarios patch `litellm.completion` directly (no network), so they verify the wrapper's own logic, not litellm/provider behavior.

## The `uc/` provider — browser-driven Grok routed through litellm

**Domain:** a litellm `CustomLLM` (`UCBrowserCustomLLM`, `ext/universal_controller/uc_browser/llm_providers/uc.py:179`) that maps `model="uc/grok"` to a Playwright-driven Grok web session instead of an HTTP API call.
**Runs at:** synchronous `completion()` runs on whatever thread calls it; `acompletion()` (used by the litellm proxy) hands off to a dedicated single-worker `ThreadPoolExecutor` via `loop.run_in_executor`.
**Inputs:** OpenAI-shaped `messages`, plus out-of-band `extra_body` keys: `session_id`, `conversation_url`, `wait_for_response`, `delete_after`, `ephemeral`, and the `action: "cleanup_threads"` control message.
**Outputs:** an OpenAI `ModelResponse` with length-estimated (chars÷4) `Usage`, and `_hidden_params["uc_conversation_url"]`/`["uc_conversation_id"]` for session tracking.

### How litellm is taught about it

Two independent registration paths for the same module-level singleton `uc_handler`:

- **In-process:** `register_uc_provider()` (`uc.py:538`) appends `{"provider": "uc", "custom_handler": uc_handler}` to `litellm.custom_provider_map`. Idempotent, guarded by `_register_lock`.
- **Proxy:** `litellm.config.yaml:51-56` wires `custom_provider_map` declaratively:
  ```yaml
  litellm_settings:
    custom_provider_map:
      - provider: uc
        custom_handler: uc_provider_loader.uc_handler
  ```
  The proxy resolves `custom_handler` as a **file path relative to the config directory**, not a Python import — hence `uc_provider_loader.py` at the repo root exists purely as a shim re-exporting `uc_handler` from `ext/universal_controller/uc_browser/llm_providers/uc.py` (`uc_provider_loader.py:14`), because the proxy's loader can't reach a path under `ext/`.

`litellm.config.yaml` also declares two model aliases, both pointing at `uc/grok` with `timeout: 180`:

| `model_name` | `litellm_params.model` | `timeout` |
| --- | --- | --- |
| `uc-grok` | `uc/grok` | 180s |
| `grok-browser` | `uc/grok` | 180s |

**fragile:** `uc.py:281-284` floors whatever `timeout` it receives at `UC_GROK_TIMEOUT_S` (env, default `300`):
```python
_floor = int(os.environ.get("UC_GROK_TIMEOUT_S", "300"))
timeout_s = int(timeout) if isinstance(timeout, (int, float)) and timeout else _floor
if timeout_s < _floor:
    timeout_s = _floor
```
So the `timeout: 180` in `litellm.config.yaml` is always raised to 300 internally by the handler — but per `docs/02 - Implementation/03 - UC Grok Proxy.md` §8 ("Long prompt times out at ~182 s"), the litellm proxy apparently still enforces its **own** outer ~180s ceiling independent of what the handler does with the value it receives. `unverified` in this read (would require reading litellm's proxy internals) exactly which layer imposes that outer cutoff; documented here only because the companion doc reports it as observed behavior and the two numbers (180 configured vs. 300 floored-to) are inconsistent on their face.

### Concurrency — the real "one session at a time" mechanism

Per `uc.py:186-196`, `UCBrowserCustomLLM.__init__` creates:
```python
self._browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uc-grok-pw")
```
Comment: Playwright's sync API is "greenlet-thread-bound to the first OS thread that touched it"; `asyncio.to_thread`'s default pool would dispatch consecutive `acompletion` calls to different worker threads and crash with `greenlet.error: Cannot switch to a different thread`. The 1-worker executor pins every browser-touching call to the same thread for the process lifetime. This is reinforced (not duplicated) at the proxy level by `litellm.config.yaml`'s `general_settings.worker_concurrency: 1` and the operational requirement to pass `--num_workers 1` on the proxy CLI, both aimed at the same underlying constraint: the Chrome profile at `data/.uc_chromium_profile/` is single-writer.

Session continuity is a separate, in-memory `dict[str, str]` (`self._sessions`, guarded by `self._sessions_lock`, a plain `threading.Lock`) mapping caller-supplied `session_id` → Grok conversation URL. Not persisted to disk — a proxy restart drops all session mappings.

Failure modes surfaced explicitly by the handler:
- `GrokAuthRequired` → re-raised as `litellm.exceptions.AuthenticationError`.
- Unsupported model string (anything but `"grok"` after the `uc/` prefix is stripped) → `litellm.exceptions.BadRequestError`.
- No user message in `messages` → `litellm.exceptions.BadRequestError`.
- `stream=True` is supported but fake: `_stream_chunks` (`uc.py:448`) yields the full text as one chunk then an `is_finished=True` chunk — no token-level streaming, because `GrokClient.send` is anchor-locked/blocking.

## `interlock.py` + `_interlock_worker.py` — local RyzenAI backend, not a lock

**Domain:** subprocess isolation for local ONNX/NPU inference via the RyzenAI SDK, used only when `LLMConfig.backend == "local"`. Despite the name and the framing this doc was commissioned to verify, **this is not a mutex.** A repo-wide search of `src/event_harvester` for `FileLock`, `flock`, `.lock`, or bare `acquire(`/`Lock(` outside this module returns nothing lock-related to Grok/browser sessions — `interlock.py` doesn't reference `uc_browser` or Grok at all. "Interlock" here names the subprocess boundary around the NPU driver session, not a cross-process semaphore with on-disk state. **There is no lock file and no on-disk representation of any kind.**
**Runs at:** the client half (`RyzenSession`) runs synchronously in the caller's process/thread — `_send`/`_recv` are blocking pipe I/O; `_recv` is bounded by `io_timeout_s`. The worker half (`_interlock_worker.py`) runs in a separate OS process spawned via `ryzenai-python` (resolved with `shutil.which`, `interlock.py:36-44`; raises `FileNotFoundError` if not on `PATH`).
**Inputs:** `model_path` (`.onnx` file, or a directory containing `genai_config.json` for the OGA hybrid path), `device` (`"npu"`/`"gpu"`/`"cpu"`), `cache_dir`, `cache_key` (defaults to the first 16 hex chars of the model file's SHA-256, or the directory name for OGA models), `tokenizer_path`, and per-call `messages`/`inputs`.
**Outputs:** for `generate()`, a dict with `text`, `elapsed_s`, `tokens_generated`; for raw `run()`, numpy arrays round-tripped through `.npy` files in a per-session temp directory (`tempfile.mkdtemp(prefix="ryzenai_io_")`) — the module docstring explains this is to dodge pipe-size limitations, since arrays don't fit comfortably in a JSON-line protocol.

### Protocol

Newline-delimited JSON over stdin/stdout, one command → one response, no pipelining. Commands: `init`, `run`, `generate`, `quit`. All diagnostics go to stderr (`_log()`, `_interlock_worker.py:29`) — kept separate from stdout so the protocol channel stays clean. `interlock.py`'s `subprocess.Popen` explicitly passes `stderr=sys.stderr` (inherited, not piped) so worker diagnostics land directly in the parent's stderr stream.

Backend auto-detection at `init` (`_interlock_worker.py:187-197`): a directory containing `genai_config.json` → OGA (`onnxruntime_genai`, for AMD-optimized hybrid NPU+iGPU models); otherwise ORT (`onnxruntime.InferenceSession`). Provider selection for ORT (`_build_providers`, `_interlock_worker.py:55-93`):

| `device` | Provider order attempted |
| --- | --- |
| `npu` | `VitisAIExecutionProvider` (if available, with `cacheDir`/`cacheKey` under `cache_dir`) → `DmlExecutionProvider` (if available) → `CPUExecutionProvider` |
| `gpu` | `DmlExecutionProvider` (if available) → `CPUExecutionProvider` |
| `cpu` | `CPUExecutionProvider` only |

### Acquisition / timeout / staleness — as actually implemented

- **Acquisition:** `llm.py:_get_local_session` is a bare module-level-global getter (`_local_session`, `_local_cfg_key`), keyed by `f"{cfg.model_path}:{cfg.device}"`. Reuses the existing session if the key matches; otherwise builds a new `RyzenSession` and calls `__enter__()`. **fragile:** no `threading.Lock` guards this getter — concurrent calls from two threads in the same process racing on cache-miss could both spawn workers and clobber `_local_session`, leaking the loser's subprocess.
- **Timeout:** `io_timeout_s`, default **600s**, constructor-settable; `0` restores the old unbounded behaviour for debugging under a breakpoint. *Before 2026-07-30 there was none: a worker that hung mid-inference rather than crashing blocked the caller indefinitely.* On expiry the worker is terminated (escalating to `kill`) and `InterlockError` is raised — the session is not reusable, because a reply we stopped waiting for would be read by the *next* `_recv`.

  The read runs on an explicit **daemon** `threading.Thread`. That detail is load-bearing and was found the hard way: a `ThreadPoolExecutor` was tried first, and its threads are non-daemon and joined by an `atexit` hook, so abandoning a blocked `readline` on a pool thread hangs interpreter shutdown instead of the call — trading one hang for a worse one. The test suite hanging at exit is what surfaced it.

  The default is deliberately generous rather than tight: first-run NPU compilation legitimately takes minutes, and a false positive kills a healthy session. The goal is converting "hangs forever" into "fails after a bounded wait", not tight deadline enforcement.
- **Staleness:** none. No heartbeat, no lease, no expiry. The only liveness signal checked before sending a command is `self._proc.poll() is not None` (`interlock.py:165`, `207`) — this only detects a fully-exited process; a wedged-but-alive worker passes this check and then hangs the caller in `_recv()`.

### What happens if a worker dies holding "the lock"

- **Clean crash (process exits):** the next `_send()` either succeeds at the OS pipe-buffer level or raises `BrokenPipeError`, wrapped as `InterlockError("Worker process died unexpectedly.")` (`interlock.py:239-240`). If the write succeeds but the process has already exited, `_recv()`'s `readline()` returns `""` (EOF), raising `InterlockError("Worker process closed stdout (crashed or exited prematurely).")` (`interlock.py:245-248`). Either way, a clean crash is detected and surfaced as a catchable exception — not silent.
- **`llm.py:_complete_local`'s recovery:** catches that exception, logs `"Local session crashed, restarting..."`, calls `shutdown_local()` (best-effort teardown — see below), builds a fresh session, and retries the call exactly once (`llm.py:132-140`). A second failure propagates uncaught.
- **Hang (process alive, unresponsive):** **not detected at all.** No code path times out `_recv()`. This is the deadlock hazard: a wedged NPU driver or an infinite loop inside `_generate_ort`'s token loop (`_interlock_worker.py:274-342`, which has no `max_tokens`-independent escape other than hitting `eos_id` or the loop bound) hangs `chat_completion(backend="local")` forever, with cleanup (`shutdown_local()`) never reached because the hang happens *before* that call returns.
- **Normal process exit (reaping):** `shutdown_local()` (`llm.py:44`) is called from `cli/dispatch.py:main_sync`'s `finally` block (`dispatch.py:136-138`) on every CLI run, success or failure. It calls `RyzenSession.__exit__`, which sends `{"cmd": "quit"}` and `self._proc.wait(timeout=5)`; on any exception (including that 5s wait expiring) it falls back to `self._proc.kill()` (`interlock.py:143-156`). The temp `io_dir` is removed with `shutil.rmtree(..., ignore_errors=True)` regardless of outcome.
- **Abnormal parent death (SIGKILL/taskkill/crash):** `finally` never runs, so `shutdown_local()` is never called. **known rough edge:** nothing in this codebase wraps the `ryzenai-python` child in a Windows job object or equivalent, so an orphaned worker is not guaranteed to die with its parent — `unverified` whether the OS, `pixi`, or the RyzenAI SDK itself provides that cleanup, but no such mechanism exists in `interlock.py`/`_interlock_worker.py`. An orphaned worker holds the NPU device claim and its `.npy` temp directory indefinitely.
- **Cross-process races:** since there is no on-disk lock, nothing stops a second `event_harvester` process (or a second `pixi run` invocation) from also calling `chat_completion(backend="local")` concurrently and spawning a second `ryzenai-python` worker against the same NPU device. Whether the VitisAI EP or the NPU driver itself rejects a second exclusive claim, queues it, or corrupts state under contention is `unverified` — outside what this codebase controls or tests.

## Verification

```powershell
# Confirms the chat-vs-vision model-selection asymmetry and the six
# documented vision fallback/warning behaviors (all mocked, no network).
pixi run pytest tests/test_llm_vision.py -v

# Confirms cfg.api_key is never forwarded to litellm.completion() in
# either wrapper (greps the two call sites for an api_key kwarg — expect no match).
rg -n "api_key" src/event_harvester/llm.py

# Confirms interlock.py has no on-disk lock representation anywhere
# in the package (expect no output relevant to Grok/browser locking).
rg -n "FileLock|flock|\.lock\b" src/event_harvester

# Confirms the real single-flight mechanism for uc/grok lives in uc.py
# and the proxy config, not in interlock.py.
rg -n "max_workers=1|worker_concurrency" "ext/universal_controller/uc_browser/llm_providers/uc.py" "litellm.config.yaml"
```
