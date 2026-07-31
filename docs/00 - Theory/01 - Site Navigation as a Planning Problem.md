# Walking a site is a partially-observed planning problem whose hidden state is the *site*, not the page — and because observation is cheap, the tractable policy is determinize-and-replan, not a POMDP solver.

**Status:** DRAFT (theory; nothing here is implemented) · **Date:** 2026-07-30 · **Applies to:** `src/event_harvester/sources/web_steps.py`, `web_explore.py`, `src/event_harvester/observability/hints.py`, `src/event_harvester/web_primitives.py`, `data/web_sources.json` · **Depends on:** `docs/03 - Architecture/03 - Sources — Web.md`, `docs/05 - Standards/EH-STD-WEBSRC-v0 §3.2a`

**Executive summary:** The route engine shipped on 2026-07-30 executes a fixed action sequence and stops on the first failure. That is an *open-loop plan*, and it is the weakest structure that can do the job: it cannot distinguish "this step failed" from "this step's preconditions were never met", so every drift requires a human to re-author. This document argues that the same seven verbs, plus preconditions and effects written as data, turn navigation into a classical planning domain (GOAP) over a state vector the codebase *already computes* in `hints.collect_signals`. Non-determinism is real — `click(pagination)` may load, may no-op, may navigate away — which makes the honest model a POMDP. But solving a POMDP requires transition and observation models nobody has, and the mitigating fact is that **observing is cheap here**: one `page.evaluate` after every action. When sensing is cheap and informative, the standard determinizing approximation (assume deterministic effects, act, observe, replan on surprise) is a respectable policy, and it is exactly GOAP-with-replanning. The genuinely hidden variable is not the page but the *site's shape* — whether events sit behind search, behind pagination, or in the DOM already — and belief about that accumulates **across runs**, which is a different timescale from anything the current design models. Two cautions carry evidence: minting planning-domain elements at runtime breaks the executability guarantee that makes a plan worth having, and post-hoc deduplication of state nodes would inherit the order-dependence already documented as an open defect in `events_match` (roadmap 2.6).

---

## 0. Preliminaries / definitions

| Term | Meaning here |
|---|---|
| **verb** | One of the seven actions in `web_steps.STEP_VERBS`: `goto`, `dismiss`, `fill`, `click`, `scroll`, `wait`, `extract`. The action set. |
| **signal bundle** | The dict returned by `hints.collect_signals` — text length, link/input/dialog/iframe counts, password-field flag, next-control count, scroll geometry. One `page.evaluate`. |
| **world state** *(proposed)* | A typed predicate vector derived from the signal bundle. Not implemented; today the bundle is consumed by regex rules and discarded. |
| **route** | An ordered `steps[]` array in `web_sources.json` (`EH-STD-WEBSRC-v0 §3.2a`). An open-loop plan. |
| **role** | A target name (`search`, `pagination`, `modal`, `feed`, `chat`) resolved through a `web_primitives.find_*` verb *at replay time*, as opposed to a literal `selector:<css>`. |
| **goal** | A predicate the planner is trying to make true. In practice `event_links ≥ N`. |
| **replan** | Re-deriving a plan from the *observed* state after an action's actual effect diverged from its expected effect. |
| **rot** | A route that still executes but no longer yields, because the page changed underneath it. Indistinguishable from a quiet week without history. |

**GOAP** (Goal-Oriented Action Planning, from game AI): actions declare preconditions and effects as world-state predicates plus a cost; a planner searches for an action sequence from the current state to a goal state. Its defining property is not the search — it is that **the domain is data**, so adding an action never touches the planner, and a failed action is answered by replanning rather than by a scripted fallback.

---

## 1. What the codebase already has, in planning vocabulary

This is the load-bearing observation of the document: three of the four pieces of a planning domain exist, under other names.

| Planning concept | Where it already lives | Gap |
|---|---|---|
| Action set | `STEP_VERBS`, seven verbs, each mapping to exactly one L1 primitive | none |
| State observation | `hints.collect_signals` — one round-trip, ~14 fields | Consumed by regexes, never typed or retained |
| Action cost | Implicit and heterogeneous: `dismiss` ≈ 50 ms, `scroll` = 8 s, `goto` risks a Cloudflare challenge *and* a cooldown, a vision call costs money | Nowhere written down |
| Preconditions / effects | **Prose only**, in docstrings | The actual gap |

`hints.rule_hints` is worth naming precisely: it is a **degenerate one-step planner**. It reads a state, and for three of its hint kinds emits a `suggested_step` — `needs_scroll → {"verb": "scroll"}`, `cookie_banner → {"verb": "dismiss"}`, `needs_search → {"verb": "fill", "target": "search"}`. That is precondition-matching with a horizon of one and no goal test. Everything below is the generalization of a thing already shipped.

---

## 2. The domain, written out

**State predicates** (derived from the signal bundle; all cheap, all from one round-trip):

```
blocked_by_dialog     dialogs > 0 ∨ consent-wording matched
blocked_by_login      password_inputs > 0
blocked_by_challenge  bot-check wording matched
content_visible       text_len ≥ MIN_CONTENT_CHARS
links_present(n)      links ≥ n
search_available      search_inputs > 0 ∨ find_search_box() ≠ ∅
more_below            scroll_height > k · viewport_height
next_control          next_controls > 0
extracted(n)          n links accumulated in ReplayResult
```

**Actions**, as ⟨precondition → effect, cost⟩. Costs are wall-clock milliseconds except where a risk premium is noted:

| Action | Precondition | Intended effect | Cost |
|---|---|---|---|
| `dismiss` | `blocked_by_dialog` | `¬blocked_by_dialog` | ~50 |
| `wait(ms)` | `blocked_by_challenge` | `¬blocked_by_challenge` | ms |
| `scroll(s)` | `more_below` | `links_present(n+Δ)` | 1000·s |
| `fill(search, q)` | `search_available ∧ ¬blocked_*` | `content_visible ∧ links_present` | ~2500 |
| `click(pagination)` | `next_control ∧ ¬blocked_*` | `links_present(n+Δ)` | ~2000 |
| `goto(url)` | — | resets everything | ~1500 + challenge risk + cooldown |
| `extract` | `content_visible` | `extracted(n)` | ~20 |

**Goal:** `extracted(n) ∧ n ≥ expect_min_events`, which is a field the schema already has.

Two things fall out of writing this table that do not require a planner at all:

1. **`validate_steps` gets semantics.** It currently checks syntax — unknown verbs, missing keys, bad targets. With preconditions it can reject `extract` before anything that establishes `content_visible`, at authoring time, with no browser. That is the same class of win as the browser-free `validate_steps` already delivers, one level up.
2. **The cost column is the actual product.** A planner that prefers `dismiss` (50 ms) over `scroll` (8 s) over `goto` (a cooldown and a Cloudflare risk) is encoding the identical judgement `hints.VLM_ESCALATION_THRESHOLD` encodes by hand: *do the cheap deterministic thing first, and pay only when it comes back inadequate.* GOAP forces that judgement to be written down once instead of re-derived per module.

---

## 3. Why it is a POMDP, and why that does not mean solving one

Effects above are labelled *intended* because none are guaranteed. `click(pagination)` may append results, may silently no-op, may navigate away. `scroll` may load more, or may have been at the bottom already. The effect is a distribution, not a value, so the honest model is a POMDP: hidden state, stochastic transitions, noisy observations.

**Solving it is not on the table.** A POMDP solver needs a transition model `T(s'|s,a)` and an observation model `O(o|s)`. Estimating those from 29 sources means estimating a model per site shape from a handful of runs each — and the feed probe's measured 1/27 hit rate is a reminder of how thin the evidence per site actually is.

**The mitigating structure is that observation is cheap.** `collect_signals` is one `page.evaluate`, and it can run after *every* action. Partial observability is expensive to reason about when sensing is costly, because you must plan *to* sense; when sensing is nearly free and highly informative, most of the belief collapses on contact and the problem degrades toward an MDP with frequent state resets.

That justifies the standard determinizing approximation:

> Assume the most likely effect. Act. Observe. If the observation contradicts the expectation, replan from what you actually see.

This is a known-adequate family (QMDP / most-likely-state / replanning determinizers) — and it is precisely GOAP-with-replanning. **The correct claim is therefore not "use a POMDP instead of GOAP" but "GOAP-with-replanning is a defensible approximate POMDP policy in the cheap-observation regime."**

### 3.1 The hidden variable is the site, not the page

The page's state is *observed*, cheaply, after every action. What is genuinely hidden is one level up: **the site's shape** — does this venue put its events behind a search box, behind pagination, in the initial DOM, or nowhere?

That variable is:

- **constant across a run** and slowly varying across months;
- **informed by every run**, not by every action;
- already being measured. `web.route.yield` keyed by `source` is a per-site outcome distribution, which is the raw material for exactly this belief.

The consequence is a timescale mismatch with everything currently built. A route is re-derived per harvest at best; belief about site shape wants to accumulate across harvests, in per-source state. This is the same gap already logged as roadmap 4.6: `expect_min_events` is hand-set because nothing records history. The planner and the tripwire want the same missing structure.

---

## 4. Where planning must and must not run

The earlier decision — *the agent runs at authoring and repair time only, never per-harvest* — was correct **about the LLM** and does not transfer to a planner.

| | Cost per invocation | Belongs in the harvest loop? |
|---|---|---|
| LLM / VLM call | money + ~1 s | No. Per source per run is how a scraper becomes too expensive to run daily |
| `collect_signals` | one `page.evaluate` | Yes |
| A* over 7 actions, ~10 predicates, plans of length 3–8 | microseconds | Yes |

**A planner is not a model.** Planning is arithmetic. This reframes what a route *is*: not a script, but a **cached plan** — replayed while its preconditions hold, replanned the moment one does not. That is strictly more capable than the current engine, which cannot tell a failed step from a step whose preconditions were never established, and which therefore escalates every drift to a human.

The LLM stays where it was: authoring a route for a site nobody has walked, and repairing one when replanning itself fails.

---

## 5. Minting and identity — the two places this goes wrong

### 5.1 Two proposals wearing one word

**Minting map nodes** — recording that `/events/all` exists and what was found there — is a site graph. It is orthogonal to the planning domain, it is useful, and it composes with everything above.

**Minting planning-domain elements at runtime** — new predicates or new actions — is a different proposal and should be refused. A new action is a new verb, which is a new L1 primitive, which is code. A new predicate that nothing knows how to *achieve* is either inert or makes the planner fail to find a plan. The property that makes a plan worth having is that every action's preconditions are satisfiable by construction; mutating the domain at runtime is exactly the operation that removes it.

### 5.2 Post-hoc dedup inherits a defect this repo has already documented

Deduplicating minted nodes after the fact requires a state-identity relation. The repo has one for events, and its properties are recorded in `00 - Theory/00`: `events_match` is **symmetric but not transitive**, and `dedup_events` is greedy first-match, so clustering depends on insertion order. That is roadmap **2.6**, still open, and explicitly `unverified` — no test pins a three-event chain.

For events, order-dependent clustering is a nuisance. For state nodes it is a correctness failure: merging two genuinely different pages makes every plan routed through that node wrong, and the error is silent.

**The fix is to move identity earlier.** Assign a canonical key at mint time so deduplication is exact-match on a hash rather than fuzzy clustering afterwards. The mechanism exists: `uc.save_signature` / `uc.load_signatures` is a page-signature store, already used in `web_fetch.py` to persist detected modes. Note what the key must **not** be: the URL. SPAs, session parameters and dated permalinks all lie about identity — `feed_probe._DATED_PERMALINK_RE` exists precisely because a URL that looked like a calendar was a single event.

---

## 6. What this predicts, and how it would be falsified

If the argument holds:

- Preconditions alone (no planner) should catch semantically impossible routes that `validate_steps` currently passes.
- Replanning-on-failure should recover a measurable fraction of the routes that currently return zero. The baseline already exists — `hints`' one-step `suggested_step` — and the measurement already exists: `web.steps.executed{ok}` and `web.route.yield{source}`.
- Cost-ordered action selection should reduce mean route wall-clock without reducing yield.

If it does not hold, the likeliest reasons, in order:

1. **The domain is too small for search to matter.** Seven actions, ten predicates, plans of 3–8 steps. "Filter to actions whose preconditions hold, take the cheapest" may capture most of the benefit with no A* at all. *The planner is the cheap part; the model is the work.* If §2's table is not worth writing on its own merits, a search on top of it will not rescue it.
2. **Effects are too stochastic for determinization.** If `click(pagination)` succeeds under identical observed states less than roughly half the time, replanning thrashes rather than converges.
3. **The predicates are not the ones that matter.** The signal bundle was designed to explain *blockage*, not to support planning. Two of its rules were measurably wrong on first contact with a live page (`2026-07-30_routes-and-hints.md` §5) — a bare `\b404\b` matching "1,404 going", and a suggestion outranking a `content` verdict. A state vector built from heuristics that were wrong twice in one afternoon is a shaky foundation, and the fix in both cases came from a live page rather than from reasoning.

---

## Minimal starter set (what to build first)

Ordered so each step is useful if the next is never built:

1. **Type the state.** Turn the signal bundle into an explicit predicate vector. A rename of what exists; no risk.
2. **Annotate the seven verbs** with precondition, effect, cost. Pure data. Pays for itself in `validate_steps` alone.
3. **~150 lines of A*.** Goal `links_present(n)`.
4. **Repair only, measured.** Replan when replay fails; compare against the `suggested_step` baseline using metrics that already exist.
5. **Site-graph nodes last**, with canonical keys from day one (§5.2).

---

## Verification

Nothing in this document is implemented, so there is nothing to run. What *can* be checked is that the premises are true of the code as it stands:

```powershell
pixi run python -c "from event_harvester.sources.web_steps import STEP_VERBS; print(sorted(STEP_VERBS))"
pixi run test tests/test_hints.py -v          # the rule layer this generalizes
pixi run test tests/test_web_steps.py -v      # the open-loop engine it would replace
```

`unverified` — every claim about planner behaviour in §6 is a prediction, not a measurement. No planner exists.

`inferred` — the cost figures in §2 are read off default arguments and observed step timings in one live run against `lu.ma/discover`, not from a profile across sources. The `goto` risk premium is qualitative.

`known rough edge` — §3.1 asserts that site shape is stable across a run and slowly varying across months. That is an assumption from how these sites have behaved, not a measurement; no per-source history exists yet to check it against, which is the same missing structure roadmap 4.6 needs.
