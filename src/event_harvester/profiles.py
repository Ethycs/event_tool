"""Named pipeline profiles — preset web-sources + caps.

A profile bundles:
- a list of web sub-source names from data/web_sources.json
- a partial caps dict (any of discord/telegram/gmail/signal/web/total/no_cap
  plus any web-sub-source name like ``luma=25``)

Profiles do NOT touch top-level source toggles, days_back, or behavior
flags — those remain the user's persistent choices. Hand-edit
``data/profiles.json`` to manage them; there is intentionally no in-UI
Save/Delete.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("event_harvester.profiles")

PROFILES_FILE = Path("data/profiles.json")


class ProfileError(ValueError):
    """Raised when data/profiles.json is malformed."""


@dataclass
class Profile:
    name: str
    description: str = ""
    web_sources: list[str] = field(default_factory=list)
    caps: dict[str, Any] = field(default_factory=dict)


def load_profiles(path: Path | None = None) -> list[Profile]:
    """Load profiles from JSON. Returns [] if the file is missing.

    Raises ProfileError for malformed JSON or duplicate names.
    """
    if path is None:
        path = PROFILES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProfileError(f"{path} is not valid JSON: {e}") from e

    raw = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ProfileError(
            f"{path} must contain a top-level 'profiles' list."
        )

    profiles: list[Profile] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ProfileError(f"{path}[{i}] is not an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProfileError(f"{path}[{i}] missing required 'name'")
        if name in seen:
            raise ProfileError(f"{path}: duplicate profile name {name!r}")
        seen.add(name)
        profiles.append(Profile(
            name=name,
            description=entry.get("description", "") or "",
            web_sources=list(entry.get("web_sources") or []),
            caps=dict(entry.get("caps") or {}),
        ))
    return profiles


def find_profile(profiles: list[Profile], name: str) -> Profile | None:
    """Case-sensitive name lookup."""
    for p in profiles:
        if p.name == name:
            return p
    return None


def apply_profile_by_name(cfg, name: str) -> bool:
    """Convenience: load profiles, find by ``name``, apply to ``cfg``.

    Returns True on success, False (and logs a clear error) if the
    profile doesn't exist or the file is missing.
    """
    profiles = load_profiles()
    p = find_profile(profiles, name)
    if p is None:
        if not profiles:
            logger.error(
                "Profile %r requested but %s is missing or empty.",
                name, PROFILES_FILE,
            )
        else:
            logger.error(
                "Profile %r not found. Available: %s",
                name, ", ".join(p.name for p in profiles),
            )
        return False
    apply_profile(cfg, p)
    return True


def apply_profile(cfg, profile: Profile) -> None:
    """Mutate ``cfg`` to reflect ``profile``.

    - Sets ``cfg.web.selected_names`` to the profile's web sub-source list.
    - Merges ``profile.caps`` into ``cfg.caps`` via the existing
      ``apply_caps_to_config`` helper so keys like ``luma=25`` extend
      ``CapConfig`` dynamically the same way ``--cap luma=25`` does.

    Top-level source toggles, days_back, and behavior flags are left
    untouched on purpose.
    """
    # Defer the import to avoid a cli/ ↔ event_harvester/ circular
    # dependency at module load time.
    from event_harvester.cli.parse_helpers import apply_caps_to_config

    cfg.web.selected_names = list(profile.web_sources)
    if profile.caps:
        apply_caps_to_config(cfg, [profile.caps])
    logger.info(
        "Applied profile %r: web_sources=%s, caps=%s",
        profile.name, profile.web_sources or "(all)", profile.caps or "(none)",
    )
