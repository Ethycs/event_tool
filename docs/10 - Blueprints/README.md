# Blueprints

**Status:** CURRENT · **Date:** 2026-08-04 · **Applies to:** repeatable operator procedures

A **blueprint** is a procedure you run more than once, written so the next run
is identical and so the *judgement* in it is explicit rather than tacit.

## What separates a blueprint from the neighbouring folders

| Folder | Answers |
|---|---|
| `02 - Implementation` | *How do I stand this up?* — one-time or occasional setup |
| `04 - Reference` | *What does this flag do?* — derived from the code, exhaustive |
| **`10 - Blueprints`** | *What do I do, in order, and where does it usually go wrong?* |

A CLI reference tells you `web test` exists. A blueprint tells you to run it
against three candidate URLs before you pick one, and why the homepage is
usually the wrong answer.

## Rules

1. **Every step is a command or a decision, never "check that it looks right".**
   If a step needs judgement, name the signal and the threshold.
2. **Record the failure the step exists to prevent.** A step with no failure
   behind it is ceremony and will be skipped.
3. **Cite real runs.** A blueprint claiming a procedure works must name a case
   where it did, with numbers. Blueprints rot exactly like architecture docs.
4. **Cheap checks before expensive ones.** Anything that avoids launching a
   browser or spending LLM tokens goes earlier.

## Index

| Blueprint | Run when |
|---|---|
| `01 - Adding a Web Source.md` | Someone hands you a URL and wants its events harvested |
| `02 - Re-running Web Sources Fast.md` | You're iterating on one source (parser/mode/pipeline) and don't want to re-run the whole list |
