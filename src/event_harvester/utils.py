"""Shared utility helpers."""

import json
from pathlib import Path


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
