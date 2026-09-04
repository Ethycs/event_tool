"""argparse type helpers and post-parse normalization for the CLI.

Centralizes parsing logic so the parser stays declarative and command
modules receive clean, validated inputs.
"""

import argparse

# ── Validation sets ──────────────────────────────────────────────────

VALID_PLATFORMS = frozenset({"discord", "telegram", "gmail", "signal", "web"})
VALID_CAP_KEYS = frozenset({"discord", "telegram", "gmail", "signal", "web", "total"})


# ── argparse type functions ──────────────────────────────────────────


def parse_cap_arg(raw: str) -> dict[str, int]:
    """Parse 'discord=20,telegram=30' into {'discord': 20, 'telegram': 30}.

    Used as ``type=parse_cap_arg`` on ``--cap`` (action='append'), so a
    single CLI invocation can mix forms:

        --cap discord=20,telegram=30 --cap total=200

    Raises ArgumentTypeError on invalid keys, non-int values, or
    negative numbers — argparse converts this to SystemExit with a
    helpful error.
    """
    out: dict[str, int] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise argparse.ArgumentTypeError(
                f"--cap expects key=value, got {piece!r}"
            )
        k, v = piece.split("=", 1)
        k = k.strip().lower()
        if k not in VALID_CAP_KEYS:
            raise argparse.ArgumentTypeError(
                f"unknown cap key {k!r}; valid: {sorted(VALID_CAP_KEYS)}"
            )
        try:
            n = int(v.strip())
        except ValueError as e:
            raise argparse.ArgumentTypeError(
                f"--cap {k} must be an integer, got {v!r}"
            ) from e
        # -1 is "unlimited" — the config default since 2026-08-11 and the way
        # `prioritize_with_caps` has always spelled it. Rejecting it here left
        # the flag unable to express the value the config itself defaults to.
        # Any other negative normalises to -1 rather than erroring, matching
        # `config._clamp_cap`, so the two paths cannot disagree.
        if n < 0:
            n = -1
        out[k] = n
    return out


def parse_web_sources_csv(raw: str) -> list[str]:
    """Parse one ``--web-source`` value: ``'erobay,luma'`` -> ``['erobay', 'luma']``.

    Used as ``type=`` on ``--web-source`` (``action='append'``), so a targeted
    re-run can name a *set* of sites either way, or both at once:

        --web-source erobay,luma --web-source ra-sf

    Names are not validated here — they are matched against
    ``data/web_sources.json`` at fetch time, and a name with no match is simply
    skipped (with a warning) rather than being a parse error.
    """
    return [n.strip() for n in raw.split(",") if n.strip()]


def flatten_web_sources(appended) -> list[str] | None:
    """Flatten ``--web-source`` occurrences into one name list.

    ``action='append'`` with ``type=parse_web_sources_csv`` yields a list of
    lists; a profile or back-compat caller may hand a bare string. Return an
    order-preserving, de-duplicated list, or ``None`` when the flag was unset —
    which the pipeline reads as "all web sources".
    """
    if not appended:
        return None
    if isinstance(appended, str):
        appended = [appended]
    names: list[str] = []
    for group in appended:
        items = group if isinstance(group, (list, tuple)) else str(group).split(",")
        for n in items:
            n = str(n).strip()
            if n and n not in names:
                names.append(n)
    return names or None


def parse_platform_csv(raw: str) -> set[str]:
    """Parse 'discord,telegram' into {'discord', 'telegram'}.

    Used as ``type=parse_platform_csv`` on ``--only`` and ``--skip``.
    Raises on unknown platform names.
    """
    items = {p.strip().lower() for p in raw.split(",") if p.strip()}
    bad = items - VALID_PLATFORMS
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown platform(s): {sorted(bad)}; valid: {sorted(VALID_PLATFORMS)}"
        )
    return items


# ── Post-parse normalization ─────────────────────────────────────────


def resolve_platforms(only: set[str] | None, skip: set[str] | None) -> dict[str, bool]:
    """Translate --only/--skip into the no_* kwargs harvest_messages expects.

    Returns a dict like {"no_discord": False, "no_telegram": True, ...}
    suitable for splatting into harvest_messages(**resolve_platforms(...)).
    """
    if only is not None:
        active = only
    elif skip is not None:
        active = VALID_PLATFORMS - skip
    else:
        active = VALID_PLATFORMS
    return {f"no_{p}": (p not in active) for p in VALID_PLATFORMS}


def apply_caps_to_config(cfg, cap_dicts: list[dict[str, int]]) -> None:
    """Merge a list of {key: value} cap dicts into cfg.caps in place.

    Each --cap on the command line produces one dict (because action='append').
    Later values override earlier ones for the same key.
    """
    if not cap_dicts:
        return
    merged: dict[str, int] = {}
    for d in cap_dicts:
        merged.update(d)
    for key, value in merged.items():
        setattr(cfg.caps, key, value)
