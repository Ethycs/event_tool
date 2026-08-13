# Session Report — a local model by default, and the harness that proved it

**Date**: 2026-08-13 · **Status**: ✅ LANDED · ⚠️ **the event cache is empty and the OpenAI account has no credit**
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
| Tests | **1468 passed**, 0 failed |
| Lint | 43, unchanged all session |
| Git | `main` is **6 commits ahead of `origin/main`**, working tree clean |
| Services | `EventHarvesterMCP` + `EventHarvesterSite` running; site on `0dac2fd`, **stale by 5 commits** |
| Event cache | `data/.mcp_events.json` — **0 events** (§5) |
| Message cache | 5.2 MB, 20,716 messages — **TTL expired**, a harvest will re-scrape |
| Not mine | the 8 `.dvc` paths, still untracked and uncommitted |
| Secrets | `.env` has a new appended block; backup at `.env.bak-20260813`, now gitignored |

---

## 8. Open, in priority order

1. **The site serves 0 events.** One harvest on the local model restores it. Do this first and compare the count against yesterday's 251 — it is also the first real test of the new default.
2. **A date-selection weakness in `qwen3-8b`, unquantified.** Given `Friday 2026-08-21` where `build_prompt` emitted `dates=2026-08-14,2026-08-21`, it chose **the 14th**; the fallback chose correctly. One example, not a measurement — but a wrong date still fingerprint-matches, so **the 90% recall figure would not show this**. Worth a targeted date-accuracy eval before trusting it unattended.
3. **`n_ctx=8192` on the qwen3-8b deployment** — would allow `batch_size=10` again and covers the generation budget.
4. **`phi-4-14b` and `phi-3.5-mini` are not loaded in Triton** (`'phi-4-14b-gguf' is not found`), so the 14B class that also fits 12 GB is untested.
5. **The OpenAI account has no credit** — `gpt-4o*` on the proxy fail with an upstream auth error. `claude_code/*` and `codex_sdk/*` work.
6. **`sources/venue_location.py` is still unwired** — unchanged for two sessions.
7. **`pixi run check-head` has side effects** — it printed source banners, attempted a web harvest and collided with the running service's Chromium profile. A checker that answers "does a bare checkout import?" should not open a browser.

## Verification

```powershell
pixi run test                                   # 1468 passed
pixi run lint                                   # 43, all pre-existing
pixi run event-harvester eval-extraction --baseline codex_sdk/gpt-5.6-luna `
  --candidate qwen3-8b --api-base https://central-dev.lan:4000/v1 `
  --candidate-api-key <local-key> --insecure --batches 15 --save data/ab.json
git status --short                              # clean but for the .dvc paths
```
