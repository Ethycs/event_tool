"""Shared utility helpers."""

import json
from pathlib import Path

# ── Repo-anchored paths ───────────────────────────────────────────────────
#
# Every on-disk location the app owns hangs off REPO_ROOT, never off the
# current working directory. Two classes of bug motivated centralising this:
#
# 1. **CWD-relative paths.** `data/...` as a bare relative path resolves
#    against wherever the process was started. The stdio MCP server is
#    launched by whatever client spawns it, so it silently used a *different*
#    fingerprint store and event cache than the CLI — same repo, same config,
#    two sets of state.
# 2. **Hand-counted `.parents[N]`.** This file is `src/event_harvester/utils.py`,
#    so the root is `parents[2]` from here — but modules one level deeper need
#    `parents[3]`, and the repo has already shipped that off-by-one twice
#    (the classifier directory, then the message cache, which landed in
#    `src/data/`). Counting it once here means no other module counts at all.
#
# `parents[2]`: utils.py -> event_harvester -> src -> <repo root>.
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


def data_path(*parts: str) -> Path:
    """A path under the repo's ``data/`` directory, independent of the CWD."""
    return DATA_DIR.joinpath(*parts)


# ── INI parsing ───────────────────────────────────────────────────────────


def parse_llm_ini(raw: str) -> dict[str, dict[str, str]]:
    """Parse INI-formatted LLM output into {section: {key: value}} dict.

    Handles LLM quirks like markdown code fences.

    **`interpolation=None` is load-bearing.** `ConfigParser` treats `%` as a
    substitution syntax, so a perfectly ordinary event — a URL containing
    `%20`, a title saying "50% off" — raised `InterpolationSyntaxError` and
    **lost the whole batch of ten events**. Diagnosed 2026-08-12 on `erobay`,
    whose Calcium detail links are full of `%`: 121 messages reached the model
    and produced zero events, run after run.

    The failure was doubly hidden. `build_prompt` already escapes `%` on the
    way *in*, which made `%` look like a solved problem; nothing escaped the
    model's reply on the way *out*. And interpolation happens on **value
    access**, not on parse — so `read_string` inside the `try` succeeded and
    the exception came from the `dict(...)` comprehension below it, escaping
    the `except configparser.Error` that was meant to contain exactly this.
    """
    import configparser

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines)

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(cleaned)
        # Inside the try: reading values is its own failure point, and a
        # malformed reply must cost this batch, never the run.
        return {section: dict(parser[section]) for section in parser.sections()}
    except configparser.Error:
        return {}


# ── JSON I/O ──────────────────────────────────────────────────────────────


def load_json(path: Path | str, default=None):
    """Load JSON from file with standard error handling.

    Returns *default* (or empty dict) if file is missing or corrupt.
    """
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def save_json(path: Path | str, data, *, indent: int = 2) -> None:
    """Save data as JSON, creating parent directories as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=indent, ensure_ascii=False),
        encoding="utf-8",
    )
