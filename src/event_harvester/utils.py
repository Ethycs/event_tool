"""Shared utility helpers."""

import json
from pathlib import Path


# ── INI parsing ───────────────────────────────────────────────────────────


def parse_llm_ini(raw: str) -> dict[str, dict[str, str]]:
    """Parse INI-formatted LLM output into {section: {key: value}} dict.

    Handles LLM quirks like markdown code fences.
    """
    import configparser

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines)

    parser = configparser.ConfigParser()
    try:
        parser.read_string(cleaned)
    except configparser.Error:
        return {}
    return {section: dict(parser[section]) for section in parser.sections()}


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
