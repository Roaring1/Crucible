"""
crucible/process/watchdog.py

Polls tmux every 10 seconds to detect unexpected server crashes.

"Unexpected" = tmux session disappeared while we were watching it
(i.e. the user did NOT press Stop — InstancePanel calls unwatch()
before a graceful stop so we know the difference).

Design:
  - Runs in its own QThread (moveToThread pattern, same as LogWatcher)
  - QTimer created in start() — NOT in __init__ — to avoid the
    cross-thread timer warning
  - One tmux list-sessions call per poll covers all watched instances
  - Crash-loop protection: stops auto-restarting after N consecutive crashes
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..data.instance_model import ServerInstance
from .tmux_manager import TmuxManager

POLL_INTERVAL_MS   = 10_000   # 10 seconds
CRASH_LOOP_LIMIT   = 3        # give up after this many consecutive crashes
RESTART_DELAY_MS   = 30_000   # 30 s cool-down before each restart attempt


class Watchdog(QObject):
    """
    Monitors registered ServerInstances for unexpected session loss.

    Signals
    -------
    crash_detected(instance_id)         emitted immediately when crash seen
    restarted(instance_id)              emitted after a successful auto-restart
    restart_failed(instance_id, reason) emitted when auto-restart fails / loop limit hit
    """

    crash_detected  = pyqtSignal(str)        # instance_id
    restarted       = pyqtSignal(str)        # instance_id
    restart_failed  = pyqtSignal(str, str)   # (instance_id, reason)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tmux   = TmuxManager()
        self._active = False

        # Per-instance state
        self._instances:   dict[str, ServerInstance] = {}
        self._watching:    dict[str, bool]           = {}
        self._auto_restart: dict[str, bool]          = {}
        self._crash_count: dict[str, int]            = {}

        # Timer created in start() on the worker thread
        self._poll_timer: QTimer | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Called after moveToThread() + thread.start()."""
        self._active     = True
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_INTERVAL_MS)

    def stop(self) -> None:
        self._active = False
        if self._poll_timer:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
            self._poll_timer = None

    # ── Registration ──────────────────────────────────────────────────────────

    def watch(self, instance: ServerInstance, auto_restart: bool = False) -> None:
        """
        Register an instance for crash monitoring.
        Call AFTER a successful Start.
        """
        self._instances[instance.id]    = instance
        self._watching[instance.id]     = True
        self._auto_restart[instance.id] = auto_restart
        self._crash_count[instance.id]  = 0

    def unwatch(self, instance_id: str) -> None:
        """
        Deregister an instance.
        Call BEFORE a graceful Stop — otherwise we'd mistake a clean
        shutdown for a crash.
        """
        self._watching.pop(instance_id, None)
        self._instances.pop(instance_id, None)
        self._auto_restart.pop(instance_id, None)
        self._crash_count.pop(instance_id, None)

    # ── Poll ──────────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        if not self._active or not self._watching:
            return

        for iid, instance in list(self._instances.items()):
            if not self._watching.get(iid):
                continue
            # Use is_running() (tmux has-session) rather than list_sessions()
            # so we match the session by exact name, independent of any prefix.
            if not self._tmux.is_running(instance):
                self._handle_crash(iid)

    def _handle_crash(self, iid: str) -> None:
        self._watching[iid] = False   # stop watching until/unless restarted
        count = self._crash_count.get(iid, 0) + 1
        self._crash_count[iid] = count

        self.crash_detected.emit(iid)

        if not self._auto_restart.get(iid, False):
            return

        if count > CRASH_LOOP_LIMIT:
            self.restart_failed.emit(
                iid,
                f"Crash loop — {count} consecutive crashes. Auto-restart disabled."
            )
            return

        # Schedule restart after cool-down
        QTimer.singleShot(
            RESTART_DELAY_MS,
            lambda: self._do_restart(iid),
        )

    def _do_restart(self, iid: str) -> None:
        instance = self._instances.get(iid)
        if instance is None:
            return
        ok, msg = self._tmux.start(instance)
        if ok:
            self._watching[iid] = True   # resume monitoring
            self.restarted.emit(iid)
        else:
            self.restart_failed.emit(iid, msg)
