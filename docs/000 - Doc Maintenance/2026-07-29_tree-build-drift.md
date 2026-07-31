# Docs-tree gaps — 2026-07-29
**Status:** Observational note · **Date:** 2026-07-29 · **Applies to:** `docs/` as a tree

*Observational note. Not a roadmap, not a plan.* Records what drifted and what it would cost to fix, so the next reader sees a marker instead of acting on stale prose. Actionable defects are tiered in `docs/06 - Roadmaps/00 - Gap Analysis and Open Work.md`; this note is about the **tree itself**.

## The tree today

```
docs/
├── README.md                      index + governance                    ✅ new
├── 000 - Doc Maintenance/         this note                             ✅ new
├── 00 - Theory/                   1 doc — ranking/gating/identity       🟡 thin by design
├── 01 - Design/                   1 doc — why the system has this shape 🟡 thin; rationale still lives in docstrings
├── 02 - Implementation/           4 docs — env, creds, deploy, dev loop ✅
├── 03 - Architecture/             12 docs — overview + 11 subsystems    ✅ complete
├── 04 - Reference/                7 docs — UC, pixi, CLI, MCP, glossary,  ✅
│                                  Fable + engineer personas
├── 05 - Standards/                4 docs — docs style + 3 record specs  🟡 v0 DRAFT, not yet frozen
├── 06 - Roadmaps/                 1 doc — gap analysis                  ✅ supersedes TODO.md
├── 07 - Status Reports/           4 docs — tree build + 3 fix passes    ✅ (1 at time of writing)
├── 08 - Analysis/                 3 docs — browser log, what-worked,    ✅ (was ⚠️ — see Resolution log)
│                                  test inventory
└── 09 - Archived/                 1 transcript + ARCHIVE-INDEX          ✅
```

## Gap 1 — `README.md` is the most-read and least-true doc in the repo

Four contradictions against the code, catalogued as Tier 0.1–0.5 in the roadmap: a `--no-cap` flag that does not exist, a `--cap web=-1` example that exits non-zero, "five web modes" when there are six, a "Project structure" section describing the pre-refactor flat layout, and 6 MCP tools listed against 20 registered.

→ **Cheap fix (~1h):** rewrite the four sections against `docs/04 - Reference/CLI Command Reference.md` and `docs/04 - Reference/MCP Tool Reference.md`, which were derived from the parser and the server rather than from memory.
· **Alternative:** cut `README.md` down to a landing page that links into `docs/` and stops restating flag tables it cannot keep current. Lower ongoing cost, loses the single-file onboarding read.

## Gap 2 — The attempt log points at tooling that no longer exists

`08 - Analysis/00 - Browser Configuration Attempt Log.md` closes with "add a `BrowserConfig` preset to `scripts/browser_configs.py`" and "run `pixi run python scripts/browser_test.py --run BC-NNN`". Neither file is in the repo; `data/browser_test_results.jsonl` is, so the harness existed and was removed or never committed. The log's *content* is the most valuable analysis doc here — eleven configurations with lessons — and it is unaffected. Only the "how to add an attempt" procedure is dead.

→ **Cheap fix (~10min):** mark step 4–5 of "How to add a new attempt" as `harness removed — record the attempt by hand`.
· **Alternative:** restore the harness from history (`git log --diff-filter=D -- scripts/`) if the automation is still wanted.

## Gap 3 — Module docstrings assert counts that have moved

`web_modes/__init__.py` says "five entry points" while re-exporting six; `grok_tools.py` says "Exposes 12 tools" while registering 14. Both are the same failure: a count in prose next to the list it counts.

→ **Cheap fix (~5min):** delete the numbers, keep the lists.

## Gap 4 — `01 - Design` and `00 - Theory` hold one doc each

Not drift yet — a deliberate starting point. But most design rationale in this project currently lives in module docstrings (`pipeline/orchestrator.py`'s header is a genuine design doc), and the `.parent`-counting scars are recorded as code comments. That material is correct and well-written; it is just filed where only someone already reading the code will find it.

→ **Cheap fix (~30min each):** when a docstring grows past ~15 lines of *why*, move it into `01 - Design` and leave a one-line pointer.
· **Alternative:** accept docstrings as the design lane for this project and note that in `docs/README.md`. Cheaper, and arguably more honest for a codebase this size.

## Gap 5 — `05 - Standards` is normative but derived, not decided

The three `EH-STD-*` record specs were reverse-engineered from the as-built producers and consumers, so they currently *describe* rather than *govern* — and their §4 sections list the places the code already violates them (feed's `platform` tag, `notes` vs `details`, TickTick's `status="auto"`). A standard nobody has ratified is a strong draft.

→ **Cheap fix (~1h):** review §4 of each, decide per item "fix the code" or "amend the spec", then freeze at v0.
· **Alternative:** leave them at DRAFT and treat them as documentation. Honest, but then nothing in the repo is normative and `05` is redundant with `03`.

## Gap 6 — 33,305 lines of raw transcript are archived but only partly decomposed

`09 - Archived/2026-05-30_browser-auth-session-transcript.md` produced two analysis docs covering what worked and what was tried in the browser. Whether anything *else* in those 2,925 turns is worth extracting is unknown — nobody has read the remainder.

→ **Cheap fix:** none. This is a real cost.
· **Alternative:** run the `chat-decompose` skill over it once and see whether the output earns its keep; if not, the archive row stands as-is and this gap closes as "checked, nothing further".

## Why this note exists

Volume of code change routinely exceeds volume of doc maintenance, so drift accrues silently and the first sign of it is usually someone acting on a wrong doc. Every gap above was found in the space of one documentation pass, which is roughly the rate to expect. Making them visible costs one file; discovering them the other way costs an afternoon each.

## Suggested cadence

One note per session that changes code without the matching doc. Name it `YYYY-MM-DD_<topic>-drift.md`. Append; never rewrite an earlier note.

## Decision log

| Date | Decision | By |
|---|---|---|
| 2026-07-29 | Built the numbered tree under existing `docs/` rather than a parallel `event_tool_docs/` — one docs root, existing files relocated with `git mv` so history follows | this session |
| 2026-07-29 | Filed the portable style guide as `05 - Standards/EH-STD-DOCS-v0 — Agentic Technique Master.md` — it is normative for this repo even though it is project-agnostic in origin | this session |
| 2026-07-29 | `TODO.md` reduced to a pointer; open work lives in `06 - Roadmaps` where items can carry tier, dependency, and verification status | this session |
| 2026-07-29 | `CLAUDE.md` rewritten: its `./pixi_guide.md` pointer had already broken (file moved to `docs/guides/` before this session) and three agents independently reported it as missing | this session |
| 2026-07-29 | Gaps 1–6 recorded, none actioned in this pass | this session |
| 2026-07-29 | Gap 1 resolved by the **alternative**, not the cheap fix: `README.md` is now a landing page. It keeps install, first run, and system shape; it delegates the full flag and tool tables to `04 - Reference`, which are derived from `parser.py` and the server. Rationale: the tables drifted *because* they were duplicated, so fixing them in place would have re-armed the same trap | this session |
| 2026-07-29 | Gap 2 resolved by marking the harness dead rather than restoring it — nobody has asked for `BC-NNN` automation since 2026-04-28, and the log's value is the eleven lessons, not the runner | this session |
| 2026-07-29 | Gap 3 resolved by deleting the count from `web_modes/__init__.py` and pointing at `__all__`. `grok_tools.py` left alone: it lives in the `ext/universal_controller` submodule, so the fix belongs upstream | this session |

## Resolution log

Appended after the first fix pass, same day. Gaps 4, 5, and 6 are unchanged and still open.

| Gap | State | Where the fix landed |
|---|---|---|
| 1 — `README.md` untrue | ✅ Closed | `README.md` rewritten as a landing page; five contradictions gone; a "Verification" block at the bottom names the three commands that would catch the next drift |
| 2 — attempt log cites a removed harness | ✅ Closed | `08 - Analysis/00 - Browser Configuration Attempt Log.md`, "How to add a new attempt" step 4 now reads `harness removed — record the attempt by hand` |
| 3 — docstring counts | 🟡 Half closed | `sources/web_modes/__init__.py` fixed. `grok_tools.py` is upstream in the submodule — tracked as roadmap 0.9, deliberately not fixed here |
| 4 — `01 - Design` / `00 - Theory` thin | 🔲 Open | No decision yet on docstrings-as-design-lane |
| 5 — `05 - Standards` derived, not ratified | 🔲 Open | Still the largest honest gap in the tree: the folder says Normative and nothing has been ratified |
| 6 — transcript only partly decomposed | 🔲 Open | Nobody has read the remaining ~2,900 turns |

`known rough edge`: this note's tree diagram counts docs as of the build pass. It is not regenerated automatically, so treat it as a snapshot rather than a live index — `docs/README.md` is the index.

## Second resolution log — 2026-07-30 (two fix passes)

Appended, not rewritten, per the cadence rule above.

| Gap | State | Note |
|---|---|---|
| 4 — `01 - Design` / `00 - Theory` thin | 🟡 **Half addressed** | `00 - Theory` went 1 → 2 docs: `01 - Site Navigation as a Planning Problem.md` (GOAP/POMDP over the route engine). It is filed as `DRAFT` deliberately — nothing in it is implemented, and a theory folder whose freeze policy is "stable, corrections only" must not imply that an argument is an as-built description. `01 - Design` is unchanged, and the docstring-as-design-lane question is still undecided: `pipeline/orchestrator.py`'s header and the `ExtractionResult` docstring remain design documents living in code |
| 5 — `05 - Standards` derived, not ratified | 🟡 **Progress, not closed** | Two §4 non-conformances were resolved *in favour of the spec* rather than amended away: `BUG-MSG-1` (feed mode's `platform` tag) and `BUG-WEBSRC-1` (the `no-web` profile). That is two decisions made out of the set; the folder is still Normative-in-name until the rest are ruled on |
| 6 — transcript only partly decomposed | 🔲 Still open | Nobody has read the remaining ~2,900 turns |

**New files this pass** (not drift, recorded so the tree diagram above is known-stale): `03 - Architecture` gained no docs but ~6 were reconciled; `07 - Status Reports` went 1 → 4; `05 - Standards` gained a `source_page` and `queries` field; `docs/` totals ~42.

**Observation worth keeping.** Every defect closed on 2026-07-30 was *documented before it was fixed* — the doc build found them, the roadmap tiered them, and the fixing was mechanical afterwards. The expensive part was reading the code once, carefully. That is the argument for the tree, and it is now evidenced rather than asserted.
