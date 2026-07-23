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


from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from ..data.instance_model import ServerInstance
from .crash_recovery import HeartbeatStore
from .tmux_manager import TmuxManager

POLL_INTERVAL_MS   = 10_000   # 10 seconds
CRASH_LOOP_LIMIT   = 3        # give up after this many consecutive crashes
RESTART_DELAY_MS   = 30_000   # 30 s cool-down before each restart attempt
CRASH_CONFIRM_POLLS = 2        # ignore one transient tmux-query miss
STABLE_UPTIME_MS    = 10 * 60_000  # reset crash count after 10 healthy minutes


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
        self._tmux       = TmuxManager()
        self._heartbeats = HeartbeatStore()
        self._active = False

        # Per-instance state
        self._instances:   dict[str, ServerInstance] = {}
        self._watching:    dict[str, bool]           = {}
        self._auto_restart: dict[str, bool]          = {}
        self._crash_count: dict[str, int]            = {}
        self._miss_count: dict[str, int]             = {}
        self._java_miss_count: dict[str, int]        = {}
        self._watch_generation: dict[str, int]       = {}

        # Timer created in start() on the worker thread
        self._poll_timer: QTimer | None = None

    # Lifecycle

    @pyqtSlot()
    def start(self) -> None:
        """Called after moveToThread() + thread.start()."""
        self._active     = True
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_INTERVAL_MS)

    @pyqtSlot()
    def stop(self) -> None:
        self._active = False
        if self._poll_timer:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
            self._poll_timer = None

    # Registration

    @pyqtSlot(object, bool)
    def watch(self, instance: ServerInstance, auto_restart: bool = False) -> None:
        """
        Register an instance for crash monitoring.
        Call AFTER a successful Start.
        """
        iid = instance.id
        already_known = iid in self._instances
        self._instances[iid] = instance
        self._watching[iid] = True
        self._auto_restart[iid] = auto_restart
        self._miss_count[iid] = 0
        self._java_miss_count[iid] = 0
        # Record that this instance is now believed running under the CURRENT
        # boot session. If the whole host dies before unwatch()/_handle_crash()
        # ever run again, crash_recovery.reconcile() detects the stale
        # "running" heartbeat against a new boot id on the next launch.
        self._heartbeats.mark_running(iid, instance.tmux_session)
        # A manual stop removes the instance entirely, so a later manual start
        # resets the sequence. Re-watching after an automatic restart preserves
        # the count until the server has remained healthy for STABLE_UPTIME_MS.
        if not already_known:
            self._crash_count[iid] = 0
        generation = self._watch_generation.get(iid, 0) + 1
        self._watch_generation[iid] = generation
        QTimer.singleShot(
            STABLE_UPTIME_MS,
            lambda: self._mark_stable(iid, generation),
        )

    @pyqtSlot(str)
    def unwatch(self, instance_id: str) -> None:
        """
        Deregister an instance.
        Call BEFORE a graceful Stop — otherwise we'd mistake a clean
        shutdown for a crash.
        """
        self._heartbeats.mark_stopped_clean(instance_id)
        self._watching.pop(instance_id, None)
        self._instances.pop(instance_id, None)
        self._auto_restart.pop(instance_id, None)
        self._crash_count.pop(instance_id, None)
        self._miss_count.pop(instance_id, None)
        self._java_miss_count.pop(instance_id, None)
        self._watch_generation.pop(instance_id, None)

    # Poll

    def _poll(self) -> None:
        if not self._active or not self._watching:
            return

        for iid, instance in list(self._instances.items()):
            if not self._watching.get(iid):
                continue
            # Require repeated misses: one timed-out/failed tmux query must not
            # trigger a false crash and an unnecessary competing restart.
            running = self._tmux.probe_running(instance)
            if running is None:
                # Unknown is not offline. Preserve the previous evidence and
                # retry next poll instead of inventing a crash.
                continue
            if not running:
                misses = self._miss_count.get(iid, 0) + 1
                self._miss_count[iid] = misses
                if misses >= CRASH_CONFIRM_POLLS:
                    self._handle_crash(iid)
                continue
            self._miss_count[iid] = 0

            # The tmux *session* can stay alive forever even after java
            # crashes or exits cleanly: official start scripts such as
            # GTNH's startserver-java9.sh/.bat wrap java in an outer
            # "while true" reboot loop, by design, so the session survives
            # every crash. Watch the pane's foreground command too, so a
            # crash is still detected and reported even though has-session
            # never flips to False. If auto-restart is enabled, _do_restart's
            # own is_running() guard prevents launching a second, competing
            # java process while the wrapper's own countdown is still live.
            java_up = self._tmux.is_java_foreground(instance)
            if java_up is None:
                # Uncertain — do not invent a crash off an unrelated pane
                # query failure.
                continue
            if java_up:
                self._java_miss_count[iid] = 0
                continue
            java_misses = self._java_miss_count.get(iid, 0) + 1
            self._java_miss_count[iid] = java_misses
            if java_misses >= CRASH_CONFIRM_POLLS:
                self._handle_crash(iid)

    def _handle_crash(self, iid: str) -> None:
        self._watching[iid] = False   # stop watching until/unless restarted
        self._miss_count[iid] = 0
        self._java_miss_count[iid] = 0
        # Invalidate a pending stable-uptime callback for the crashed run.
        self._watch_generation[iid] = self._watch_generation.get(iid, 0) + 1
        count = self._crash_count.get(iid, 0) + 1
        self._crash_count[iid] = count

        # This crash was witnessed live -- mark it handled so a later
        # Crucible restart never re-reports it as an unseen host-level crash.
        self._heartbeats.mark_crashed_handled(iid)

        self.crash_detected.emit(iid)

        if not self._auto_restart.get(iid, False):
            return

        if count >= CRASH_LOOP_LIMIT:
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

    def _mark_stable(self, iid: str, generation: int) -> None:
        """Reset consecutive crashes only after one uninterrupted healthy run."""
        if (
            self._active
            and self._watching.get(iid, False)
            and self._watch_generation.get(iid) == generation
        ):
            self._crash_count[iid] = 0

    def _do_restart(self, iid: str) -> None:
        instance = self._instances.get(iid)
        if instance is None:
            return
        ok, msg = self._tmux.start(instance)
        if ok:
            self._watching[iid] = True   # resume monitoring
            self._miss_count[iid] = 0
            self._heartbeats.mark_running(iid, instance.tmux_session)
            self.restarted.emit(iid)
        else:
            self.restart_failed.emit(iid, msg)
