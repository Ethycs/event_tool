Read `docs/04 - Reference/Pixi CLI Reference.md` to properly use the python env including pip.
Never run bare `python` — everything goes through `pixi run`.

## Documentation

All docs live in a numbered tree under `docs/`. Read `docs/README.md` first — it maps
folder → kind of claim. The rules for writing docs are in
`docs/05 - Standards/EH-STD-DOCS-v0 — Agentic Technique Master.md`; follow them.

| Need | Read |
|---|---|
| the system map | `docs/03 - Architecture/00 - System Overview.md` |
| a subsystem's as-built detail | `docs/03 - Architecture/` (one doc per subsystem) |
| a data shape (message / event / web source) | `docs/05 - Standards/` — normative, frozen |
| setup, credentials, deploy, dev loop | `docs/02 - Implementation/` |
| open work | `docs/06 - Roadmaps/` |
| what the last session did | newest file in `docs/07 - Status Reports/` |

When you change code that a doc describes, reconcile the doc in the same pass. If you
cannot, add a drift note to `docs/000 - Doc Maintenance/` — do not leave it unmarked.
