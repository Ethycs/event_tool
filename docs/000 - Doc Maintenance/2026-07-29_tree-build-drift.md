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
├── 04 - Reference/                6 docs — UC, pixi, CLI, MCP, glossary ✅
├── 05 - Standards/                4 docs — docs style + 3 record specs  🟡 v0 DRAFT, not yet frozen
├── 06 - Roadmaps/                 1 doc — gap analysis                  ✅ supersedes TODO.md
├── 07 - Status Reports/           1 doc — this session's handoff        ✅
├── 08 - Analysis/                 3 docs — browser log, what-worked,    ⚠️ browser log cites missing scripts
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
