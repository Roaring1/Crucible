"""
crucible/utils/__init__.py
Shared CLI presentation helpers and subprocess utilities.
"""

from .term import (
    BOLD, DIM, GREEN, YELLOW, RED, CYAN, RESET,
    ok, warn, err, info, dim, banner,
    status_dot,
)

__all__ = [
    "BOLD", "DIM", "GREEN", "YELLOW", "RED", "CYAN", "RESET",
    "ok", "warn", "err", "info", "dim", "banner", "status_dot",
]
