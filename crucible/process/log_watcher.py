"""
crucible/process/log_watcher.py

Watches the active server log file for a running GTNH server and emits Qt signals
as new content appears.

Log file resolution (matches instance_model.get_log_path()):
  1. logs/fml-server-latest.log  — GTNH 1.7.10 primary log (FML era)
  2. logs/latest.log             — vanilla / 1.12+ Forge / fallback

Design:
  - Lives in its own QThread (never polls on the main thread)
  - Primary mechanism: 1-second QTimer poll (reliable on Wayland where
    QFileSystemWatcher can miss inotify events)
  - QFileSystemWatcher as an acceleration layer (fires immediately on write)
  - Handles the file not existing yet (server still starting)
  - Handles log rotation: file shrinks → reset position to 0
  - Watches logs/ directory so it detects whichever log file appears first
  - log_missing only emits ONCE per "not-found" run (no per-second spam)

GTNH 1.7.10 log format (fml-server-latest.log):
  2024-01-15 14:23:45 [INFO] [Minecraft-Server] <message>
  2024-01-15 14:23:45 [INFO] [ForgeModLoader] <message>

Vanilla / 1.12+ format (latest.log):
  [14:23:45] [Server thread/INFO]: <message>
"""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QTimer,
    pyqtSignal,
)

from ..data.instance_model import ServerInstance


# ── Regex patterns for log line parsing ───────────────────────────────────────

# "Done (67.412s)!" — server finished starting (handles integer seconds too)
_RE_DONE = re.compile(r"Done \(([\d.]+)s\)!")

# "Stopping the server"
_RE_STOPPING = re.compile(r"Stopping the server")

# Player joined:
#   GTNH 1.7.10:  "Roaring joined the game"
#   Also catches: "Roaring[/ip:port] logged in with entity id ..."
_RE_JOIN = re.compile(
    r"(\w+)(?:\[.*?\])? (?:joined the game|logged in with entity)"
)

# Player left:
#   "Roaring left the game"
#   "Roaring lost connection: ..."
#   "Roaring was kicked from the game: ..."
_RE_LEAVE = re.compile(
    r"(\w+) (?:left the game|lost connection|was kicked from)"
)

# /forge tps output: "Overall: Mean tick time: 50.123 ms; Mean TPS: 19.975"
# Also matches per-dimension lines
_RE_TPS = re.compile(r"Mean TPS:\s*([\d.]+)")


class LogWatcher(QObject):
    """
    Watches a GTNH server log file and emits signals as events occur.

    Intended to be moved to a QThread by the caller:

        self._thread = QThread()
        self._watcher = LogWatcher(instance)
        self._watcher.moveToThread(self._thread)
        self._thread.started.connect(self._watcher.start)
        self._thread.start()
    """

    # ── Signals ───────────────────────────────────────────────────────────────

    new_lines       = pyqtSignal(list)   # list[str] — raw log lines, newest last
    tps_update      = pyqtSignal(float)  # TPS value parsed from /forge tps output
    player_joined   = pyqtSignal(str)    # player name
    player_left     = pyqtSignal(str)    # player name
    server_started  = pyqtSignal(float)  # startup time in seconds
    server_stopping = pyqtSignal()
    log_missing     = pyqtSignal()       # emitted ONCE when log file goes missing

    def __init__(self, instance: ServerInstance, parent: QObject | None = None):
        super().__init__(parent)
        self._instance        = instance
        self._file_pos        = 0
        self._last_size       = 0
        self._log_was_missing = False   # track so we don't spam log_missing
        self._watcher         = QFileSystemWatcher()
        self._poll_timer: QTimer | None = None
        self._active          = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Call this after moveToThread() + thread.start()."""
        self._active = True

        # Acceleration layer
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

        # Primary layer: 1-second poll.
        # Created HERE in the worker thread — QTimer requires same-thread ownership.
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._on_file_changed)
        self._poll_timer.start(1000)

        self._attach_watchers()
        self._on_file_changed()

    def stop(self) -> None:
        self._active = False
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
            self._poll_timer = None
        self._watcher.deleteLater()

    def reset(self, instance: ServerInstance) -> None:
        """Switch to watching a different instance."""
        self._instance        = instance
        self._file_pos        = 0
        self._last_size       = 0
        self._log_was_missing = False
        self._attach_watchers()
        self._on_file_changed()

    # ── Watcher setup ─────────────────────────────────────────────────────────

    def _attach_watchers(self) -> None:
        old = self._watcher.files() + self._watcher.directories()
        if old:
            self._watcher.removePaths(old)

        log      = self._instance.get_log_path()
        logs_dir = Path(self._instance.path) / "logs"

        if log and log.exists():
            self._watcher.addPath(str(log))
        if logs_dir.exists():
            self._watcher.addPath(str(logs_dir))

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_dir_changed(self, _path: str) -> None:
        """New file appeared in logs/ — re-attach and read."""
        self._attach_watchers()
        self._on_file_changed()

    def _on_file_changed(self) -> None:
        if not self._active:
            return

        log = self._instance.get_log_path()
        if log is None:
            # Only emit log_missing once per "gone" period to avoid per-second spam
            if not self._log_was_missing:
                self._log_was_missing = True
                self.log_missing.emit()
            return

        # Log file (re-)appeared
        self._log_was_missing = False

        try:
            size = log.stat().st_size
        except OSError:
            if not self._log_was_missing:
                self._log_was_missing = True
                self.log_missing.emit()
            return

        # Detect log rotation (new server start rewrites the file)
        if size < self._last_size:
            self._file_pos = 0
        self._last_size = size

        if size == self._file_pos:
            return  # Nothing new

        try:
            with log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._file_pos)
                raw = fh.read()
                self._file_pos = fh.tell()
        except OSError:
            return

        if not raw:
            return

        lines = [ln for ln in raw.splitlines() if ln]
        if lines:
            self.new_lines.emit(lines)
            for line in lines:
                self._parse(line)

    # ── Line parsing ──────────────────────────────────────────────────────────

    def _parse(self, line: str) -> None:
        if m := _RE_DONE.search(line):
            self.server_started.emit(float(m.group(1)))
            return

        if _RE_STOPPING.search(line):
            self.server_stopping.emit()
            return

        if m := _RE_TPS.search(line):
            self.tps_update.emit(float(m.group(1)))
            return

        if m := _RE_JOIN.search(line):
            name = m.group(1)
            # Filter out common false positives from log prefixes
            if name and name not in ("INFO", "WARN", "ERROR", "DEBUG",
                                     "Server", "Forge", "FML", "NET"):
                self.player_joined.emit(name)
            return

        if m := _RE_LEAVE.search(line):
            name = m.group(1)
            if name and name not in ("INFO", "WARN", "ERROR", "DEBUG",
                                     "Server", "Forge", "FML", "NET"):
                self.player_left.emit(name)
            return
