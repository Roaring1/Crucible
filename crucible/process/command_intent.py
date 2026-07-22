"""Normalize console input into lifecycle intent without guessing from substrings."""

from __future__ import annotations


def lifecycle_intent(command: str) -> str | None:
    """Return a recognized lifecycle intent for an exact console command.

    Minecraft's dedicated-server console accepts commands with no slash, while
    people sometimes include the in-game-style leading slash. Only an exact
    ``stop`` command is lifecycle-changing: ``say stop``, ``stopsound``, and
    commands with arguments are deliberately not treated as a stop request.
    """
    normalized = command.strip()
    if normalized.startswith("/"):
        normalized = normalized[1:].strip()
    if normalized.casefold() == "stop":
        return "stop"
    return None
