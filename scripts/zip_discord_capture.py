r"""Bundle the messages the capture plugin recorded into a zip in Downloads.

The BetterDiscord `HarvesterMessageCapture` plugin appends every message you
load while browsing to per-channel JSONL under
``%APPDATA%\discord-harvester\messages``. It appends without deduping (simple
and crash-safe); this consolidates: dedup by message id per channel, sort by
timestamp, and write one zip with a manifest.

Run:  pixi run python scripts/zip_discord_capture.py
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def capture_dir() -> Path:
    override = os.environ.get("DISCORD_CAPTURE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "discord-harvester" / "messages"


def _dedup(path: Path) -> list[dict]:
    by_id: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("id"):
            by_id[rec["id"]] = rec           # last write wins (captures edits)
    return sorted(by_id.values(), key=lambda r: r.get("timestamp") or "")


def main() -> int:
    src = capture_dir()
    if not src.exists() or not any(src.glob("*.jsonl")):
        print(f"No captured messages at {src}")
        print("Install the HarvesterMessageCapture plugin and browse the "
              "Discord channels you want, scrolling up to load history.")
        return 1

    downloads = Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = downloads / f"discord_capture_{stamp}.zip"

    manifest = []
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.glob("*.jsonl")):
            msgs = _dedup(f)
            if not msgs:
                continue
            total += len(msgs)
            body = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs)
            z.writestr(f.name, body + "\n")
            first = msgs[0]
            manifest.append({
                "file": f.name,
                "guild": first.get("guild_name", ""),
                "channel": first.get("channel_name", ""),
                "messages": len(msgs),
                "earliest": msgs[0].get("timestamp"),
                "latest": msgs[-1].get("timestamp"),
            })
        z.writestr("manifest.json", json.dumps({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "total_messages": total,
            "channels": len(manifest),
            "chats": sorted(manifest, key=lambda x: -x["messages"]),
        }, indent=1, ensure_ascii=False))

    print(f"{total} message(s) across {len(manifest)} channel(s)")
    print(f"-> {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")
    for c in sorted(manifest, key=lambda x: -x["messages"])[:8]:
        print(f"  {c['messages']:>7}  {c['guild']} / {c['channel']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
