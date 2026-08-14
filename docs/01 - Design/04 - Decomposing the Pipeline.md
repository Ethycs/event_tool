# Decomposing the Pipeline — declared drops, and a funnel that adds up

**Status:** 🟡 IN FLIGHT — **sequencing steps 1–2 are built and tested** (`pipeline/filtering.py`, 31 tests); 3 is partly measured; 4–6 are proposed only. Every measurement below is real.
**Date:** 2026-08-14 · **Revised** the same day, after executing step 1 turned up an error in §8 — see §4.5
**Applies to:** `pipeline/orchestrator.py`, `pipeline/analysis.py`, and the four `Pipeline` callers
**Depends on:** `03 - Architecture/00 - System Overview.md` (axiom 1, axiom 3) · `05 - Standards/EH-STD-MSG-v0 — Message Record.md`
**Origin:** a design exchange with **206-1002** (`blackbox / structure_net research`) over the mesh exchange on 2026-08-14, about the stage pipeline in `gpt2_attack_notions`, `src/cat_scanner/pipeline/`. Their pattern, our risk profile — §2.

---

## The problem, stated as arithmetic

The pipeline is 96% a removal machine. Here is the harvest of 2026-08-13 19:30 UTC, `validated` — these are the recorded numbers of job `…dcf5`, not an estimate:

```
19,646  messages
   ↓  classifier   −13,126
 6,520
   ↓  caps              −0
 6,520
   ↓  reranker      −5,715
   805
   ↓  llm_past         −30
   775  reach the model
   ↓
   244  events          ← and llm_no_events reported 0
```

**775 messages went to the model, 244 events came back, and nothing was filed for the difference.** The reject counts no longer sum to the input, and no code path notices.

That is the second incident of the same family in three days:

| # | What happened | What it was recorded as |
|---|---|---|
| 1 | 2026-08-12: the API returned 429 for every batch | `llm_no_events` — *"the model read these and found nothing"*. The run reported `ok`, 0 events, and the empty result overwrote a cache holding 251 |
| 2 | 2026-08-13: a batch of 5 yielding 1 event | The other 4 filed nowhere. ~530 messages unattributed in one run |

Axiom 3 says every drop is attributable. **It is not, and the failure is invisible in the output** — which is the actual danger. A number that reads as a measurement and is structurally fixed is worse than a missing number, because a missing number prompts a question.

---

## 1. Hard constraints — non-negotiable

1. **One pipeline.** `harvest → filter → extract → match` lives only in `Pipeline` (axiom 1). This design adds no second execution path, no config-driven stage list, and no plugin registry.
2. **`Pipeline.run()`'s signature does not change.** Four callers depend on it: `cli/commands/harvest.py:62`, `cli/commands/serve.py:70`, `servers/mcp_server.py:366`, and the job runner behind the last of those.
3. **Cooperative Stop must keep working.** `await self._checkpoint()` between stages is what lets the job runner and the Flet UI stop a 25-minute harvest. Those boundaries are load-bearing and survive this change intact.
4. **No stage self-reports its drop count.** Counts are derived at the boundary from what went in and what came out.
5. **The accounting unit is the message**, the same unit as the pipeline element. Not the batch.

Constraints 4 and 5 are the two that our current code violates, and each maps to one incident above.

---

## 2. Where this came from, and what we did not take

206-1002 described a five-part pattern: a structural `Stage` protocol, per-stage I/O contracts, a fluent builder folding stages into one lazy generator, observability as a decorator, and a name→class registry with `from_config()`.

**We take two of the five**, and extend one of them with machinery they do not have.

Asked directly whether their contracts express *removal*, they answered no, and volunteered two things that are the most useful part of the exchange:

- `validate_contracts()` does `if contract is None: continue`, and their reductive stages declare no contract. **Only 9 of 28 stage modules declare one, so the stages that remove things are exactly the ones the validator skips.**
- Their `ObservableStage` maintains `_skip_count` and reports it in the metrics of every `complete` event. **Nothing ever emits a skip event.** It is initialized, reset, and reported — never incremented. Every run they have done reports `skip_count: 0` with full apparent authority, including runs where 90% of the stream was dropped.

Their conclusion, adopted here verbatim as constraint 4:

> Never let a stage self-report its drop count. Derive it at the boundary from the two things you can observe without the stage's cooperation, `in` and `out`. A counter a stage must remember to increment is a counter that will read zero on the path nobody exercised.

Their own caveat, which is worth recording because it sets how much of their design to trust: **their drops are lossy, not wrong.** A germ filtered out of a research scan is a missed finding in a run they re-execute at will. Ours is a message that silently never got attributed in production, on a nightly job nobody watches. Same hole, different consequence — our risk profile justifies machinery theirs has not been forced to build.

### Ran their audit here

`known rough edge` — the audit found no exact instance of their bug, and two things with the same *reading*:

| Check | Result |
|---|---|
| A reject bucket declared and never written | **None.** All six are written, in two different shapes: the filter buckets by assignment (`analysis.py:406, 414, 424, 438`), the three LLM buckets as a dict returned by whichever backend ran (`565` cloud, `706` local) and merged at `463`. That the two halves are written by different mechanisms in different scopes is itself part of why the sum was never checked |
| The backend-swap bug (`local` run reporting the cloud path's buckets) | **Already fixed.** `analysis.py:463` does `rejects.update(llm_rejects)`, a merge, not a replace |
| A channel that is structurally zero on the path that actually runs | **Two** — see §7 |

---

## 3. What "decomposable" has to mean here

Three different things get called decomposition, and they need different work:

| | Meaning | Today | Wanted |
|---|---|---|---|
| **Reuse** | Run `filter` without `extract` | ✗ — `skip_analyze` stops one stage too early | ✅ the whole point |
| **Re-entry** | Start from a cached artefact | ~ — `load_path` only | nice to have |
| **Substitution** | Swap a stage's implementation | ✗ | ❌ **explicitly rejected** — axiom 1 |

Only reuse is required. It has one immediate consumer: the source-onboarding funnel in `10 - Blueprints/01 - Adding a Web Source.md`, which wants "does this page have events?" answered by the classifier and reranker **without paying for the LLM**. Those two stages exist and are free; they are unreachable today because they live inside `extract_events_llm` (`analysis.py:402-424`), behind the stage that costs money.

`known rough edge`: the free funnel is informative in proportion to how many messages a page produces. A `raw` source yields **one message however dense the page**, so the classifier sees a single blob and the funnel says almost nothing. 12 of 33 configured sources are `raw`. The gate answers the easy cases well and the hard case not at all.

---

## 4. The design

### 4.1 A stage declares what it may drop

Parallel to the `produces` idea, and the piece 206-1002 does not have:

```python
@dataclass(frozen=True)
class StageContract:
    name: str
    cardinality: Literal["1:1", "reductive", "expanding", "buffering"]
    drops: tuple[str, ...] = ()      # enumerated permitted reasons
```

A stage may only drop for a **declared reason**, exactly as their stages may only append a declared computation. `reductive` with an empty `drops` is a contradiction and fails at construction.

### 4.2 The arithmetic happens at the boundary, in the wrapper

The wrapper sees the input and the output. It counts both without trusting anyone:

```
assert sum(dropped_by_reason.values()) == n_in - n_out
```

The batch-of-5 case dies here immediately: 5 in, 1 out, reasons summing to 0, which is not 4 — so it raises instead of quietly under-attributing four messages. **This is the whole design in one line.** Everything else is scaffolding for it.

### 4.3 A contract-less stage is rejected, not skipped

The single most transferable thing from the exchange. `if contract is None: continue` is what exempted their least accountable stages from their only check. Ours must fail loudly on a stage that declares nothing, or the exemption reappears here the first time someone is in a hurry.

### 4.4 "Dropped by policy" and "never processed" must not collapse

These are different facts and one masquerading as the other cost 251 events. We already encode this distinction — `llm_errors` versus `llm_no_events`, added 2026-08-12 — but it lives in one hand-written branch in one backend. Generalize it: **every** reason carries which kind it is.

| Kind | Meaning | Example |
|---|---|---|
| `policy` | The stage looked and decided | `classifier`, `reranker`, `caps`, `llm_past` |
| `unreached` | The stage never got to look | `llm_errors` — an outage, not a finding |

An `unreached` drop anywhere in a run is grounds for refusing to overwrite a cache, which is currently a special case in `mcp_server._save_latest_events` rather than a property of the funnel.

### 4.5 The extract stage does not balance in messages, and cannot be made to

**Correction, 2026-08-14, found while executing step 1.** An earlier draft of §8 said the extract stage's attribution should be "rewritten to use `_source_ids`". That is wrong twice over, and the reason is worth recording because it changes what the fix is.

- **`_source_ids` is deliberately partial.** `analysis.attach_source_ids` matches only on a verbatim link or an author match, and *"ambiguity resolves to no id rather than a guess"* — it was built for Gmail labelling, where a mislabelled message writes to a real mailbox. Measured on the live cache: **95 of 244 events (39%) carry no `_source_ids` at all**, 88 of them web. That is the design working, not failing.
- **It points the wrong way.** It links an event back to a message — the *surviving* direction. Drop accounting needs the messages that produced nothing.

And the deeper problem: **messages in and events out are different types.** A batch of 5 producing 2 events does not tell you which 3 messages produced nothing — one message can yield two events. Per-message attribution of `llm_no_events` is *not derivable from the model's output*. The original per-batch implementation was not carelessness; the unit genuinely does not decompose.

So the extract stage is `cardinality: "expanding"` and its invariant is stated in the unit that *does* survive — messages consumed:

```
n_in == attributed + consumed_unattributable + unreached
```

| Term | Meaning |
|---|---|
| `attributed` | a link or author match pinned it to an event (`_source_ids`, the 61%) |
| `consumed_unattributable` | in a batch the model answered; which message yielded what is unknowable |
| `unreached` | `llm_errors` — never sent, or the call failed |

Events produced is a **separate number that does not participate in the message balance**. This is a smaller change than the doc originally proposed: no attribution rewrite, one new bucket, and the ~530 messages currently invisible become a named category instead of being pinned to a reason that cannot be computed. Naming the unknowable as unknowable is the honest version, and it still makes the sum checkable.

### 4.6 Terminal events carry a reason

206-1002's parting observation, and our failure family for the third time in one exchange: their `complete` event fires from a `finally`, so a cancelled run and a finished run emit **the same event shape** with different numbers, and nothing marks which is which. If our Stop lands 25 minutes into a harvest, the terminal record must say `cancelled` explicitly — otherwise a Stop is indistinguishable from a quiet night, which is exactly incident #1 wearing a third costume.

---

## 5. What this fixes, concretely

| Defect | What raises, and when |
|---|---|
| 775 messages in, 0 attributed | The `filter`/`extract` boundary check, at the end of the stage |
| A 429 storm filed as "no events" | `unreached` is a distinct kind; the sum still balances but the kind is wrong for a cache write |
| `caps: 0` unreadable (§7) | `cardinality` says whether the policy was applicable at all |
| Free funnel unreachable | `filter` is a stage you can run without `extract` |
| Stop indistinguishable from a quiet night | The terminal record carries a reason |

---

## 6. What we are not taking, and why

| Their piece | Why not |
|---|---|
| **The lazy generator fold** | Three of our stages are genuine barriers — `prioritize_with_caps` must see the whole set to rank, `rerank_messages(top_k=)` to take a top-K, `dedup_events` to deduplicate. They would buffer internally and *claim* to stream. Worse, the fold makes granularity per-item through the whole chain with **no inter-stage barrier**, and our `await` checkpoints are constraint 3. 206-1002's own advice: if inter-stage await points are load-bearing, the fold is the one thing not to copy |
| **The fluent builder** (~30 methods) | Right for a research tool composing novel chains. Our order is canonical and the only variation is *where you stop*. A list you can truncate is nearly all of the value |
| **The registry / `from_config`** | A plugin seam is precisely how a codebase with a one-pipeline axiom acquires a second pipeline without anyone deciding to build one |

---

## 7. Reading the two zeros

Today's run reported `caps: 0` and `llm_no_events: 0`. **One is true and one is a hole, and nothing in the output distinguishes them.**

- **`caps: 0` is legitimate.** The timer path passes unlimited caps — `apply_cli_defaults()` lives only in `harvest.py`, so the queue path leaves `caps.total` at the `-1` sentinel, `analysis.py:396` sets `cap_total = max(total, 1)`, and nothing is trimmed. The policy ran and dropped nothing. But the service path is the one that runs *every night*, so on that path this number is **permanently** 0 — a channel that is structurally zero on the only path in production. Their `_skip_count` with a different cause.
- **`llm_no_events: 0` is a hole**, per §0.

The `has_trained_models()` false branch at `analysis.py:430` has the identical property for `classifier` and `reranker`: no classifier ran, so the channel reads "dropped nothing".

`cardinality` plus a declared reason set is what makes these two legible without reading the source, because "not applicable" stops looking like "applied, found nothing".

---

## 8. Sequencing, in priority order

1. ~~**Split the filter stages out of `extract_events_llm`.**~~ ✅ **Done 2026-08-14** — `pipeline/filtering.py`. `filter_for_extraction` takes no `LLMConfig`; `analysis` re-exports the vocabulary so no caller moved. `validated`: replayed against job `…dcf5`'s own cache, the classifier reproduces **13,126 exactly** and caps **0 exactly**; the reranker differs by 64, of which **60 are provably the calendar** (it keeps only future dates and a day had passed — 60 lost, 0 gained, measured by resolving the same messages against both dates).
2. ~~**Add the boundary check to the filter stages.**~~ ✅ **Done 2026-08-14.** `StageCount`/`FilterResult.check` assert `n_in == n_out + sum(drops)` per stage and across the section. Raises under pytest, logs at ERROR and records in production (Open Question 2, resolved: an accounting bug must not cost a 24-minute harvest). `EH_STRICT_FUNNEL` forces either. 31 tests, of which the load-bearing ones prove the check *can fail*.
3. **Backtest the free funnel** — survived-to-LLM against events actually produced. `known rough edge`: **the correlation half is not cleanly answerable from the current cache.** 39% of events carry no `_source_ids` (§4.5), and the cache holds 734 web messages across 92 channel labels rather than 33 named sources. The funnel half *is* measured — per-channel keep rates, and **22 of 92 channels where the free stages keep nothing** — which is the signal the onboarding gate actually needs. Crediting events per source needs either a harvest that records source→event links, or matching on the free-text `source` string, which is fuzzy enough to be worth doing deliberately rather than by accident.
4. **Formalise `StageContract`** with `cardinality` and `drops`, and reject contract-less stages.
5. **Move the extract stage onto it.** Now smaller than originally written — see §4.5. One new bucket for the consumed-but-unattributable messages, not an attribution rewrite.
6. **Terminal reason on cancellation.** Small, independent, and can land any time.

Steps 1–2 landed at no architectural cost and are worth having even if 4–6 never happen. Step 3 delivered the funnel numbers but not the correlation.

---

## 9. Open questions

1. **Does the reject shape become normative?** `REJECT_BUCKETS` is currently a tuple in `analysis.py` consumed by the CLI, the Flet UI and `rejects.ini`. A declared reason vocabulary has a real claim on `05 - Standards/`, which would freeze it.
2. **What does the boundary check do in production — raise, or record?** Raising in a test suite is obviously right. Raising mid-harvest turns an accounting bug into a lost run, which is the failure mode we are trying to stop. Probably: raise under test, record-and-alarm in production. Not decided.
3. **Does `caps` survive at all?** Since 2026-08-11 the default is unlimited everywhere and filtering moved to the events page. A stage that is structurally inert on the production path may be better deleted than instrumented.
4. **Whether to tell 206-1002 what their audit turned up here.** They found a live bug in their own instrumentation by answering our question; the reciprocal finding is §7.

---

## Verification

```powershell
# The funnel numbers this document is built on
Get-Content data\.jobs\finished\j_20260813T190623914496_000000_dcf5.json | ConvertFrom-Json |
  Select-Object -ExpandProperty result | Select-Object -ExpandProperty rejects

# Every write to a reject bucket, in both of its shapes
Select-String -Path src\event_harvester\pipeline\analysis.py `
  -Pattern 'rejects\["|"llm_(past|no_events|errors)":'
```

Expect `classifier 13126 · caps 0 · reranker 5715 · llm_past 30 · llm_no_events 0 · llm_errors 0` against `total_messages 19646` and `events 244` — note `19646 − 13126 − 0 − 5715 − 30 = 775` reaching the model against `244` events, with nothing filed for the difference.

Expect the second command to return **8 lines across 6 sites**: four assignments at 406/414/424/438 inside `extract_events_llm`, and two two-line dict literals at 565 and 706 inside the cloud and local backends. Six buckets, two mechanisms, two scopes, no shared check.

**Then decide:** run sequencing steps 1–3 and stop at a measurement (cheap, delivers the onboarding gate), or commit to 4–6 as well (formalises the contract, and is the only path that makes incident #2 structurally impossible).
