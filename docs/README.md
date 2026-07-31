# event_tool — Documentation Tree

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** all documentation under `docs/`
**Depends on:** `docs/05 - Standards/EH-STD-DOCS-v0 — Agentic Technique Master.md`

Every folder is a prerequisite for the ones after it: theory grounds design, design precedes implementation, implementation produces the architecture that reference and standards describe. Find a doc by asking *what kind of claim am I making* — a rationale, a procedure, a map, a rule, a plan, a snapshot, or a measurement — and the folder follows.

## Hard rules (non-negotiable)

1. **A doc's folder is determined by its lane, not its topic.** The web fetch path appears in five folders; each copy makes a different kind of claim. Do not merge them.
2. **`05 - Standards` is frozen after release.** Change it by versioned amendment (`v0` → `v1`), never by edit.
3. **`09 - Archived` is append-only.** An archived doc is a snapshot; never edit one in place.
4. **Dated docs carry `YYYY-MM-DD_` prefixes and the date is load-bearing** — it is how a reader judges staleness.
5. **A doc is trustworthy only if it has been reconciled since the last substantial change to what it describes.** Drift you cannot fix now goes in `000 - Doc Maintenance` as a note, not in your head.

## The tree

| Folder | Purpose | Freeze policy |
|---|---|---|
| `000 - Doc Maintenance` | Drift / tree-health notes | Append-only; action or explicitly defer |
| `00 - Theory` | The scoring/gating/identity model, and the navigation model | Stable; corrections only. A `DRAFT` here is an argument, not a plan — see the freeze note below |
| `01 - Design` | Why the system is shaped this way | Archive when superseded |
| `02 - Implementation` | Setup, credentials, deploy, dev loop | Versioned with the code |
| `03 - Architecture` | The as-built system map — one doc per subsystem | Keep current; single trusted map |
| `04 - Reference` | External material + user-facing references | Living |
| `05 - Standards` | Normative record schemas (`EH-STD-*`) | **Frozen after release** |
| `06 - Roadmaps` | Open work, gap analysis | Living |
| `07 - Status Reports` | Dated session/agent handoffs | Archive when complete |
| `08 - Analysis` | Measured deep-dives, attempt logs | Living |
| `09 - Archived` | Superseded material and raw transcripts | Append-only; never edit |

## Where to start

| You are… | Read |
|---|---|
| new to the codebase | `03 - Architecture/00 - System Overview.md` |
| standing the project up | `02 - Implementation/00 - Environment & Toolchain.md`, then `01 - Credentials & First Run.md` |
| deploying to a server | `02 - Implementation/02 - Deploy Playbook.md` |
| changing the pipeline | `03 - Architecture/01 - Pipeline.md` + `00 - Theory/00 - Ranking, Gating and Event Identity.md` |
| changing a data shape | `05 - Standards/` — the record schemas are the contract |
| picking up work cold | the newest file in `07 - Status Reports/`, then `06 - Roadmaps/` |
| debugging the web path | `03 - Architecture/03 - Sources — Web.md`, `04 - Observability.md`, `08 - Analysis/00 - Browser Configuration Attempt Log.md` |
| changing how sites are navigated | `00 - Theory/01 - Site Navigation as a Planning Problem.md` + `05 - Standards/EH-STD-WEBSRC-v0 §3.2a` |

## Adding a doc

1. Pick the lane, then the folder, using the table above.
2. Use the matching template in `05 - Standards/EH-STD-DOCS-v0 — Agentic Technique Master.md` (§ Document-type templates).
3. Open with a status block. Never with "This document describes…".
4. Mark what is unproven — `BUG:`, `unverified`, `placeholder` — wherever a reader might assume more certainty than exists.
5. Close actionable docs with a **Verification** block: a command to run and a decision to make from its output.
6. Numeric prefixes order *sequential* docs; date prefixes order *dated* docs; descriptive names cover the rest.

## Verification

```powershell
Get-ChildItem docs -Recurse -Filter *.md | Select-Object FullName
```

Every file listed should sit in a folder whose purpose matches the kind of claim the file makes. One that does not is either misfiled or is two docs wearing one filename.
