"""Download every conversation in the signed-in Grok account.

Runs Grok's own web API from inside a signed-in grok.com page (in-page
``fetch`` with the session cookies), so Cloudflare and the session see a real
browser. Read-only: GET the list and each response tree, POST
``load-responses`` to read message bodies. Nothing is sent, renamed or deleted.

Writes, per conversation, ``<out>/json/<id>.json`` (list metadata, response
tree and full response objects as Grok returns them) and
``<out>/md/<created>_<title>_<id>.md`` (a readable transcript), plus
``<out>/index.json``. Re-running skips conversations whose ``modifyTime`` has
not changed, so an interrupted run resumes.

Session: set UC_STATE_FILE to a storage-state JSON holding grok.com cookies
(``data/.grok/state.json``), and UC_CHROME_PROFILE to a profile directory no
other process holds (the Grok proxy owns ``data/.grok``):

    UC_CHROME_PROFILE=$PWD/data/.grok-export/.chrome_profile \\
    UC_STATE_FILE=$PWD/data/.grok/state.json \\
    pixi run python scripts/export_grok_history.py [--out ~/grok-export]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from event_harvester.sources.uc_browser import BrowserMode, WebBrowser

API = "https://grok.com/rest/app-chat"
PAGE_SIZE = 60
BATCH = 100          # response ids per load-responses call
PAUSE_S = 0.4        # between requests: one person's browsing pace, not a crawl

_FETCH = """async ([url, method, body]) => {
  const r = await fetch(url, {
    method, credentials: 'include',
    headers: body ? {'content-type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  return {status: r.status, body: await r.text()};
}"""


def _call(page, url: str, method: str = "GET", body: dict | None = None) -> dict:
    for attempt in range(3):
        r = page.evaluate(_FETCH, [url, method, body])
        if r["status"] == 200:
            time.sleep(PAUSE_S)
            return json.loads(r["body"] or "{}")
        if r["status"] in (401, 403):
            raise SystemExit(
                f"Grok refused {method} {url.split('?')[0]} with HTTP {r['status']}: "
                "the session is signed out or blocked. Refresh UC_STATE_FILE.")
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{method} {url.split('?')[0]} failed: HTTP {r['status']}")


def list_conversations(page) -> list[dict]:
    out, token, seen_tokens = [], "", set()
    while True:
        url = f"{API}/conversations?pageSize={PAGE_SIZE}"
        if token:
            url += f"&pageToken={quote(token)}"
        data = _call(page, url)
        out.extend(data.get("conversations") or [])
        token = data.get("nextPageToken") or ""
        print(f"  listed {len(out)}", flush=True)
        if not token or token in seen_tokens:
            return out
        seen_tokens.add(token)


def fetch_conversation(page, cid: str) -> dict:
    tree = _call(page, f"{API}/conversations/{cid}/response-node?includeThreads=true")
    nodes = tree.get("responseNodes") or []
    ids = [n["responseId"] for n in nodes]
    responses: list[dict] = []
    for i in range(0, len(ids), BATCH):
        part = _call(page, f"{API}/conversations/{cid}/load-responses", "POST",
                     {"responseIds": ids[i:i + BATCH]})
        responses.extend(part.get("responses") or [])
    order = {rid: n for n, rid in enumerate(ids)}
    responses.sort(key=lambda r: (r.get("createTime") or "", order.get(r.get("responseId"), 0)))
    return {"responseNodes": nodes, "responses": responses}


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "untitled").strip("-").lower()
    return s[:60] or "untitled"


def to_markdown(meta: dict, convo: dict) -> str:
    lines = [f"# {meta.get('title') or 'Untitled'}", "",
             f"- id: `{meta['conversationId']}`",
             f"- url: https://grok.com/c/{meta['conversationId']}",
             f"- created: {meta.get('createTime')}", f"- modified: {meta.get('modifyTime')}", ""]
    for r in convo["responses"]:
        who = "You" if r.get("sender") == "human" else f"Grok ({r.get('model') or 'assistant'})"
        lines += [f"## {who} — {r.get('createTime')}", "", (r.get("message") or "").rstrip(), ""]
        for url in r.get("generatedImageUrls") or []:
            lines.append(f"![generated image](https://assets.grok.com/{url})")
        for f in r.get("fileAttachmentsMetadata") or []:
            lines.append(f"- attachment: {f.get('fileName') or f.get('fileMetadataId')}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path.home() / "grok-export")
    args = ap.parse_args()
    out: Path = args.out.expanduser()
    (out / "json").mkdir(parents=True, exist_ok=True)
    (out / "md").mkdir(parents=True, exist_ok=True)

    with WebBrowser(mode=BrowserMode.CHROMIUM_EXT) as uc:
        page = uc.new_page()
        page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        print("Listing conversations…", flush=True)
        convs = list_conversations(page)
        (out / "index.json").write_text(json.dumps(convs, indent=1), encoding="utf-8")

        done = skipped = failed = 0
        for n, meta in enumerate(convs, 1):
            cid = meta["conversationId"]
            jpath = out / "json" / f"{cid}.json"
            if jpath.exists():
                try:
                    if json.loads(jpath.read_text())["conversation"].get("modifyTime") == meta.get("modifyTime"):
                        skipped += 1
                        continue
                except (ValueError, KeyError):
                    pass
            try:
                convo = fetch_conversation(page, cid)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 — one bad conversation must not end the export
                failed += 1
                print(f"  [{n}/{len(convs)}] {cid} FAILED: {e}", flush=True)
                continue
            jpath.write_text(json.dumps({"conversation": meta, **convo}, indent=1), encoding="utf-8")
            day = (meta.get("createTime") or "")[:10] or "undated"
            (out / "md" / f"{day}_{_slug(meta.get('title'))}_{cid[:8]}.md").write_text(
                to_markdown(meta, convo), encoding="utf-8")
            done += 1
            if n % 25 == 0 or n == len(convs):
                print(f"  [{n}/{len(convs)}] saved {done}, unchanged {skipped}, failed {failed}", flush=True)

    print(f"Done: {len(convs)} conversations — saved {done}, unchanged {skipped}, failed {failed} -> {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
