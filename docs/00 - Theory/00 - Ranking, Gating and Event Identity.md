# A raw message survives four independent, hand-tuned gates before an LLM ever sees it, and two events are "the same" iff their structured-field match score is ≥ 2.

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/pipeline/weights.py`, `src/event_harvester/pipeline/classifier.py`, `src/event_harvester/pipeline/reranker.py`, `src/event_harvester/pipeline/event_match.py`, `src/event_harvester/pipeline/dedup.py`, `src/event_harvester/training/recruiter_score.py`, `src/event_harvester/training/eval_classifier.py`, `src/event_harvester/training/label.py`

**Executive summary:** A message is ranked/gated by a chain of independent scorers — regex gate → RandomForest classifier (`threshold=0.1`) → keyword `score_message` cap → optional cross-encoder reranker — none of which share a loss function or a probability space, so their scores cannot be added meaningfully and are used only for ordering/thresholding within each stage. Two events are judged identical by `events_match()` in `event_match.py`, a hand-built point system (title-word-overlap=1, date-equal=1, location-word-overlap=1, exact-link=2, match iff total ≥ 2) that has no learned component anywhere. The only trained model in this whole path is the two RandomForestClassifier instances (`classifier_email.pkl`, `classifier_chat.pkl`); every weight in `weights.py`, `reranker.py`, `event_match.py`, and `recruiter_score.py` is a hand-tuned magic number with no fitting procedure behind it.

---

## 0. Preliminaries / definitions

| Term | Meaning in this codebase |
|---|---|
| **message** | A dict with `content`, `timestamp`, `author`, `platform`, `channel`, optionally `pinned`, `is_sent` — the atomic unit scored by classifier/reranker/weights. |
| **event** | A structured dict with `title`, `date`, `location`, `link`, `time`, `notes` — produced downstream (by an LLM extraction step, not shown in these files) from one or more messages. |
| **gate** | A boolean pass/fail filter (regex gate in `weights.py`, classifier threshold in `classifier.py`). Gates drop items; they do not rank survivors. |
| **rank / score** | A real number used only to *order* items within a stage (e.g. `extract_links` score, reranker combined score). Scores from different stages are never summed into one global score in the code read here. |
| **fingerprint** | A normalized, hashable-by-comparison signature of an event (`event_signature()` in `event_match.py`) used for both within-run dedup and cross-run "have I seen this" lookups (`data/.event_fingerprints.json`). |
| **actionable** | Classifier's positive class (label `1`) — "this message needs a human/LLM look," as opposed to noise (`0`). |

Four independent scoring pipelines are documented here; they are **not** the same pipeline with different names:

1. `weights.py` — regex-based link/date/proximity scorers used for the keyword heuristic (`analysis.py:score_message`, `prioritize`) and as the reranker's date component.
2. `classifier.py` — sklearn `RandomForestClassifier`, one per platform family (email/chat), binary actionable-vs-noise.
3. `reranker.py` — cross-encoder (`ms-marco-MiniLM-L-6-v2`) semantic score + date proximity, used only on the subset of messages that already have a date/event-link signal.
4. `event_match.py` / `dedup.py` — structured-field identity comparator, not a message scorer at all; it operates on *events*, post-extraction.

`recruiter_score.py` is a fifth, self-contained scorer (Gmail recruiter triage) that shares no code path with the above four; it is included because it is the other hand-tuned point-additive scorer in the training package and is a useful contrast case (same "sum of deltas, then clamp/bucket" shape as `events_match`, but for a different decision).

---

## 1. Objects & operations

**Message → boolean (gate) or float (rank):**

- `has_date_or_event_signal(text) -> bool` (`weights.py:73-86`) — OR of `GATE_DATE_RE`, `GATE_TIME_RE`, and an event-link-domain substring check. Used as a pre-filter before the expensive cross-encoder in `reranker.py` (via the separate, near-duplicate `_resolve_message_dates` path) and in `analysis.py:score_message`.
- `predict(messages) -> list[bool]` (`classifier.py:275-325`) — per-platform RandomForest `predict_proba`, thresholded at `ACTIONABLE_THRESHOLD`.
- `rerank_messages(messages) -> list[dict]` (`reranker.py:151-238`) — filters to messages with a future date or event link, scores survivors with a cross-encoder + date/URL/pin bonuses, sorts, truncates to `top_k`.
- `score_message(m) -> int` (`analysis.py:121-139`, skimmed for context) — additive keyword/regex score, used by `prioritize()` to cap message volume before the LLM extraction call.

**Event × Event → (bool, int):**

- `events_match(a, b) -> (bool, score)` (`event_match.py:130-158`) — the identity relation. Symmetric in practice (all four sub-comparisons are symmetric), not transitive (see §4).

**Event → normalized signature:**

- `event_signature(event) -> dict` (`event_match.py:116-127`) — the "fingerprint" input; not itself a hash, but the object every comparison and every stored fingerprint is built from.

---

## 2. The scoring pipeline as one equation

There is **no single global equation** — that would misrepresent the code. What exists is a *sequence of independently-thresholded stages*, each with its own local score. Written as a funnel (per `eval_classifier.py:106-141`, which is the only place in the repo that actually walks the whole chain and counts survivors):

```
messages
  --[Gmail read/sent filter]-->            (not scored, boolean)
  --[classifier.predict, thr=0.1]-->       P(actionable) >= 0.1   (learned)
  --[analysis.prioritize, cap=150]-->      score_message() additive, top-K only (hand-tuned)
  --[LLM extraction]-->                    events (not covered by these files)
  --[weights.extract_events / prefilter_events]--> date-based structural filter (hand-tuned)
  --[reranker.rerank_messages]-->          semantic*10 + date + bonuses, top-150 (hybrid: 1 learned term, rest hand-tuned)
  --[event_match.dedup_events / dedup.py]--> identity merge (hand-tuned, unlearned)
```

`weights.py` events and the classifier/reranker message stream are parallel, overlapping consumers of the same raw messages, not strictly sequential stages feeding one another (`extract_events`/`prefilter_events` build the "Events:" funnel branch shown separately in `eval_classifier.py:134-141`, apart from the "Pipeline Funnel" message-count branch).

### 2.1 The classifier stage (the one learned component)

`classifier.py:214-217` — `RandomForestClassifier(n_estimators=100, random_state=42, class_weight="balanced")`, trained per-platform (`_train_one`, `classifier.py:204-236`) on features from `extract_email_features` (`classifier.py:80-104`) or `extract_chat_features` (`classifier.py:107-132`).

Email features (12): `length`, `word_count`, `num_links`, `num_questions`, `has_unsubscribe`, `is_reply`, `has_scheduling`, `has_question_words`, `is_noreply_sender`, `is_notification`, `sender_is_self`, `html_density`.

Chat features (13): `length`, `word_count`, `num_links`, `num_questions`, `has_scheduling`, `has_question_words`, `has_mention`, `has_everyone`, `has_code_block`, `is_short` (<20 chars), `is_long` (>200 chars), `is_reaction_only`, `has_attachment_signal`, `is_bot`, `is_pinned`.

Both share `has_scheduling` — `SCHEDULING_KEYWORDS` from `weights.py:89-103` (meeting/call/event vocabulary), so the "learned" model is partly conditioned on the same hand-authored keyword list used elsewhere in the pipeline.

**Training data source:** `train(messages, labels, ...)` (`classifier.py:165-201`) takes labels produced upstream by `training/label.py:label_messages()`, which sends batches of messages to an LLM (`chat_completion`) with `_LABEL_SYSTEM_PROMPT` (`label.py:14-42`) asking for an `E`/`N` (event/noise) verdict in INI format, parsed by `parse_llm_ini`. So **the RandomForest's ground truth is itself LLM-generated**, not human-labeled — `eval_classifier.py` explicitly calls the LLM labels "ground-truth" (module docstring, `eval_classifier.py:1`) and describes the LLM-labeled recall as "per LLM labels" (`eval_classifier.py:162`). Any bias in the labeling prompt propagates directly into the classifier. On labeling-call failure, messages default to `label=1` (actionable) — `label.py:96-97` — a fail-open choice consistent with the recall-favoring theme of this stage.

**Threshold, and why 0.1 (hand-tuned, with a documented sweep):** `ACTIONABLE_THRESHOLD = 0.1` (`classifier.py:260`). The comment block above it (`classifier.py:241-259`) records an actual threshold sweep on 2,347 held-out chat messages (125 true events):

| threshold | precision | recall | kept/2347 | events missed |
|---|---|---|---|---|
| 0.05 | 0.161 | 0.960 | 747 | 5 |
| 0.10 | 0.173 | 0.952 | 686 | 6 |
| 0.15 | 0.178 | 0.928 | 653 | 9 |
| 0.30 (previous value) | 0.240 | 0.880 | 458 | 15 |
| 0.50 (sklearn default) | 0.392 | 0.752 | 240 | 31 |

`hand-tuned`: the threshold itself (0.10) is a manually chosen point on this curve, justified by "9 of 15 missed events recovered for 228 extra messages," with the stated stopping rule that going below 0.10 buys "~1pp of recall for another 61 messages — not worth it" (`classifier.py:257-259`). The underlying probabilities are learned; the operating point is not.

**Fallback behavior — fail open, not fail closed:** if no model file exists for a platform, `predict()` returns `True` for every message in that group (`classifier.py:311-313`); if *no* classifier files exist at all, `filter_actionable()` returns every message unfiltered with a `logger.warning` (`classifier.py:351-357`). Web-platform messages are never classified — `filter_actionable()` routes them straight through on the stated assumption that they are "pre-curated event pages... already filtered by DOM extraction, fingerprint dedup, and dead-page detection" (`classifier.py:359-361`).

**BUG note (already fixed, but worth flagging as a class of risk):** the module docstring at `classifier.py:19-40` documents a real historical bug where `_find_classifiers_dir()` walked the wrong number of `.parent` hops and pointed at `src/classifiers` instead of `<repo>/classifiers`, silently taking the "no models" branch and disabling the entire noise filter with only a warning that "nobody reads." The current implementation searches upward for an existing `classifiers/` directory instead of counting parents, which degrades path-layout changes to a warning rather than silent misrouting — but it is the same fail-open design (a missing-models condition still returns "everything is actionable," now for a *different* possible reason, such as the directory search itself matching an unintended ancestor `classifiers/` folder). `EVENT_HARVESTER_CLASSIFIERS_DIR` overrides it (`classifier.py:41-43`).

### 2.2 The regex gate + keyword heuristic stage (`weights.py`, hand-tuned throughout)

`has_date_or_event_signal()` (`weights.py:73-86`) is explicitly "loose — false positives are fine, false negatives are not" (docstring), built from `GATE_DATE_RE` (`weights.py:28-57`) and `GATE_TIME_RE` (`weights.py:59-70`), plus event-link-domain substring matching against `_EVENT_LINK_DOMAINS` (`weights.py:366-370`: `luma.com`, `lu.ma`, `eventbrite`, `meetup.com`, `zoom.us`, `meet.google.com`, `webex.com`, `hopin.com`, `fetlife.com/events`, `plra.io`, `partiful.com`).

`extract_links()` scoring (`weights.py:250-283`):

| component | formula/value | line |
|---|---|---|
| recency score | 10 if ≤7d, 8 if ≤14d, 6 if ≤30d, 4 if ≤90d, 2 if ≤180d, else 1 | `_recency_score`, `weights.py:210-229` |
| link type score | 8 event platform, 6 tech (`openai.com`, `anthropic`, `claude.ai`, `arxiv`, `github.com`), 5 article (`medium.com`, `substack`, `blog`, `article`), 3 social (`instagram.com`, `twitter.com`, `x.com`, `youtube.com`), 1 discord-channel link, 0 GIF domains (`tenor.com`, `giphy.com`), 4 default | `_link_type_score`, `weights.py:232-247` |
| pin boost | `+2.0` if `m["pinned"]` | `weights.py:263` |
| combined | `round(recency * 0.6 + type_score * 0.4 + pin_boost, 1)` | `weights.py:273` — pinned test in `test_weights.py:65` confirms `10*0.6 + 8*0.4 = 9.2` for a top-scoring luma link |

`extract_events()` scoring (`weights.py:286-363`):

- `_event_proximity_score(resolved_date)` (`weights.py:186-207`): 10 today, 9 if ≤3d, 7 if ≤7d, 5 if ≤14d, 3 if ≤30d, 1 otherwise, 0 if past or unresolved.
- `base = max(recency, proximity)` (`weights.py:344`), then `score = base + (3 if has_sched else 0) + (3 if is_pinned else 0)` (`weights.py:345`) — max theoretical score ≈16, confirmed by the comment at `weights.py:341-343` and by `test_weights.py:115` (`10 + 3 = 13` for a scheduling-keyword message).
- Short-message drop: `len(content) < 20 and not times` → discarded (`weights.py:305-306`), pinned by `test_weights.py:131-141` (`"ok Monday"` yields zero events).

`prefilter_events()` (`weights.py:373-409`) — a purely structural boolean gate, not a score: keep if (a) resolved date ≥ today, or (b) has scheduling keyword + (date or time) with no resolved date yet, or (c) contains an event-platform link substring; drop a resolved-past-date event outright even if other signals are present.

`SCHEDULING_KEYWORDS` (`weights.py:89-103`) is a single hand-authored list shared by `classifier.py` (`has_scheduling` feature), `analysis.py` (`_get_actionable_signals`), and `weights.py` itself — a `magic number`/hand-tuned vocabulary list with no learned weighting per keyword; every keyword contributes identically (binary "any match").

`analysis.py:score_message()` (skimmed, not in the assigned file list but load-bearing for how the gate and classifier outputs are consumed downstream): `+3` if `has_date_or_event_signal`, `+2` if URL present, `+2` if pinned, `+1` if any actionable-signal keyword — used only to sort-and-cap (`prioritize`, `max_messages=150`, `analysis.py:142-151`), not as a further gate. All four weights (`3`, `2`, `2`, `1`) are `hand-tuned magic numbers` with no cited justification in the code.

### 2.3 The reranker stage (`reranker.py`, mostly hand-tuned, one learned term)

Only messages with a **future** resolved date or an event-link (per `_resolve_message_dates`, `reranker.py:107-148`, itself built on `weights.py`'s `GATE_DATE_RE`, `_resolve_date`, `_event_proximity_score`, `_EVENT_LINK_DOMAINS`) reach the cross-encoder. This is `has_future or has_event_link` at `reranker.py:188` — messages that resolve to a *past* date or carry no date/link signal at all are dropped before any semantic scoring happens (counts logged at `reranker.py:196-199`).

Combined score (`reranker.py:217-224`):

```python
score = (
    float(sem_score) * 10
    + best_proximity
    + (2 if TIME_RE.search(content) else 0)
    + (2 if url_re.search(content) else 0)
    + (3 if has_event_link else 0)
    + (2 if m.get("pinned") else 0)
)
```

| term | source | weight | tuned? |
|---|---|---|---|
| `sem_score` | `CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2").predict()` against `_EVENT_QUERY` (`reranker.py:14-18`) | `×10` | learned model, hand-tuned scale factor |
| `best_proximity` | `_event_proximity_score` from `weights.py` (0–10 scale, §2.2) | `×1` | hand-tuned |
| has explicit time (`TIME_RE`) | `weights.py:21-24` regex | `+2` | hand-tuned |
| has any URL | `reranker.py:172` | `+2` | hand-tuned |
| event-link domain present | `_EVENT_LINK_DOMAINS` | `+3` | hand-tuned |
| pinned | `m.get("pinned")` | `+2` | hand-tuned |

The `×10` scale factor on `sem_score` is itself a hand-tuned constant chosen so the cross-encoder's [-∞, ∞]-ish logit-derived score (in practice roughly 0–1 after the model's internal scaling for this MiniLM checkpoint) sits in the same rough numeric range as the other additive terms — the code offers no derivation for why 10 rather than, say, 5 or 20; treat it as a `magic number`. `top_k=150` (`reranker.py:154`) truncates the final ranked list; no justification given beyond matching the same 150-message batch-size logic used in `analysis.py:prioritize`.

**How classifier and reranker compose:** they are not literally chained on the same score. `classifier.py` operates on the full raw message stream to cut noise before the LLM-extraction stage (`eval_classifier.py`'s "Pipeline Funnel"); `reranker.py` operates on a *separately filtered* date/link-bearing subset, used elsewhere in the harvest flow (not shown in the funnel walk in `eval_classifier.py`, which uses `prioritize()` instead of `rerank_messages()` for its funnel simulation — `eval_classifier.py:128`). Both exist as alternative/parallel "cut down the message volume before the LLM" strategies; nothing in the read files shows them feeding into one merged score.

### 2.4 Recruiter scorer (`recruiter_score.py`) — same shape, different domain, fully unlearned

`grade_email()` (`recruiter_score.py:311-349`): `base_score = 50` (`recruiter_score.py:325`), then additive deltas from `_score_sender`, `_score_subject`, `_score_body`, clamped to `[0, 100]` (`recruiter_score.py:339`).

| signal | delta | source |
|---|---|---|
| sender domain in `_STAFFING_FIRM_DOMAINS` (39 body-shop domains, `recruiter_score.py:28-69`) | `-25` | `_score_sender`, `recruiter_score.py:206-208` |
| subject matches `_MASS_BLAST_SUBJECT_RE` ("urgent requirement", "hot job", etc.) | `-5` | `recruiter_score.py:217-219` |
| subject has contract terms (`_CONTRACT_RE`) without full-time terms (`_FULLTIME_RE`) | `-5` | `recruiter_score.py:222-224` |
| body matches `_BODY_SHOP_FORMAT_RE` ("Job Title: ... // Location: ...") | `-20` | `recruiter_score.py:235-237` |
| body matches `_DURATION_LINE_RE` ("Duration: N months") instead (elif) | `-10` | `recruiter_score.py:238-240` |
| body has a template opener phrase (13 phrases, e.g. "hope you are well") | `-10` (first match only) | `recruiter_score.py:242-246` |
| body has contract terms without full-time | `-8` | `recruiter_score.py:250-252` |
| body has a direct meeting-request phrase (15 phrases) | `+15` (first match) | `recruiter_score.py:254-258` |
| body has a calendar-scheduling domain (`_CALENDAR_DOMAINS`, 9 domains) | `+15` (first match) | `recruiter_score.py:261-266` |
| body has personalization language (`_PERSONALIZATION_RE`) | `+10` | `recruiter_score.py:268-271` |
| body names a `_QUALITY_COMPANIES` entry (39 companies) | `+10` (first match) | `recruiter_score.py:273-278` |
| body mentions full-time (`_FULLTIME_RE`) | `+8` | `recruiter_score.py:280-283` |
| SF/Bay Area (`_SF_BAY_RE`) AND onsite/hybrid (`_ONSITE_RE`) both present | `+5` | `recruiter_score.py:285-288` |
| salary/equity mentioned (`_SALARY_RE`) | `+5` | `recruiter_score.py:290-293` |

Action buckets (`_action_for_score`, `recruiter_score.py:298-305`, boundaries pinned by `test_recruiter_score.py:134-142`):

| score range | action |
|---|---|
| ≤ 20 | trash |
| 21–45 | ignore |
| 46–65 | review |
| ≥ 66 | respond |

Optional LLM refinement (`_llm_refine_borderline`, `recruiter_score.py:389-460`) re-scores only messages with `30 ≤ score ≤ 60` (default `llm_threshold`, `recruiter_score.py:356`) and overwrites `grade.score`/`grade.action` outright if the LLM returns a parseable value — i.e., for borderline cases the hand-tuned heuristic is a fallback/prior, not a floor. Every constant in this section (all deltas, both threshold boundaries, and the domain/phrase lists) is `hand-tuned`; there is no fitting step anywhere in this file.

---

## 3. Reduction to the concrete implementation

Reading top to bottom, the actual call graph a message passes through in a harvest run (per `eval_classifier.py`'s funnel, which is the only executable trace of the full path in these files) is:

```python
after_read = gmail_filter(eval_msgs)                 # sources.filter_read_sent — boolean, Gmail only
after_clf  = classifier_filter(after_read)            # classifier.filter_actionable — learned, thr=0.1
after_kw   = prioritize(after_clf)                    # analysis.prioritize — hand-tuned additive score, cap 150
# -> after_kw goes to the LLM for task/event extraction
events           = extract_events(after_read)          # weights.extract_events — separate branch, not gated on classifier output
future_events    = prefilter_events(events)             # weights.prefilter_events — boolean structural gate
```

`reranker.rerank_messages` is not invoked in this funnel function at all — it exists as an alternative message-narrowing entry point used elsewhere in the harvest pipeline (not present in the eight files read for this document beyond its own module), so its precise place in the end-to-end call order is `unverified` from these files alone.

Recall-losses are tracked stage by stage against the LLM-labeled ground truth (`eval_classifier.py:149-159`): `lost_read`, `lost_clf`, `lost_kw`, then `survived`. The design intent stated at `classifier.py:245` ("a missed event is unrecoverable, while extra noise is cheap") is the single guiding principle behind every threshold choice documented above — every hand-tuned constant in §2.1–§2.3 either directly implements or is consistent with "favor recall, let the LLM clean up downstream."

---

## 4. Identity & deduplication

### 4.1 The fingerprint function

`event_signature(event)` (`event_match.py:116-127`) — the normalized comparison object (not a hash digest; a dict of normalized fields):

| field | normalization | function |
|---|---|---|
| `title_words` | lowercase → strip `string.punctuation` → split on whitespace → drop stop words | `_normalize_title`, `event_match.py:74-79` |
| `date` | ISO passthrough, else double dateutil-fuzzy-parse with defaults `1900-01-01` / `2099-12-31`; **rejected** (`None`) unless month and day agree between both parses, guarding against dateutil's fuzzy parser fabricating a date from a stray digit like "Event 2" (explicit guard, `event_match.py:27-32`); year resolved to current year only when both defaulted years disagree in the expected 1900/2099 pattern; final range check `2000 ≤ year ≤ 2100` | `_normalize_date`, `event_match.py:21-71` |
| `location_words` | same normalization as title (lowercase, strip punctuation, drop stop words), or `None` if empty/falsy/non-string | `_normalize_location`, `event_match.py:82-92` |
| `link` | `.strip().lower()` only — no scheme/query/trailing-slash canonicalization | `event_match.py:126` |

Stop words (`_STOP_WORDS`, `event_match.py:18`): `{the, a, an, at, in, on, for, and, of, to}` — an 10-word `hand-tuned` list.

### 4.2 The dedup similarity rule

`_titles_overlap(a, b)` (`event_match.py:95-113`), used for both title and location word-set comparison:

- `False` if either set is empty.
- `True` if the sets are exactly equal (handles 1–2-word titles that would otherwise fail the ratio test — e.g. "AI Meetup" vs itself, per docstring).
- Otherwise: overlap must be **≥ 2 words** AND overlap ratio `len(a & b) / min(len(a), len(b)) ≥ 0.5`. Both conditions are `magic number`s with no derivation given; pinned by `test_event_match.py:44-54` (`{belgian, waffle}` overlap of 2/3 = 66% matches; `{lvfc}` overlap of 1/3 = 33% does not, precisely because it's below the 2-word floor as well as arguably below ratio).

`events_match(a, b) -> (bool, score)` (`event_match.py:130-158`):

| signal | condition | points |
|---|---|---|
| title | `_titles_overlap(title_words)` | +1 |
| date | both dates normalized and equal (string equality on ISO date) | +1 |
| location | both location word-sets present and `_titles_overlap` | +1 |
| link | both links present and equal after `.strip().lower()` | +2 |

**Match iff total score ≥ 2** (`event_match.py:158`). Consequences, all confirmed by `test_event_match.py`:

- Same title + same date → match (score 2). `test_event_match.py:58-63`.
- Same title, different date → **no match** (score 1 — title alone is insufficient). `test_event_match.py:65-70`.
- Different titles (overlap <50%), same date → no match (score 1, since title contributes 0). `test_event_match.py:72-76`.
- Exact same link, everything else different → match (score 2, link alone suffices). `test_event_match.py:78-83`.

This is a hand-built point system with a hand-picked cutoff (`≥ 2`); there is no learned weighting of which field matters more — link is arbitrarily worth exactly 2x any other single field, meaning link-equality alone reaches the match threshold but title/date/location individually never do without at least one partner signal. This is not proven optimal anywhere in the code; it is asserted by the test suite as the intended behavior, i.e. `hand-tuned` by construction.

### 4.3 The fingerprint store — cross-run identity persistence

`data/.event_fingerprints.json`, path constant `FINGERPRINT_FILE = Path("data/.event_fingerprints.json")` (`event_match.py:16`).

`save_fingerprint()` (`event_match.py:211-246`) stores `title`, normalized `date`, `time` (raw, unnormalized), `location`, `link`, `ticktick_id`, `status`, `created_at` (today's ISO date). `status` is one of:

| status | meaning | filters user-visible "seen" list? |
|---|---|---|
| `"auto"` | saved during fetch/extract (e.g., web-link pre-fetch dedup) | no |
| `"approved"` | user clicked Approve | yes |
| `"declined"` | user clicked Decline | yes |

`find_fingerprint()` (`event_match.py:252-264`) matches against **all** stored fingerprints regardless of status — used only as a pre-fetch skip hint. `find_acted_fingerprint()` (`event_match.py:267-283`) restricts to `status in {"approved", "declined"}` (`_ACTED_STATUSES`, `event_match.py:249`) — this is the one that actually hides events from the user, so an auto-saved dedup hint from web scraping can never silently suppress an event the user hasn't explicitly acted on.

**Pruning** (`load_fingerprints()`, `event_match.py:164-208`), applied on every load, in this exact order:

1. Parseable date in the past (before `date.today()`) → drop.
2. Unparseable/missing date **and** `created_at` more than 30 days ago → drop.
3. Otherwise → keep.

30 days (`event_match.py:201`) is a `hand-tuned magic number` staleness window for date-less fingerprints, pinned by `test_event_match.py:137-154` (31 days old with no date is dropped; 5 days old is kept).

### 4.4 Dedup within a single run vs. against TickTick

`dedup_events()` (`event_match.py:286-318`) — within-run dedup: exact-link fast path via a `seen_links` set, else O(n·m) fuzzy match against previously-kept signatures using `_sig_match_score` (`event_match.py:321-333`, a duplicate of the scoring logic inside `events_match` — same weights, same ≥2 cutoff, kept as a separate function purely to reuse precomputed signatures and avoid recompute cost in the inner loop).

`dedup.py:dedup_events_against_ticktick()` (`dedup.py:35-75`) — three-way classification against existing TickTick tasks:

1. If `find_acted_fingerprint(event)` returns a hit → `skip` (user already approved/declined this).
2. Else compare against each existing TickTick task via `events_match`, using a degraded `task_as_event` that only has `title` and `date` (sliced to `dueDate[:10]`) — **location and link are always `None` for TickTick tasks** (`dedup.py:51-56`), so a TickTick match can only ever come from the title+date pair (score 2) since link (worth 2 alone) and location are structurally unavailable here.
3. On match, `_merge_action()` (`dedup.py:14-32`) decides `update` vs `skip`: `update` if the new event's `link`, `location`, `time`, or `notes` contains text not already present (case-insensitive substring check) in the existing task's `content + title`; otherwise `skip`.

**Not transitive:** because `events_match` requires ≥2 of 4 independent signals and different event pairs can satisfy the threshold via different signal combinations (e.g., A–B match via title+date, B–C match via link only, A–C match via neither), nothing in the code enforces transitivity of the match relation. `dedup_events()`'s greedy "match against first sufficiently-similar existing unique event" strategy (`event_match.py:304-311`) is therefore order-dependent: which event in a near-duplicate cluster becomes the canonical "unique" entry depends on iteration order, and A could fail to be recognized as a duplicate of C even after both matched B, if inserted in the wrong order. This is `unverified` as a real-world failure mode (no test exercises a 3-event near-duplicate chain) but is a structural property of the point-threshold design, not a bug in any single function.

---

## Minimal starter set (what to tune first)

If forced to tune only a handful of constants to move overall pipeline quality, in order of leverage (highest first, based on where the code's own comments admit uncertainty or a documented tradeoff):

1. **`ACTIONABLE_THRESHOLD = 0.1`** (`classifier.py:260`) — the only constant in this document backed by an actual sweep table; re-sweep whenever `label.py`'s LLM-labeling prompt or model changes, since the threshold was fit against one specific labeled set (2,347 chat messages).
2. **`events_match` cutoff `score >= 2`** and the **link weight `+2`** (`event_match.py:154-158`) — controls both within-run dedup aggressiveness and whether users see repeat events; currently means link-equality alone is dispositive while title/date/location never are alone. Worth an ablation against real duplicate/non-duplicate event pairs before touching.
3. **`_titles_overlap` thresholds — `min 2 overlapping words` and `ratio >= 0.5`** (`event_match.py:110-113`) — governs both title and location matching; short titles are especially sensitive to the 2-word floor.
4. **Reranker's `sem_score * 10` scale factor** (`reranker.py:218`) — no stated derivation; if the cross-encoder checkpoint changes, this scale needs re-deriving against the other additive terms' 0–10ish range.
5. **`SCHEDULING_KEYWORDS`** (`weights.py:89-103`) — touches classifier features, `analysis.score_message`, and the gate simultaneously; a single edit here has the widest blast radius of any constant in the codebase covered by this document.
6. **Fingerprint staleness window `30 days`** (`event_match.py:201`) — only affects events with unparseable/missing dates; low risk to tune but currently arbitrary.

---

## Verification

```bash
pixi run pytest tests/test_weights.py tests/test_recruiter_score.py tests/test_event_match.py -v
```

This proves: the recency/link-type/event-score formulas in `weights.py` (`extract_links`, `extract_events`, the gate regexes) match the literal numbers cited in §2.2; the recruiter scorer's sender/subject/body deltas and the four action-bucket boundaries in §2.4 match `recruiter_score.py`; and the `events_match`/`_titles_overlap`/fingerprint-pruning behavior in §4 (same-title-same-date matches, same-title-different-date does not, link-alone matches, 30-day/past-date pruning) matches `event_match.py`. It does not exercise `classifier.py`'s trained models (no `.pkl` fixtures in these three files) or `reranker.py`'s cross-encoder (network/model-store dependent) — those are asserted from source reading only, marked `unverified` by execution in this pass since the task scope was read-only.
