"""
crucible/utils/term.py

ANSI color helpers for the CLI output.
Keep this the single source of truth — the Qt UI will ignore these entirely.
"""

import sys

# ── ANSI codes ─────────────────────────────────────────────────────────────────

BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
PURPLE = "\033[35m"
RESET  = "\033[0m"


# ── Formatted print helpers ────────────────────────────────────────────────────

def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET}  {msg}")

def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET}  {msg}", file=sys.stderr)

def err(msg: str) -> None:
    print(f"  {RED}✗{RESET}  {msg}", file=sys.stderr)

def info(msg: str) -> None:
    print(f"  {CYAN}·{RESET}  {msg}")

def dim(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def status_dot(status: str) -> str:
    """Return a colored status dot for terminal display."""
    return {
        "running":      f"{GREEN}●{RESET}",
        "stopped":      f"{DIM}●{RESET}",
        "tmux_missing": f"{YELLOW}●{RESET}",
    }.get(status, f"{YELLOW}?{RESET}")


def banner() -> str:
    return (
        f"\n"
        f"{BOLD}{CYAN}"
        f"  ╔═══════════════════════════════╗\n"
        f"  ║   C R U C I B L E             ║\n"
        f"  ║   GTNH Server Manager  v0.1   ║\n"
        f"  ╚═══════════════════════════════╝"
        f"{RESET}\n"
    )
