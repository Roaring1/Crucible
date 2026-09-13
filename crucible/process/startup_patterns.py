"""
crucible/process/startup_patterns.py

Shared, Qt-free regex patterns used to detect server lifecycle events from
text (log lines or a raw tmux pane capture). Kept in a plain module (no
PyQt6 import) so:
  1. It can be unit-tested in headless/CI environments without PyQt6 installed.
  2. Two independent detection paths (LogWatcher's log-file tailing, and
     InstancePanel's tmux-pane-capture fallback) can share one pattern and
     never drift apart.
"""

from __future__ import annotations

import re

# "Done (67.412s)!" -- server finished starting (handles integer seconds too).
# Matches vanilla/Forge/FML/GTNH (RetroFuturaBootstrap) startup logging alike,
# since it's a substring search, not an anchored match.
RE_SERVER_DONE = re.compile(r"Done \(([\d.]+)s\)!")


def stale_starting_should_promote(
    elapsed_s: float, *, java_foreground: bool | None, threshold_s: float = 300.0
) -> bool:
    """True once a server has been stuck in "starting" for an unreasonably
    long time while java is confirmed to still be running.

    This is a last-resort escape hatch. Normally "starting" -> "running" is
    driven entirely by catching "Done (Xs)!" -- in the log file (LogWatcher),
    or as a fallback in the console pane's recent scrollback (RE_SERVER_DONE
    above). Both depend on catching that one line at the right moment; if a
    poll is ever missed (several rapid restarts rotating the log faster than
    a slow read can catch up, a startup burst blowing past the pane's small
    capture window before the next fallback poll, etc.) there is otherwise no
    way out of "starting" for as long as the same instance stays selected --
    the periodic health check is deliberately barred from overriding it, so a
    genuinely-still-booting server is never marked "running" early.

    No real modpack takes anywhere close to `threshold_s` just to boot, so
    sitting in "starting" that long while java is still confirmed alive is
    much stronger evidence that detection missed the line than that the
    server is still starting.
    """
    if java_foreground is not True:
        return False
    return elapsed_s >= threshold_s
