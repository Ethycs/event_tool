"""Local event review server.

Generates a markdown report with approve/decline links pointing to a local
HTTP server. The server handles the actions (TickTick create, fingerprint save)
so the markdown can be viewed in any renderer (VS Code, Obsidian, browser).

Usage:
    pixi run event-harvester --serve
    # Writes events_report.md with action links
    # Runs server at http://localhost:8111 to handle approve/decline clicks
"""

import json
import logging
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger("event_harvester.server")

_PORT = 8111
_DATA_DIR = Path("data")
_EVENTS_FILE = _DATA_DIR / ".server_events.json"


def _save_events(events: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _EVENTS_FILE.write_text(
        json.dumps(events, indent=2, default=str), encoding="utf-8",
    )


def _load_events() -> list[dict]:
    if not _EVENTS_FILE.exists():
        return []
    return json.loads(_EVENTS_FILE.read_text(encoding="utf-8"))


def generate_serve_report(
    events: list[dict],
    output_path: str = "events_report.md",
    port: int = _PORT,
) -> str:
    """Generate a markdown report with approve/decline links to the local server.

    Each event gets:
    - [Approve](http://localhost:8111/approve/0) → creates TickTick task
    - [Decline](http://localhost:8111/decline/0) → saves fingerprint (skip next run)
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = f"http://localhost:{port}"

    lines = [
        "# Event Harvester Report",
        "",
        f"Generated: {now} | {len(events)} events",
        "",
        "---",
        "",
    ]

    for i, ev in enumerate(events):
        title = ev.get("title", "Untitled")
        date_val = ev.get("date", "")
        time_val = ev.get("time", "")
        location = ev.get("location", "")
        link = ev.get("link", "")
        source = ev.get("source", "")
        details = (ev.get("details") or ev.get("notes") or "")[:300]

        lines.append(f"### {i+1}. {title}")
        lines.append("")

        meta = []
        if date_val:
            meta.append(f"**{date_val}**")
        if time_val:
            meta.append(time_val)
        if location:
            meta.append(location)
        if meta:
            lines.append(" | ".join(meta))
            lines.append("")

        if link:
            lines.append(f"[{link}]({link})")
            lines.append("")

        if details:
            lines.append(f"> {details}")
            lines.append("")

        if source:
            lines.append(f"*via {source}*")
            lines.append("")

        lines.append(
            f"[Approve]({base}/approve/{i}) | "
            f"[Decline]({base}/decline/{i})"
        )
        lines.append("")
        lines.append("---")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return output_path


class _Handler(SimpleHTTPRequestHandler):
    """Handles approve/decline requests from markdown link clicks."""

    events: list[dict] = []

    def do_GET(self):
        path = self.path.rstrip("/")

        if path.startswith("/approve/"):
            self._handle_action(path, "approve")
        elif path.startswith("/decline/"):
            self._handle_action(path, "decline")
        elif path == "/status":
            self._serve_status()
        else:
            self._serve_text("Event review server running. Click links in the markdown report.")

    def _handle_action(self, path: str, action: str):
        try:
            idx = int(path.split("/")[-1])
        except (ValueError, IndexError):
            self._serve_text("Invalid event index.", code=400)
            return

        if idx < 0 or idx >= len(self.events):
            self._serve_text(f"Event {idx} not found.", code=404)
            return

        ev = self.events[idx]
        title = ev.get("title", "Untitled")

        if action == "approve":
            ev["_status"] = "approved"
            try:
                from event_harvester.ticktick import create_ticktick_tasks
                result = create_ticktick_tasks([ev])
                msg = f"Approved: {title}\n\nTickTick: {result}"
                logger.info("Approved [%d] %s → %s", idx, title, result)
            except Exception as e:
                msg = f"Approved: {title}\n\nTickTick failed: {e}"
                logger.warning("Approve [%d] TickTick failed: %s", idx, e)

        elif action == "decline":
            ev["_status"] = "declined"
            try:
                from event_harvester.event_match import save_fingerprint
                save_fingerprint(ev)
                msg = f"Declined: {title}\n\nFingerprint saved — will be skipped on future runs."
                logger.info("Declined [%d] %s", idx, title)
            except Exception as e:
                msg = f"Declined: {title}\n\nFingerprint save failed: {e}"
                logger.warning("Decline [%d] fingerprint failed: %s", idx, e)
        else:
            msg = "Unknown action."

        _save_events(self.events)
        self._serve_text(msg)

    def _serve_status(self):
        approved = sum(1 for e in self.events if e.get("_status") == "approved")
        declined = sum(1 for e in self.events if e.get("_status") == "declined")
        pending = len(self.events) - approved - declined
        self._serve_text(
            f"Events: {len(self.events)} total\n"
            f"Approved: {approved}\n"
            f"Declined: {declined}\n"
            f"Pending: {pending}"
        )

    def _serve_text(self, text: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # suppress default logging


def serve_events(events: list[dict], port: int = _PORT) -> None:
    """Start the event review server and generate the markdown report.

    The report contains clickable approve/decline links. Open it in any
    markdown viewer (VS Code, Obsidian, browser) and click to take action.
    """
    _save_events(events)
    _Handler.events = events

    report_path = generate_serve_report(events, port=port)

    server = HTTPServer(("localhost", port), _Handler)
    print(f"\n  Report written to {report_path}")
    print(f"  Review server running at http://localhost:{port}")
    print(f"  Open {report_path} and click Approve/Decline links.")
    print("  http://localhost:{}/status for summary.".format(port))
    print("  Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        approved = sum(1 for e in events if e.get("_status") == "approved")
        declined = sum(1 for e in events if e.get("_status") == "declined")
        pending = len(events) - approved - declined
        print(f"\n  Done: {approved} approved, {declined} declined, {pending} pending.")
