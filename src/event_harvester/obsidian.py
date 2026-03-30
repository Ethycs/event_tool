"""Obsidian-compatible markdown report generation and recruiter reparser."""

import logging
import re
import tempfile
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path

from event_harvester.recruiter_score import RecruiterGrade
from event_harvester.report import _ticktick_deep_link

logger = logging.getLogger("event_harvester.obsidian")

GMAIL_URL = "https://mail.google.com/mail/u/0/#all/{msg_id}"


# ── Helpers ────────────────────────────────────────────────────────────


def _run_date(d: date | None = None) -> date:
    return d or date.today()


def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp file then atomically replace (avoids OneDrive conflicts)."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".obsidian_"
    )
    try:
        with open(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp_path).replace(path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


# ── Events Report ─────────────────────────────────────────────────────


def write_events_report(
    validated_events: list[dict],
    raw_events: list[dict],
    links: list[dict],
    source_counts: dict[str, int],
    total_messages: int,
    output_dir: str,
    run_date: date | None = None,
) -> str:
    """Write an Obsidian-compatible events markdown file.

    Returns the output file path.
    """
    d = _run_date(run_date)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    events = validated_events if validated_events else raw_events
    sources = [name for name, count in source_counts.items() if count > 0]
    sources_display = " + ".join(s.capitalize() for s in sources)

    lines: list[str] = []

    # Frontmatter
    lines.append("---")
    lines.append(f"date: {d.isoformat()}")
    lines.append("type: events")
    lines.append(f"sources: {_yaml_list(sources)}")
    lines.append(f"event_count: {len(events)}")
    lines.append(f"link_count: {len(links)}")
    lines.append(f"message_count: {total_messages}")
    lines.append(f"tags: {_yaml_list(['events-pool', 'harvester'])}")
    lines.append("---")
    lines.append("")

    # Header
    lines.append(f"# Events {d.isoformat()}")
    lines.append("")
    lines.append(
        f"> [!info] Harvested from {sources_display} ({total_messages} messages)"
    )
    lines.append(f"> Run: {now}")
    lines.append("")

    # Events section
    if events:
        lines.append("## Events")
        lines.append("")

        for i, ev in enumerate(events, 1):
            _append_obsidian_event(lines, i, ev)

    # Links table
    if links:
        lines.append("## Top Links")
        lines.append("")
        lines.append("| Score | Link | Author |")
        lines.append("|-------|------|--------|")
        for lnk in links[:15]:
            score = lnk.get("score", 0)
            url = lnk.get("url", "")
            author = lnk.get("author", "")
            # Truncate URL display but keep full href
            display = url[:60] + "..." if len(url) > 60 else url
            lines.append(f"| {score} | [{display}]({url}) | @{author} |")
        lines.append("")

    # Cross-reference
    lines.append("---")
    lines.append(f"> [!note] See also: [[Recruiters {d.isoformat()}]]")
    lines.append("")

    out_path = Path(output_dir) / f"Events {d.isoformat()}.md"
    _atomic_write(out_path, "\n".join(lines))
    logger.info("Obsidian events report -> %s", out_path)
    return str(out_path)


def _append_obsidian_event(lines: list[str], index: int, ev: dict) -> None:
    """Append one event block in Obsidian format."""
    title = ev.get("title") or ev.get("content", "Untitled")[:80]
    if len(title) > 80:
        title = title[:77] + "..."

    score = ev.get("score", 0)
    author = ev.get("author", "")
    channel = ev.get("source") or ev.get("channel", "")
    timestamp = ev.get("timestamp", "")
    details = ev.get("details", "")
    content_snippet = details or ev.get("content", "") or ev.get("original", "")
    if len(content_snippet) > 200:
        content_snippet = content_snippet[:197] + "..."

    # Tags
    tags = "#event"
    if ev.get("scheduling") or ev.get("pinned"):
        tags += " #scheduled" if ev.get("scheduling") else ""
        tags += " #pinned" if ev.get("pinned") else ""

    # Date — use LLM-resolved date, then local best_date, then raw lists
    resolved_date = ev.get("date") or ev.get("best_date") or ""
    all_day = ev.get("all_day", False)
    raw_dates = ev.get("dates", [])
    raw_times = ev.get("times", [])

    lines.append(f"### {index}. {title}")
    lines.append("")
    lines.append(f"- **Score**: {score} {tags}")
    lines.append(f"- **From**: @{author} in `{channel}`")
    if timestamp:
        lines.append(f"- **Posted**: {timestamp[:16]}")
    if resolved_date:
        date_display = resolved_date
        if all_day:
            date_display += " (all day)"
        lines.append(f"- **Date**: {date_display}")
    else:
        if raw_dates:
            lines.append(
                f"- **Dates mentioned**: {', '.join(str(d) for d in raw_dates)}"
            )
        if raw_times:
            lines.append(
                f"- **Times mentioned**: {', '.join(str(t) for t in raw_times)}"
            )

    lines.append("")
    lines.append(f"> {content_snippet}")
    lines.append("")

    # TickTick deep link
    link_content = f"From @{author} in {channel}"
    deep_link = _ticktick_deep_link(title, resolved_date, all_day, link_content)
    lines.append(f"- [ ] [Add to TickTick]({deep_link})")
    lines.append("- [ ] Decide: attend or skip")
    lines.append("")
    lines.append("---")
    lines.append("")


# ── Recruiter Report ──────────────────────────────────────────────────


def write_recruiter_report(
    grades: list[RecruiterGrade],
    output_dir: str,
    run_date: date | None = None,
) -> str:
    """Write an Obsidian-compatible recruiter grades markdown file.

    Returns the output file path.
    """
    d = _run_date(run_date)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    counts = {
        "respond": sum(1 for g in grades if g.action == "respond"),
        "review": sum(1 for g in grades if g.action == "review"),
        "ignore": sum(1 for g in grades if g.action == "ignore"),
        "trash": sum(1 for g in grades if g.action == "trash"),
    }

    lines: list[str] = []

    # Frontmatter
    lines.append("---")
    lines.append(f"date: {d.isoformat()}")
    lines.append("type: recruiter-grades")
    lines.append(f"total: {len(grades)}")
    for action, count in counts.items():
        lines.append(f"{action}: {count}")
    lines.append(f"tags: {_yaml_list(['recruiters', 'harvester'])}")
    lines.append("---")
    lines.append("")

    # Header
    lines.append(f"# Recruiters {d.isoformat()}")
    lines.append("")
    lines.append(
        f"> [!info] Graded {len(grades)} recruiter emails | "
        f"See also: [[Events {d.isoformat()}]]"
    )
    lines.append(f"> Run: {now}")
    lines.append("")

    # Group by action tier
    tiers = [
        ("Respond", "respond", ">= 66", "important", True),
        ("Review", "review", "46-65", "warning", True),
        ("Ignore", "ignore", "21-45", None, False),
        ("Trash", "trash", "<= 20", "caution", False),
    ]

    for tier_name, action, score_range, callout, actionable in tiers:
        tier_grades = [g for g in grades if g.action == action]
        if not tier_grades:
            continue

        lines.append(f"## {tier_name} ({score_range}) #{action}")
        lines.append("")
        if callout:
            callout_msgs = {
                "important": "These need a reply",
                "warning": "Worth a second look",
                "caution": "Auto-trashed or safe to ignore",
            }
            lines.append(f"> [!{callout}] {callout_msgs.get(callout, '')}")
            lines.append("")

        for g in tier_grades:
            _append_recruiter_item(lines, g, action)

    if not grades:
        lines.append("No recruiter emails to grade.")
        lines.append("")

    out_path = Path(output_dir) / f"Recruiters {d.isoformat()}.md"
    _atomic_write(out_path, "\n".join(lines))
    logger.info("Obsidian recruiter report -> %s", out_path)
    return str(out_path)


def _append_recruiter_item(
    lines: list[str], grade: RecruiterGrade, action: str,
) -> None:
    """Append one recruiter email item."""
    gmail_url = GMAIL_URL.format(msg_id=grade.message_id)

    if action == "trash":
        # Pre-checked, strikethrough links
        lines.append(f"- [x] **{grade.subject}** ({grade.score}) #{action}")
        lines.append(f"  - From: {grade.sender}")
        for reason in grade.reasons[:3]:
            lines.append(f"  - {reason}")
        lines.append(f"  - ~~[Open in Gmail]({gmail_url})~~ Trashed")
    elif action == "ignore":
        # Plain bullet, still has action links
        lines.append(f"- **{grade.subject}** ({grade.score}) #{action}")
        lines.append(f"  - From: {grade.sender}")
        for reason in grade.reasons[:3]:
            lines.append(f"  - {reason}")
        lines.append(
            f"  - [Open in Gmail]({gmail_url}) | "
            f"[Trash](event-harvester://trash/{grade.message_id})"
        )
    else:
        # respond / review — unchecked checkbox with full action links
        lines.append(f"- [ ] **{grade.subject}** ({grade.score}) #{action}")
        lines.append(f"  - From: {grade.sender}")
        for reason in grade.reasons[:3]:
            lines.append(f"  - {reason}")
        action_links = (
            f"[Open in Gmail]({gmail_url}) | "
            f"[Trash](event-harvester://trash/{grade.message_id})"
        )
        lines.append(f"  - {action_links}")

    lines.append("")


# ── Reparser ──────────────────────────────────────────────────────────

_GMAIL_ID_RE = re.compile(r"mail\.google\.com/mail/u/0/#all/([a-zA-Z0-9]+)")
_ITEM_RE = re.compile(
    r"^- \[[ x]\] \*\*(.+?)\*\* \((\d+)\) #(respond|review)",
    re.MULTILINE,
)


def reparse_recruiter_report(filepath: str, gmail_cfg) -> None:
    """Interactive reparser: open, reply, trash recruiter emails from an Obsidian report.

    Reads the markdown, finds actionable items (respond/review), and prompts
    the user for each one.
    """
    from event_harvester.sources import gmail_reply, gmail_trash

    path = Path(filepath)
    if not path.exists():
        print(f"File not found: {filepath}")
        return

    content = path.read_text(encoding="utf-8")
    md_lines = content.split("\n")

    # Find all actionable items with their Gmail message IDs
    items: list[dict] = []
    for i, line in enumerate(md_lines):
        match = _ITEM_RE.match(line)
        if not match:
            continue
        subject, score, action = match.group(1), int(match.group(2)), match.group(3)

        # Look ahead for Gmail URL to extract message ID
        msg_id = None
        for j in range(i + 1, min(i + 6, len(md_lines))):
            id_match = _GMAIL_ID_RE.search(md_lines[j])
            if id_match:
                msg_id = id_match.group(1)
                break

        if msg_id:
            items.append({
                "subject": subject,
                "score": score,
                "action": action,
                "message_id": msg_id,
                "line_index": i,
            })

    if not items:
        print("No actionable recruiter items found in report.")
        return

    print(f"\nFound {len(items)} actionable recruiter email(s).\n")
    modified = False

    for item in items:
        gmail_url = GMAIL_URL.format(msg_id=item["message_id"])
        print(f"  [{item['action'].upper()}] {item['subject']} (score: {item['score']})")
        print(f"  Gmail: {gmail_url}")
        print()

        while True:
            choice = input("  [o]pen  [r]eply  [t]rash  [s]kip  [q]uit > ").strip().lower()
            if choice in ("o", "open"):
                webbrowser.open(gmail_url)
                print("  Opened in browser.\n")
                continue  # Let them choose another action after opening
            elif choice in ("r", "reply"):
                print("  Type reply (empty line to cancel):")
                reply_text = input("  > ").strip()
                if reply_text:
                    sent_id = gmail_reply(gmail_cfg, item["message_id"], reply_text)
                    if sent_id:
                        print(f"  Replied (sent: {sent_id}).\n")
                        # Mark as checked in markdown
                        md_lines[item["line_index"]] = md_lines[item["line_index"]].replace(
                            "- [ ]", "- [x]", 1,
                        )
                        modified = True
                    else:
                        print("  Reply failed.\n")
                else:
                    print("  Cancelled.\n")
                break
            elif choice in ("t", "trash"):
                if gmail_trash(gmail_cfg, item["message_id"]):
                    print("  Trashed.\n")
                    md_lines[item["line_index"]] = md_lines[item["line_index"]].replace(
                        "- [ ]", "- [x]", 1,
                    )
                    modified = True
                else:
                    print("  Trash failed.\n")
                break
            elif choice in ("s", "skip"):
                print()
                break
            elif choice in ("q", "quit"):
                print("  Done.\n")
                if modified:
                    _atomic_write(path, "\n".join(md_lines))
                    print(f"  Updated: {filepath}")
                return
            else:
                print("  Invalid choice. Try o/r/t/s/q.")

    if modified:
        _atomic_write(path, "\n".join(md_lines))
        print(f"\nUpdated: {filepath}")
