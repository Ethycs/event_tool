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
