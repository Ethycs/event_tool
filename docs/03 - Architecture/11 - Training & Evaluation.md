# Training & Evaluation

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/training/__init__.py`, `src/event_harvester/training/label.py`, `src/event_harvester/training/eval_classifier.py`, `src/event_harvester/training/recruiter_score.py`, `src/event_harvester/cli/commands/classifier.py`, `src/event_harvester/cli/commands/recruiters.py`, `src/event_harvester/pipeline/classifier.py` · **Depends on:** `docs/00 - Theory/00 - Ranking, Gating and Event Identity.md`

This subsystem is the offline loop that keeps the actionable/noise classifier honest: LLM-labeled ground truth in, two `RandomForestClassifier` `.pkl` files out, loaded by the live pipeline at `pipeline/classifier.py:predict`. A second, unrelated tool in the same package — `recruiter_score.py` — grades Gmail recruiter emails with a pure heuristic (no model, no training). Every step that moves a file from "just produced" to "affects the running pipeline" is a manual step; nothing here retrains or reloads automatically.

**Hard constraints**

1. The runtime classifier loads from exactly two paths, resolved at import time by `pipeline/classifier.py:_find_classifiers_dir` walking `Path(__file__).resolve().parents` upward looking for a directory literally named `classifiers`: `EMAIL_MODEL_PATH = <found dir>/classifier_email.pkl` and `CHAT_MODEL_PATH = <found dir>/classifier_chat.pkl`. On this checkout that resolves to `c:\Users\jobhelp\event_tool\classifiers\classifier_email.pkl` and `...\classifier_chat.pkl` (verified present, 596 KB / 10.6 MB). `EVENT_HARVESTER_CLASSIFIERS_DIR` overrides the search.
2. **BUG (fixed, documented in-code as a cautionary tale):** the old path logic was a fixed `.parent.parent.parent` walk that landed on `src/classifiers` instead of `<repo>/classifiers`, so every trained model was silently invisible and `filter_actionable` fell through to "no models found → return everything unfiltered" — with only a log line nobody reads. The fix (upward directory search) is what's live now; the comment in `classifier.py:19-54` is a live warning against reintroducing a fixed-depth `.parent` chain. The sibling bug in the harvest message cache (`src/data` vs repo-root `data`) mentioned in commit `d606800` is the same class of mistake.
3. A trained model only takes effect once `joblib.dump` has written the `.pkl` to the path in constraint 1 **and** the process is restarted (or the in-memory `_model_cache` in `classifier.py:262` is otherwise invalidated) — the cache is a plain module-level dict keyed by string path, populated lazily on first `predict()` call and never invalidated on file change. A long-running `serve`/`mcp` process will keep using the model it loaded at first prediction even after you retrain.
4. `classifiers/*.pkl` is gitignored (`.gitignore:48`) — the trained models are local-only artifacts, never committed. There is no model registry or versioning; "the model" is whatever `.pkl` happens to sit in `classifiers/` on a given machine.
5. `models/` (repo root) is empty on this checkout and is **not** part of this subsystem — it's the mount point for the local ONNX LLM backend (`interlock.py`, `LLMConfig.model_path`) and the reranker's cross-encoder deploy target (`pipeline/reranker.py:25`, `/data/models/ms-marco-MiniLM-L-6-v2`). Do not confuse it with `classifiers/`.

## Domain

Two independent offline tools live under `src/event_harvester/training/`:

| Module | Domain | Trains a model? |
|---|---|---|
| `label.py` | LLM auto-labeling of raw messages as actionable(1)/noise(0), ground truth for classifier train/eval | no — produces labels only |
| `eval_classifier.py` | Scores the *deployed* classifier + downstream keyword heuristic against LLM ground truth, simulates the full pipeline funnel | no — read-only evaluation |
| `recruiter_score.py` | Heuristic (regex/keyword) scoring of Gmail recruiter emails 0-100, with optional LLM refinement for borderline scores | no — stateless heuristic, no `.pkl`, no training loop |

`pipeline/classifier.py` itself is not part of "training" proper (it's the runtime load/predict path plus the `train()`/`_train_one()` functions the CLI calls into) but is covered here because the load path is the contract this whole loop exists to satisfy.

## Runs at

All of it is CLI-invoked, on demand, by a human. Nothing here runs on a schedule or is triggered by the live pipeline.

| Command | Runs at | Invoked by |
|---|---|---|
| `pixi run event-harvester classifier train ...` | manual, whenever you want to retrain | operator |
| `pixi run event-harvester classifier eval ...` | manual, after training or to audit drift | operator |
| `pixi run event-harvester recruiters grade` | manual, or via `harvest --grade-recruiters` (same code path, `recruiters.py:_run_recruiter_grading` shared by both) | operator |
| `pixi run event-harvester recruiters reparse REPORT.md` | manual, interactive terminal session | operator |

## The label → train → eval → deploy loop (numbered procedure)

| # | Command | Input artifact | Output artifact | What it changes at runtime |
|---|---|---|---|---|
| 1 | `pixi run event-harvester classifier train --out-labels data/labels.json` (no `--in-labels`) | live harvest (`harvest_messages`) across configured sources | `data/labels.json` — every harvested message + LLM-assigned `"label": 0\|1` | nothing yet (this is the labeling sub-step) |
| 1b | **manual step:** open `data/labels.json`, spot-check/hand-correct `"label"` values | `data/labels.json` | corrected `data/labels.json` | nothing yet |
| 2 | `pixi run event-harvester classifier train --in-labels data/labels.json` | `data/labels.json` | `classifiers/classifier_email.pkl`, `classifiers/classifier_chat.pkl` (or a single file at `model_path` if `train()` is called with one — not exposed via CLI, library-only path) | **nothing until step 4** — files are written but the running process's `_model_cache` (and any already-running `serve`/`mcp`/`watch` process) won't pick them up |
| 3 | `pixi run event-harvester classifier eval --labels data/labels.json --out-samples data/eval_samples/` | `classifiers/*.pkl` (loaded via `predict()`) + a labels file (can be the same file used to train — no held-out split is enforced by the CLI, see Failure modes) | stdout report; `data/eval_samples/0{1..5}_*.json` | nothing (read-only) |
| 3b | **manual step:** review `data/eval_samples/05_after_heuristic_dropped.json` etc., set `manual_label` fields | `data/eval_samples/*.json` | corrected sample files | nothing directly — these are for eyeballing, not re-fed automatically |
| 4 | **manual step:** restart the process(es) that call `predict()`/`filter_actionable()` (`serve`, `watch`, `mcp`) | new `classifiers/*.pkl` | — | live pipeline now filters using the retrained model |

There is no automated retrain-on-schedule, no CI gate on eval metrics, and no code path that reloads `.pkl` files into a running process — step 4 is entirely on the operator, and nothing in the codebase enforces it happens.

### 1. Label (`training/label.py:label_messages`)

- Batches messages (`batch_size=20` default) into one LLM prompt per batch, via `event_harvester.llm.chat_completion` using whatever backend `cfg.llm` (an `LLMConfig`) points at — always litellm regardless of the configured backend, per the comment at `label.py:82-83` ("one-time bulk operation where speed and accuracy matter more than cost").
- System prompt (`label.py:14-42`) defines EVENT vs NOISE with concrete examples; the LLM replies in INI format (`[Message.N]` / `label = E|N`), parsed by `event_harvester.utils.parse_llm_ini`.
- **Failure mode:** if a batch's LLM call throws, every message in that batch is defaulted to `label=1` (actionable) — `label.py:93-98`. This is a deliberate "fail open" choice (missing an event is worse than one noisy label surviving) but it means a labels file produced during an LLM outage silently over-labels a chunk as actionable with no marker distinguishing it from a real LLM verdict. `label_map.get(j, 1)` at line 116 does the same fail-open default per-message if the INI parse is missing an index.
- Output: `list[dict]` = input message dict + `"label"` key (1 or 0). Written to disk by the CLI layer, not by `label_messages` itself.

### `labels.json` schema (verified against `data/labels.json` on disk)

```json
{
  "platform": "gmail",
  "id": "19d1412afcb50de1",
  "thread_id": "19d1412afcb50de1",
  "timestamp": "2026-03-22T05:44:20+00:00",
  "author": "AMD Registration <account.help@amd.com>",
  "channel": "INBOX",
  "content": "Please activate your AMD account\n...",
  "is_read": true,
  "is_sent": false,
  "label": 1
}
```

It's a flat JSON array of these objects — the raw standard message dict (fields vary slightly by platform: chat messages carry `pinned`/`author` instead of `is_read`/`is_sent`) plus one added `"label"` int. `classifier.py:_classifier_train` strips `"label"` back off (`{k: v for k, v in m.items() if k != "label"}`) before feature extraction, and the label list is passed separately to `train()`.

Three real labels files exist in `data/` on this checkout: `labels.json` (65 KB), `labels_all.json` (3.7 MB), `labels_events.json` (2.75 MB, the one referenced in the `ACTIONABLE_THRESHOLD` sweep comment in `pipeline/classifier.py:247-258` — "2347 chat messages, 125 true events"). None of these are the *current* held-out set by construction; they're just whatever's been accumulated locally. `data/*.json` is gitignored except `web_sources.json` (`.gitignore:45,51,59`), so none of this ships with the repo — a fresh clone has no labels at all and must run step 1 from scratch.

### 2. Train (`pipeline/classifier.py:train` / `_train_one`)

- `classifier.py:_classifier_train` (CLI layer) either loads `--in-labels` or harvests+labels fresh, optionally writes `--out-labels`, then always calls `train_classifier(msgs, labels)` — training happens on every `classifier train` invocation, labeling is the only optional part.
- `pipeline.classifier.train()` splits messages by `platform == "gmail"` vs everything else, training two independent `RandomForestClassifier(n_estimators=100, random_state=42, class_weight="balanced")` models via `_train_one`, one per platform group, using `extract_email_features` / `extract_chat_features` respectively (feature lists at `classifier.py:80-133` — e.g. email: `length`, `has_unsubscribe`, `is_noreply_sender`, `html_density`; chat: `has_mention`, `has_everyone`, `is_bot`, `is_pinned`).
- **known rough edge:** training reports metrics on the *training set itself* (`clf.predict(X)` right after `clf.fit(X, labels)`, `classifier.py:217-221`) — this is a sanity-check print, not a held-out accuracy number. Don't read the "Classification Report" printed during `train` as generalization accuracy; that's what `classifier eval` is for.
- Model artifact format: `joblib.dump({"model": clf, "feature_names": feature_names}, model_path)` — a dict with the fitted sklearn estimator and the ordered feature-name list used to rebuild the feature vector at predict time (`classifier.py:235`). `_load_model` (`classifier.py:265-272`) does `joblib.load(path)` and caches the dict in `_model_cache[str(path)]` forever (process lifetime).
- If either `email_msgs` or `chat_msgs` is empty, only the non-empty side's model is (re)trained — the other platform's existing `.pkl` on disk is untouched, meaning a `classifier train` run over an all-Gmail labels file silently leaves the chat model stale.

### 3. Eval (`training/eval_classifier.py:run_eval`)

Refuses to run if `has_trained_models()` is false (no `.pkl` at all — legacy `classifier.pkl` counts too). Loads a labels file, strips labels to get eval messages + a parallel truth list, then:

**Metrics reported (all printed to stdout, none persisted structurally beyond the `--out-samples` dump):**

| Section | What it measures |
|---|---|
| Per-classifier `sklearn.classification_report` (email group by `platform=="gmail"`, chat group by `platform in ("discord","telegram")`) + confusion matrix | precision/recall/F1 of the deployed `.pkl` models against the labels file, split by which model handled each message |
| Per-chat-platform breakdown (only if >1 chat platform present in the data) | same report, further split Discord vs Telegram |
| Pipeline funnel simulation | message counts surviving each stage in order: total → after Gmail read/sent filter (`sources.filter_read_sent`) → after classifier (`pipeline.classifier.filter_actionable`) → after keyword heuristic (`pipeline.analysis.prioritize`) → "to LLM"; plus a parallel events funnel via `pipeline.weights.extract_events`/`prefilter_events` |
| Actionable-message recall through the funnel | how many of the LLM-labeled actionable messages survive each stage, ending in an explicit `X/Y = Z% recall` figure |
| Keyword heuristic accuracy | `classification_report` scoring `prioritize()`'s keep/drop decision against LLM labels, independent of the classifier's own accuracy |
| List of actionable messages the heuristic dropped | up to 20 printed inline, for manual inspection |

**Sample dump format** (`--out-samples DIR`, `_save_eval_samples`, `eval_classifier.py:221-276`): five files, one per pipeline stage, each a JSON array capped at 500 items (`random.sample(..., n=500)` with `random.seed(42)` for reproducibility when the stage has more than 500 messages):

```
01_all_messages.json
02_after_classifier_kept.json
03_after_classifier_dropped.json
04_after_heuristic_kept.json
05_after_heuristic_dropped.json
```

Each item: `{id, platform, author, timestamp, channel, content (truncated 500 chars), llm_label, llm_label_str, classifier_pred, heuristic_kept, manual_label: null}`. Verified against `data/eval_samples/02_after_classifier_kept.json` on disk — matches exactly. `manual_label` is always written `null` by the tool; filling it in is entirely a **manual step**, and nothing in the codebase reads `manual_label` back — it's for human review only. The eval CLI's printed hint now reads "Then retrain: pixi run event-harvester classifier train --in-labels &lt;corrected-file&gt;" (`cli/parser.py:287`). *Fixed 2026-07-29: it used to name a top-level `--load-labels` flag that does not exist in the parser.*

**Failure mode:** `run_eval` doesn't enforce that `--labels` is a held-out split distinct from what trained the model — evaluating on the same file used for training will overstate accuracy. Splitting is entirely the operator's responsibility (no `train`/`eval` split flag exists anywhere in this loop).

## `classifier` CLI (`cli/commands/classifier.py`, `cli/parser.py:273-310`)

```
pixi run event-harvester classifier train [--in-labels FILE] [--out-labels FILE] [--days N] [--only/--skip PLATFORMS]
pixi run event-harvester classifier eval --labels FILE [--out-samples DIR]
```

- `train` with neither `--in-labels` nor `--out-labels`: harvests, labels via LLM, trains, and **discards the labels** (no `--out-labels` means nothing is written to disk) — every run re-harvests and re-labels from scratch with real LLM calls. Cheap to lose track of cost here if run repeatedly.
- `eval --labels` is `required=True` in argparse; there is no default labels path.

## `eval-extraction` — A/B the extraction model · **added 2026-08-12**

`training/eval_extraction.py`, `cli/commands/eval_extraction.py`. Answers "could a much weaker model do the extraction?" with a measurement rather than an opinion. Distinct from `classifier eval`, which asks whether the *classifier* is right about what to send — a different question about a different stage.

```
pixi run event-harvester eval-extraction \
  --baseline openai/gpt-5.4-mini-2026-03-17 \
  --candidate ollama/qwen3:8b \
  --api-base https://central-dev.lan:4000/v1 \
  --batches 20 --save data/ab.json
```

Both models get **identical batches** — taken from `data/.message_cache.json`, ordered by `score_message` so they are the head of the real queue rather than a random draw, and deterministic by construction. `temperature=0`. A difference is then the model, not the sample.

Replies are parsed with the pipeline's own `_parse_event_ini`. A harness that read output more leniently than production does would flatter a weak model exactly where it is weakest.

| Reported | Why it is there |
|---|---|
| events extracted | the headline, and on its own it rewards hallucination |
| **batches that failed to parse** | a malformed reply costs all 10 messages silently — the failure mode that matters most |
| date / time / location / link fill rates | `TBD` does not count. An event with no date never reaches the calendar page |
| **agreement** — recall of the baseline's events | matched with `events_match`, so "the same event" means what it means to the deduper |
| candidate-only events | counted and surfaced, never credited: they are either finds or inventions and the harness cannot tell |
| latency, tokens | throughput at 82 calls/harvest |

**Why the workload makes this worth asking** (measured 2026-08-12 against the real cache):

| | |
|---|---|
| Messages reaching the LLM | **817 of 20,716 — 3.9%**, ~82 calls per harvest |
| Prompt size | p50 1,286 · **p95 1,863** · max 2,043 tokens. No long-context requirement anywhere |
| Arriving with a **pre-resolved ISO date** | **32%** — `build_prompt` runs `_resolve_date` before the model sees anything |

Relative-date arithmetic, the thing small models are worst at, has already been lifted into Python. What remains is selection and transcription into INI.

> **A measurement tool that is wrong is worse than none.** The first version of `agreement` called `if events_match(a, b)` — which returns `(is_match, score)`, and a tuple is always truthy, so it scored **100% recall against any candidate whatsoever**. Caught by `tests/test_eval_extraction.py::TestAgreement`, which is why that test exists.

### First run — `codex_sdk/gpt-5.6-luna` vs `qwen3-8b`, 2026-08-13

15 stratified batches, 150 messages, through the mesh proxy at `central-dev.lan:4000`.

| | baseline | qwen3-8b |
|---|---|---|
| events | 78 | 18 |
| **call errors** | 0 | **6 of 15** |
| unparseable batches | 2 | 2 |
| date / time / location / link filled | 99 / 92 / 99 / 96% | 100 / 72 / 94 / 83% |
| latency p50 | 21.9s | **6.1s** |
| recall vs baseline | — | 29.5% |

**The 29.5% is not a quality measurement.** All six errors were the same thing:

```
ValueError: Requested tokens (2519) exceed context window of 2048
  llama_cpp/llama.py(1300): _create_completion
```

`qwen3-8b` is deployed behind llama.cpp with **`n_ctx=2048`**, and our prompts run to ~2,500 tokens on the larger batches. The model was never shown 6 of the 15 batches — and those six held **51 of the baseline's 78 events**, because the biggest batches are the event-dense ones. This is a deployment setting, not a model limit; Qwen3-8B natively handles far more.

**Like-for-like on the 9 batches both models answered: 27 events vs 18 — 67%.** That is the honest headline, and it is a real gap rather than a config artefact. Format compliance was identical (2 unparseable each), `date` fill was perfect, and it ran **3.6× faster**.

**Before re-running, raise the context window.** `n_ctx` must exceed prompt + generation: prompts reach ~2,500 tokens and `extract_events_llm` asks for `max_tokens=4096`, so **`n_ctx=8192`** is the setting to use. At 5 GB of weights on a 12 GB card there is ample room, including for the 5 concurrent batches `_extract_events_cloud` runs.

`unverified` — no valid quality comparison exists yet. The numbers above measure a 2048-token deployment, not Qwen3-8B.

### Can the prompts be shortened? — measured 2026-08-13

Over the 817 messages a real harvest sends, in its contiguous batches of 10, including the 216-token system prompt:

| | p50 | p95 | max | over 2048 |
|---|---|---|---|---|
| before | 1,502 | 2,062 | 2,219 | 7 of 82 |
| after `chat_label` | 1,502 | 2,056 | 2,141 | 5 of 82 |

**`chat_label` is worth 2.2%, not the 26% one batch suggested.** A web source's `channel` is the event URL with tracking parameters — 39–64 tokens each — and because every URL is distinct, a 10-message web batch became ten one-message groups paying ten headers. On one stratified batch that was 486 of 1,881 tokens. Across all 82 real batches it is **2,459 tokens per harvest, and 78 off the worst case**. Worth having and nearly free; not a solution.

**`batch_size` is the lever that works.** It is the caller's choice, not a property of the prompt:

| batch_size | calls | p50 | max + system | fits 2048 |
|---|---|---|---|---|
| 10 | 82 | 1,286 | 2,141 | 77 of 82 |
| 8 | 103 | 1,022 | 1,771 | **all** |
| **5** | 164 | 667 | **1,297** | **all** |
| 4 | 205 | 555 | 1,078 | **all** |

At 5 every call fits with room to generate. The cost is the system prompt paid twice as often — about +18k tokens per harvest, ~15% — and twice the requests.

**Truncating content further is a bad trade.** Content is 58–74% of a prompt, but `build_prompt` derives its date hints from the same 400-char slice it sends, so cutting the slice cuts the hints:

| cap | saved | messages losing their pre-resolved date |
|---|---|---|
| 300 | ~9% | 45 of 817 (5.5%) |
| 250 | ~15% | 54 of 817 (6.6%) |
| 200 | ~23% | 66 of 817 (8.1%) |

Those hints are why a small model can do this job at all. Do not spend them for 9%.

**Unmeasured opportunity:** web message `content` repeats itself — the observed shape is `title … date: … time: … location: … link: … <title again>`. That duplication is built by the web source, not by `build_prompt`, and content is the largest share of every prompt.

## `recruiters` CLI (`cli/commands/recruiters.py`, `cli/parser.py:238-271`) — why it exists separately

`recruiter_score.py` is **not** part of the classifier train/eval loop — it has no `.pkl`, no `train()`, and is a pure rule-based scorer plus an optional LLM refinement pass for borderline cases. It exists to solve a different problem than "is this an event": triaging Gmail recruiter spam (staffing-firm mass blasts vs. genuine, personalized outreach) so the operator doesn't have to read every recruiter email by hand.

```
pixi run event-harvester recruiters grade [--days N] [--auto-trash] [--obsidian] [--no-analyze]
pixi run event-harvester recruiters reparse REPORT.md
```

| Step | What happens |
|---|---|
| `recruiters grade` | Harvests Gmail only (`no_discord/no_telegram/no_signal/no_web=True`), fetches full bodies via `sources.fetch_full_bodies`, scores each with `recruiter_score.grade_email` (base score 50, deltas from sender/subject/body regex rules — staffing-firm domain list `_STAFFING_FIRM_DOMAINS`, template-opener phrases, meeting-request phrases, calendar-link domains, `_QUALITY_COMPANIES` allowlist, SF/Bay-Area + onsite regex, salary regex), prints via `print_recruiter_grades`, and if `--obsidian` writes a markdown report (`outputs/obsidian.py:write_recruiter_report`) to `cfg.obsidian_recruiters_dir` |
| score bands | `_action_for_score`: `<=20` trash, `21-45` ignore, `46-65` review, `>=66` respond (`recruiter_score.py:298-305`, tested exactly at these boundaries in `tests/test_recruiter_score.py:TestActionForScore`) |
| LLM refinement | Only for scores in `[30, 60]` (`llm_threshold` default) and only if `cfg.llm.is_configured` — `_llm_refine_borderline` re-scores via `chat_completion`, INI-parsed the same way as `label.py`, and **overwrites** (not blends) the heuristic score if a valid response comes back (`recruiter_score.py:453`) |
| `--auto-trash` | any grade with `action == "trash"` gets `sources.gmail_trash` called on it immediately — **irreversible-ish** (moves to Gmail Trash, not a hard delete, but happens with no per-item confirmation) |
| `--obsidian` markdown | `outputs/obsidian.py:write_recruiter_report` — action tiers become checkbox lists; `trash` items are pre-checked/struck-through, `respond`/`review` are open checkboxes with `[Open in Gmail]` / `event-harvester://trash/{id}` links |
| `recruiters reparse REPORT.md` | `outputs/obsidian.py:reparse_recruiter_report` — regex-parses the markdown (`_ITEM_RE` matches only `#respond`/`#review` tagged items, i.e. trash/ignore items from a prior grade run are **not** revisited here), extracts the Gmail message ID from an embedded `mail.google.com/mail/u/0/#all/{id}` URL, then interactively prompts per item: `[o]pen [r]eply [t]rash [s]kip [q]uit`, rewriting the checkbox to `[x]` in place on reply/trash. **manual step** by design — this whole command is the human-in-the-loop action layer, not automation |
| Shared entry point | `harvest --grade-recruiters` calls the exact same `_run_recruiter_grading` helper (`cli/commands/harvest.py:114-116`), so `recruiters grade` and a flag on the main harvest run are two front doors to identical logic |

`recruiter_score.py` has no persisted state or artifact at all — every run recomputes scores from scratch from the regex/keyword tables baked into the module. There's no drift-over-time concept here the way there is for the trained classifier; "retraining" it means editing the Python constants (`_STAFFING_FIRM_DOMAINS`, `_QUALITY_COMPANIES`, etc.) and shipping a code change, not running a training command. `tests/test_recruiter_score.py` (17 tests, all read) covers domain extraction, each scoring function's sign, the four action-band boundaries inclusive, and two integration-style `grade_email` cases (obvious spam scores ≤20/trash, quality outreach scores ≥66/respond) — these tests double as the executable spec for the scoring thresholds.

## `models/` and `classifiers/` on disk — verified inventory

| Path | Contents (verified) | Belongs to this subsystem? |
|---|---|---|
| `classifiers/classifier.pkl` | 512,601 bytes — legacy single combined model, still checked for by `has_trained_models()`/`_LEGACY_MODEL_PATH` as a fallback when neither per-platform model exists | yes |
| `classifiers/classifier_email.pkl` | 609,657 bytes | yes — current email model |
| `classifiers/classifier_chat.pkl` | 11,074,009 bytes | yes — current chat model |
| `classifiers/.gitkeep` | empty, keeps the dir in git while `.pkl`s are ignored | — |
| `models/` | empty on this checkout | **no** — reserved for the local ONNX LLM backend (`interlock.py`, `qwen2.5-7b-int4.onnx`) and the reranker's cross-encoder deploy target (`pipeline/reranker.py`, `/data/models/ms-marco-MiniLM-L-6-v2`, deployed by an external `just model-deploy` per `pipelines/models/manifest.toml` — not exercised by anything in `training/`) |

## Failure modes summary

- ~~**BUG:** stale hint text in `eval_classifier.py:276` referencing a nonexistent `--load-labels` flag~~ — fixed 2026-07-29; it now prints `classifier train --in-labels FILE`.
- **BUG (historical, fixed):** fixed-depth `.parent` chain used to resolve `classifiers/`, silently pointed at `src/classifiers`, disabled filtering entirely with no visible error — see constraint 2. The upward-search fix in `_find_classifiers_dir` is what's deployed now, but any future refactor of `pipeline/classifier.py`'s location must not reintroduce a hardcoded `.parent.parent...` depth.
- Retrained `.pkl` files have **no effect on a running `serve`/`watch`/`mcp` process** — `_model_cache` never invalidates; a restart is required. Nothing in the CLI or docs currently reminds the operator of this.
- `classifier eval` does not enforce a train/eval split — self-evaluation is possible and silent.
- Empty label groups on `classifier train` leave the other platform's model stale without warning (only the platforms present in the labels file get retrained).
- `label_messages` fails open (defaults to actionable) on both whole-batch LLM errors and missing per-message parse results — a labels file built during a flaky LLM run can quietly over-label without any marker.
- `recruiters --auto-trash` performs Gmail trash actions with zero per-item confirmation, gated only by the heuristic score threshold.
- ~~`CLAUDE.md`'s pointer to `./pixi_guide.md` is dead.~~ Fixed 2026-07-29: the guide moved to `docs/04 - Reference/Pixi CLI Reference.md` and `CLAUDE.md` now points there.

## Verification

```
pixi run event-harvester classifier eval --labels data/labels.json --out-samples data/eval_samples/
```

Proves: both `.pkl` models load without error (or the command prints the red "No classifier models found" message and exits), prints per-platform precision/recall against `data/labels.json`, prints the full pipeline funnel with actionable-message recall percentage, and writes five sample JSON files to `data/eval_samples/` matching the schema documented above. A nonzero recall percentage and populated confusion matrices confirm the load path in `pipeline/classifier.py:EMAIL_MODEL_PATH`/`CHAT_MODEL_PATH` resolved to the real `classifiers/` directory rather than falling through to the "no models found, unfiltered" branch.
