# Session Report — a local model by default, and the harness that proved it

**Date**: 2026-08-13 · **Status**: ✅ LANDED · **the cache is refilled — 244 events, all local** · ⚠️ the OpenAI account has no credit
**Scope**: `/calendar` + nav + profile editor · sub-path proxy · mobile pass · the outage that erased 251 events · the extraction A/B harness · **qwen3-8b as the default model**
**Tests**: 1495 passed, 0 failed (1314 at session start) · **Lint**: 43, unchanged all session
**Follows**: [2026-08-12_the-calendar-and-the-page-nobody-could-find.md](2026-08-12_the-calendar-and-the-page-nobody-could-find.md)

---

## 1. The headline

**The extraction model is now a 5 GB local one.** `LLMConfig.model` defaults to `qwen3-8b` served through the mesh proxy, with `codex_sdk/gpt-5.6-luna` behind it as a fallback.

Measured over 150 identical messages, batched identically:

| | `gpt-5.6-luna` | `qwen3-8b` |
|---|---|---|
| events | 80 | **68 (90% recall)** |
| invented (not in baseline) | — | **0** |
| date / time / location / link filled | 96 / 90 / 96 / 95% | **98 / 93 / 100 / 97%** |
| latency p50 | 14.2s | **7.0s** |

It finds nine events in ten, invents none, fills every field *better*, and runs at half the latency on hardware you own.

### 1a. And then it did it for real

Job `…dcf5` finished at 19:30:43 UTC, **24m 19s**, on the full mailbox:

| | |
|---|---|
| events | **244**, against the 251 the cloud model produced on 2026-08-12 — **97%** |
| `llm_errors` / `llm_past` | **0** / 30 |
| fallbacks to `codex_sdk/gpt-5.6-luna` | **0** — every one of the 161 calls was `model= qwen3-8b` |
| date / time / location / link filled | **100 / 93 / 89 / 73%**; no event dated in the past |
| throughput | 161 serialised calls at ~9.0 s each ≈ 24 min |

So the 90% recall figure understated it, and the A/B harness was measuring the
right thing. **This is the number that matters**: not a 150-message sample
against a baseline, but the production cache, refilled by a 5 GB model on the
mesh, with the cloud fallback never once touched.

The 24-minute wall clock is also the evidence for §5b — it fits inside 30
minutes only because the message cache was warm. It would not have on a cold one.

---

## 2. The number that was wrong, and why

The first run said **29.5% recall** and that figure was meaningless. All six of `qwen3-8b`'s failed calls were:

```
ValueError: Requested tokens (2519) exceed context window of 2048
```

It is served behind llama.cpp with **`n_ctx=2048`**, and prompts reach ~2,500 tokens. It was refused 6 of 15 batches — and those six held **51 of the baseline's 78 events**, because the largest batches are the event-dense ones. The model was judged on work it was never shown.

**`batch_size=5` fixed it and cost nothing.** On identical input the baseline went 78 → 80 events; `qwen3-8b` went 18 → 68 and 29.5% → 90%. So halving the batch is not a trade, it is free. `LLMConfig.batch_size` is now 5 and that is load-bearing, not cosmetic.

**Raising `n_ctx` to 8192 remains the better fix** — it would allow batches of 10 again and give headroom for the `max_tokens=4096` generation budget. Not blocking.

---

## 3. Can the prompts be shortened?

Measured over the 817 messages a real harvest sends, in its real batches:

| | p50 | p95 | max | over 2048 |
|---|---|---|---|---|
| before | 1,502 | 2,062 | 2,219 | 7 of 82 |
| after `chat_label` | 1,502 | 2,056 | 2,141 | 5 of 82 |

A web source's `channel` is the **event's full URL with tracking parameters** — 39–64 tokens each, and since every URL differs, a ten-message web batch became ten one-message groups paying ten headers. `chat_label` collapses them to host + first path segment.

**Honest value: 2.2%.** On the one batch I first inspected, headers were 26% of the prompt; across all 82 they are not. I nearly reported the 26%.

**Do not truncate content further.** `build_prompt` derives its date hints from the same 400-char slice it sends, so a 300-char cap saves 9% and costs **5.5% of the pre-resolved dates** that make a small model viable in the first place.

---

## 4. What exists now that did not

| | |
|---|---|
| `/calendar` | month grid, 247 KB against the list's 1,562 KB, same `_prepare` projection, written in the same pass |
| `outputs/nav.py` | one nav bar across all three pages — `/admin` had shipped with **nothing anywhere linking to it** |
| `/admin` profile editor | closed the last gap in "a profile creater that shows different searchs" |
| `training/eval_extraction.py` | the A/B harness — identical batches, real parser, recall via `events_match` |
| `llm_errors` reject bucket | an outage is not an empty night (§5) |
| `LLMConfig` fallback chain | `api_base`, `verify_ssl`, `fallback_*`, `batch_size` |

---

## 5. The day's worst hour, and what it taught

**I restarted the site service eight times to pick up CSS changes. The service harvests on start.** Eight full harvests of 20,716 messages against a paid API in ninety minutes, for stylesheet edits. Between 16:09 and 16:12 the OpenAI account ran out of credit; the 16:12 and 16:13 harvests extracted **0 events and overwrote a cache holding 251**, unrecoverably.

`site --build-only --no-harvest-on-start` is what a stylesheet change needs.

**The defect it exposed is fixed.** A failed LLM call filed its messages under `llm_no_events` — "the model read these and found nothing" — so 814 consecutive 429s reported `ok: true, events: 0`. Now:

* `llm_errors` is its own bucket in both backends, labelled *"LLM UNREACHABLE (never asked)"*, threaded through the CLI, the Flet UI and `rejects.ini`.
* `_save_latest_events` **refuses to replace a non-empty cache with an empty result when the model was never reached**. Both conditions together: a genuine quiet night still writes its zero, a partial outage still writes what it got.
* System Overview axiom 3 gains a clause: **a drop attributed to the wrong stage is worse than an unattributed one**, because it reads as a finding.

---

## 5a. `config.py` validated nothing about the LLM

`LLMConfig.is_configured` returned `bool(self.model)`, and `model` has always had a non-empty default — so it was **`True` for every config ever constructed**. `validate_config` never warned, and the pipeline went on to make calls that could not succeed.

That was survivable while the default was `gpt-4o-mini`, which at least names a real model at a well-known provider. It stopped being survivable the moment the default became `qwen3-8b`, **which exists only behind a particular endpoint**: forget `LLM_API_BASE` and it is sent to OpenAI, which has never heard of it, and you get a 404 mid-harvest rather than a warning at startup.

Now checked, each naming the variable to set:

| | |
|---|---|
| `is_configured` | a model **and** something to reach it with — a key, or an endpoint that needs none |
| `needs_endpoint` | a bare non-`gpt-*` name with no `api_base` is going to OpenAI and will be rejected |
| `fallback_is_configured` | a fallback that inherits no usable key **fails exactly when it is needed** — on this deployment the local-models key cannot reach `codex_sdk/*` |
| TLS | reports when verification is off, and for which endpoint |

**Deliberately not warned: an absent fallback.** One model is a valid configuration, and a warning that fires on every run is one the reader learns to skip past — which costs more than it saves the first time something is genuinely wrong. I wrote that warning, saw it fire on a healthy config in the test suite, and removed it.

On the live `.env` the only warning is the TLS one, which is accurate and expected.

## 5b. The first run on the new default was killed by a clock

Restarting both services (the site was 5 commits stale, and the MCP backend owns the runner) queued a harvest that **failed after 30 minutes with 148 of 161 batches done and zero fallbacks**:

```json
"state": "failed",
"progress": {"stage": "extract"},
"result": {"error": "exceeded 1800s wall clock"}
```

It was working. It ran out of clock ~13 batches from the finish. The constant's own comment explained why it was wrong — *"A full harvest measured 320 s on 2026-07-31"* — and **every term in that measurement had since changed**:

* the message cache was cold, so the run re-scraped 33 web sources with browsers before extraction began;
* `batch_size` went 10 → 5 for the local model's context window, doubling the call count to **161**;
* extraction moved to a local model answering in ~10 s which, on a single-slot server, **serialises** — 161 calls is ~27 minutes on its own.

Harvest + extract ≈ 32 minutes. Cut off at 30.

`JOB_TIMEOUT_SECONDS` is now 90 minutes and overridable via `EH_JOB_TIMEOUT_SECONDS`, because it is a function of the deployment's speed rather than of the code.

**Throughput is the real finding.** `_extract_events_cloud` runs 5 concurrent workers and gets no benefit from them: ~6 completions/minute is one request at a time. The backend serves **one model with one slot**.

> **A trap if you add slots.** In llama.cpp `-c` is the **total** KV cache, divided across `--parallel` slots — so `--parallel 4 -c 8192` gives each request 2048, which may be exactly where our 2048 came from. Four usable slots at our ~2,500-token prompts plus generation wants roughly `-c 32768 --parallel 4`. Size the total, not the per-slot figure.

Parallel slots turn ~27 minutes of extraction into ~7, and turn `batch_size=5` from a constraint into a free choice.

## 6. Method notes worth keeping

Four faults in my own measurement tools, three of which would have produced a confident wrong answer:

1. **`if events_match(a, b)`** — it returns `(is_match, score)`, and a tuple is always truthy. The agreement metric scored **100% recall against any candidate whatsoever**.
2. **The eval sampled the head of the scored queue** — structured listings that already carry date, time and link. A 3-batch pilot scored 100% on every field for both models, answering a question nobody asked. It now strides across the whole 817.
3. **"3/3 parsed" when all three calls errored** — the one number you would read to decide a run was sound.
4. **`parse_failures` counted any reply yielding no events**, so a batch of five with nothing in it scored as malformed. At `batch_size=5` that made both models look three times worse than they were.

And two secret-handling slips, both fixed:

* Adding `LLM_ZT_KEY` as a key fallback let two unit tests find a real credential — one **made live API calls** through the guard it was testing, the other printed a working key into its assertion message. An autouse fixture now clears every key variable.
* **`.env` alone does not ignore `.env.bak-*`**. My backup sat untracked-but-not-ignored, one `git add -A` from being published. `.gitignore` now covers `.env.*` with an exception for the example.

---

## 7. State at handoff

| Fact | Value |
|---|---|
| Tests | **1495 passed**, 0 failed |
| Lint | 43, unchanged all session |
| Git | `origin/main` is at `343797e`; **`108ba59` (the timeout fix) is unpushed**. Tree clean |
| Services | both restarted onto current code, 2026-08-13 ~19:06 UTC |
| **Harvest** | job `…dcf5` **done** at 19:30:43 UTC — 244 events, 0 fallbacks, 24m 19s (§1a) |
| Event cache | **244 events**, `data/.mcp_events.json`, all extracted by `qwen3-8b` |
| Message cache | warm — the failed run repopulated it, so the re-run skips the scrape |
| Not mine | the 8 `.dvc` paths, still untracked and uncommitted |
| Secrets | `.env` has a new appended block; backup at `.env.bak-20260813`, now gitignored |

---

## 8. Open, in priority order

0. ~~Check job `…dcf5`~~ — **done: 244 events, 0 fallbacks, 24m 19s. See §1a.** The
   count is 97% of the cloud model's 251 and the cache is refilled.
1. **`llm_no_events` came back 0, and that cannot be right.** 775 messages
   reached the model in 161 batches and produced 244 events, so ~530 messages
   were read and yielded nothing — yet none were filed. The cause is
   `analysis.py:513`: a batch is filed under `llm_no_events` only when it
   produces **zero** events, so at `batch_size=5` a batch yielding one event
   leaves its other four unattributed. That was invisible at `batch_size=10`
   for the same reason it is visible now — smaller batches are less often
   wholly empty. **This is an axiom-3 hole, not an outage**: the messages are
   dropped correctly, they are just not counted anywhere, and the rejects no
   longer sum to the total. Fix by attributing per message (the events carry
   `_source_ids`), not per batch.
   **Designed 2026-08-14** in [`01 - Design/04 - Decomposing the Pipeline.md`](../01%20-%20Design/04%20-%20Decomposing%20the%20Pipeline.md),
   after an exchange with 206-1002 over the mesh exchange. The short version:
   derive drop counts at the stage boundary from `in` and `out` rather than
   letting a stage self-report, and make the accounting unit the message —
   the same unit as the pipeline element — because a bucket written per batch
   cannot represent a partial batch. Roadmap 2.16.
2. **Parallel slots on the llama.cpp server** — the single biggest win available. ~27 minutes of extraction becomes ~7, and `batch_size=5` stops being a constraint. Mind the `-c` / `--parallel` interaction in §5b.
3. **A date-selection weakness in `qwen3-8b`, unquantified.** Given `Friday 2026-08-21` where `build_prompt` emitted `dates=2026-08-14,2026-08-21`, it chose **the 14th**; the fallback chose correctly. One example, not a measurement — but a wrong date still fingerprint-matches, so **neither the 90% recall figure nor the 244 would show this**. The live cache is at least not obviously wrong: 244 of 244 carry a date, none in the past, and the tail decays plausibly to 2027-05. Still worth a targeted date-accuracy eval before trusting it unattended.
4. **`n_ctx=8192` on the qwen3-8b deployment** — would allow `batch_size=10` again and covers the generation budget. Pairs with the slot change above.
5. ~~`phi-4-14b` / `phi-3.5-mini` untested~~ — **resolved as not-a-bug**: the backend runs one model. Nothing to chase.
6. **The OpenAI account has no credit** — `gpt-4o*` on the proxy fail with an upstream auth error. `claude_code/*` and `codex_sdk/*` work.
7. **`sources/venue_location.py` is still unwired** — unchanged for two sessions.
8. **`pixi run check-head` has side effects** — it printed source banners, attempted a web harvest and collided with the running service's Chromium profile. A checker that answers "does a bare checkout import?" should not open a browser.

## Verification

```powershell
pixi run test                                   # 1468 passed
pixi run lint                                   # 43, all pre-existing
pixi run event-harvester eval-extraction --baseline codex_sdk/gpt-5.6-luna `
  --candidate qwen3-8b --api-base https://central-dev.lan:4000/v1 `
  --candidate-api-key <local-key> --insecure --batches 15 --save data/ab.json
git status --short                              # clean but for the .dvc paths
```
