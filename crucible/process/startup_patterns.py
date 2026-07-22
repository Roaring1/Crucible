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
