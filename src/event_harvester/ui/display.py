"""Terminal colours and message display."""

import logging

logger = logging.getLogger("event_harvester")

# ── Terminal colours ──────────────────────────────────────────────────────────

BLUE = "\033[94m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

PLATFORM_COLOUR = {"discord": BLUE, "telegram": CYAN}


def print_message(msg: dict) -> None:
    colour = PLATFORM_COLOUR.get(msg["platform"], "")
    ts = msg["timestamp"][:16].replace("T", " ")
    pin = f" {YELLOW}[PINNED]{RESET}" if msg.get("pinned") else ""
    print(
        f"{colour}{BOLD}[{msg['platform'].upper()}]{RESET} "
        f"{DIM}{ts}{RESET} "
        f"{BOLD}{msg['channel']}{RESET} "
        f"{DIM}@{msg['author']}{RESET}{pin}\n"
        f"  {msg['content']}\n"
    )


# ── Weighted analysis display ─────────────────────────────────────────────────


def print_links(links: list[dict], max_links: int = 15) -> None:
    """Print weighted links section."""
    W = 64
    if not links:
        return
    print(f"{'=' * W}")
    print(f"  {BOLD}Links{RESET}  {DIM}(recency x type){RESET}")
    print(f"{'=' * W}\n")
    for i, lnk in enumerate(links[:max_links], 1):
        score_color = GREEN if lnk["score"] >= 8 else YELLOW if lnk["score"] >= 5 else DIM
        pin_tag = f" {YELLOW}[PIN]{RESET}" if lnk.get("pinned") else ""
        print(
            f"  {score_color}{lnk['score']:4.1f}{RESET}{pin_tag}  "
            f"{lnk['url'][:90]}"
        )
        ctx = lnk["context"].strip()
        url_only = ctx == lnk["url"][:120].strip()
        meta = f"@{lnk['author']} {DIM}{lnk['timestamp']}{RESET}"
        if not url_only and ctx:
            ctx_short = ctx[:80]
            if ctx_short != ctx:
                ctx_short += "..."
            print(f"         {DIM}\"{ctx_short}\"{RESET}")
            print(f"         {meta}")
        else:
            print(f"         {meta}")
        print()


def print_validated_events(events: list[dict], max_events: int = 20) -> None:
    """Print LLM-validated events."""
    W = 64
    if not events:
        print(f"{DIM}No validated events.{RESET}\n")
        return
    print(f"{'=' * W}")
    print(f"  {BOLD}Validated Events{RESET}  {DIM}(LLM-filtered){RESET}")
    print(f"{'=' * W}\n")
    for i, evt in enumerate(events[:max_events], 1):
        pin_tag = f" {YELLOW}[PIN]{RESET}" if evt.get("pinned") else ""
        score = evt.get("score", 0)
        score_color = GREEN if score >= 10 else YELLOW if score >= 6 else DIM

        title = evt.get("title", "Untitled")
        date = evt.get("date") or "TBD"
        source = evt.get("source", "?")
        author = evt.get("author", "?")
        details = evt.get("details", "")

        print(
            f"  {score_color}{score:2}{RESET}{pin_tag}  "
            f"{BOLD}{title}{RESET}"
        )
        print(f"         {CYAN}{date}{RESET} in {source} (@{author})")
        if details:
            print(f"         {DIM}{details[:120]}{RESET}")
        print()


def print_raw_events(events: list[dict], max_events: int = 15) -> None:
    """Print raw weighted events (fallback when LLM is unavailable)."""
    W = 64
    if not events:
        return
    print(f"{'=' * W}")
    print(f"  {BOLD}Events & Dates{RESET}  {DIM}(recency + scheduling){RESET}")
    print(f"{'=' * W}\n")
    for i, evt in enumerate(events[:max_events], 1):
        sched_tag = f" {CYAN}[SCHED]{RESET}" if evt["scheduling"] else ""
        pin_tag = f" {YELLOW}[PIN]{RESET}" if evt.get("pinned") else ""
        score_color = GREEN if evt["score"] >= 10 else YELLOW if evt["score"] >= 6 else DIM
        print(
            f"  {score_color}{evt['score']:2d}{RESET}{sched_tag}{pin_tag}  "
            f"@{evt['author']} {DIM}{evt['timestamp']}{RESET}"
        )
        dates_str = ", ".join(evt["dates"])
        times_str = ", ".join(evt["times"])
        refs = []
        if dates_str:
            refs.append(f"dates=[{dates_str}]")
        if times_str:
            refs.append(f"times=[{times_str}]")
        if refs:
            print(f"         {BOLD}{' '.join(refs)}{RESET}")
        content = evt["content"][:120]
        if content != evt["content"][:200]:
            content += "..."
        print(f"         {DIM}\"{content}\"{RESET}")
        print()


def print_recruiter_grades(grades: list, max_items: int = 30) -> None:
    """Print graded recruiter emails with color-coded scores."""
    W = 64
    print(f"{'=' * W}")
    print(f"  {BOLD}Recruiter Email Grades{RESET}  {DIM}(0-100){RESET}")
    print(f"{'=' * W}\n")

    for grade in grades[:max_items]:
        if grade.score >= 66:
            color, tag = GREEN, "[RESPOND]"
        elif grade.score >= 46:
            color, tag = YELLOW, "[REVIEW] "
        elif grade.score >= 21:
            color, tag = DIM, "[IGNORE] "
        else:
            color, tag = RED, "[TRASH]  "

        print(
            f"  {color}{grade.score:3d}{RESET} {color}{tag}{RESET}  "
            f"{BOLD}{grade.subject[:60]}{RESET}"
        )
        print(f"       From: {grade.sender[:50]}")
        for reason in grade.reasons[:3]:
            print(f"       {DIM}- {reason}{RESET}")
        print()
