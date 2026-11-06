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
from ..data.world_start_summary import inspect_instance_world_start
from .crash_recovery import (
    CRASHED_HANDLED,
    RUNNING,
    STOP_REQUESTED,
    STOPPED_CLEAN,
    HeartbeatStore,
)
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
        self._heartbeat_state: dict[str, str | None] = {}

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

    def _stop_polling_if_idle(self) -> None:
        """Do not keep a 10-second timer alive when no instance needs watching."""
        has_pending_external_stop = any(
            self._heartbeat_state.get(instance_id) == STOP_REQUESTED
            for instance_id in self._instances
        )
        if (
            self._poll_timer is not None
            and not any(self._watching.values())
            and not has_pending_external_stop
        ):
            self._poll_timer.stop()

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
        self._auto_restart[iid] = auto_restart
        self._miss_count[iid] = 0
        self._java_miss_count[iid] = 0
        heartbeat = self._heartbeats.read(iid)
        if heartbeat and heartbeat.get("state") == STOP_REQUESTED:
            # A failed intentional Stop can leave a wrapper countdown alive
            # after Java exits. Keep a low-cost pending watch until either
            # Java is confirmed back or the tmux session is confirmed gone.
            self._watching[iid] = False
            self._heartbeat_state[iid] = STOP_REQUESTED
            if self._poll_timer is not None and not self._poll_timer.isActive():
                self._poll_timer.start(POLL_INTERVAL_MS)
            return
        self._watching[iid] = True
        # Record that this instance is now believed running under the CURRENT
        # boot session. If the whole host dies before unwatch()/_handle_crash()
        # ever run again, crash_recovery.reconcile() detects the stale
        # "running" heartbeat against a new boot id on the next launch.
        self._heartbeats.mark_running(iid, instance.tmux_session)
        self._heartbeat_state[iid] = RUNNING
        if self._poll_timer is not None and not self._poll_timer.isActive():
            self._poll_timer.start(POLL_INTERVAL_MS)
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
        Call BEFORE a graceful Stop. It records stop intent before removing
        the watch; stop_completed() records the clean result after confirmation.
        """
        self._heartbeats.mark_stop_requested(instance_id)
        self._heartbeat_state[instance_id] = STOP_REQUESTED
        self._watching.pop(instance_id, None)
        self._instances.pop(instance_id, None)
        self._auto_restart.pop(instance_id, None)
        self._crash_count.pop(instance_id, None)
        self._miss_count.pop(instance_id, None)
        self._java_miss_count.pop(instance_id, None)
        self._watch_generation.pop(instance_id, None)
        self._stop_polling_if_idle()

    @pyqtSlot(str)
    def stop_completed(self, instance_id: str) -> None:
        """Record an intentional stop only after the server is confirmed gone."""
        self._heartbeats.mark_stopped_clean(instance_id)
        self._heartbeat_state.pop(instance_id, None)
        self._stop_polling_if_idle()

    # Poll

    def _poll(self) -> None:
        if not self._active or not self._watching:
            return

        for iid, instance in list(self._instances.items()):
            heartbeat = self._heartbeats.read(iid)
            state = heartbeat.get("state") if heartbeat else None
            previous_state = self._heartbeat_state.get(iid)
            self._heartbeat_state[iid] = state

            if state == STOP_REQUESTED:
                self._watching[iid] = False
                self._miss_count[iid] = 0
                self._java_miss_count[iid] = 0
                # Intentional Stop is not a crash. Keep crash auto-restart
                # disarmed, but continue a bounded tmux/JVM status check so a
                # wrapper countdown cannot become a false crash or a permanent
                # pending heartbeat.
                running = self._tmux.probe_running(instance)
                if running is None:
                    continue
                if not running:
                    self._heartbeats.mark_stopped_clean(iid)
                    self._heartbeat_state.pop(iid, None)
                    self._watching.pop(iid, None)
                    self._instances.pop(iid, None)
                    self._auto_restart.pop(iid, None)
                    self._crash_count.pop(iid, None)
                    self._miss_count.pop(iid, None)
                    self._java_miss_count.pop(iid, None)
                    self._watch_generation.pop(iid, None)
                    continue
                java_up = self._tmux.is_java_foreground(instance)
                if java_up is True:
                    # A failed Stop was positively cancelled and Minecraft
                    # is back. Restore the configured watchdog policy now.
                    self._heartbeats.mark_running(iid, instance.tmux_session)
                    self._heartbeat_state[iid] = RUNNING
                    self._watching[iid] = True
                    self._auto_restart[iid] = bool(instance.auto_restart)
                    self._miss_count[iid] = 0
                    self._java_miss_count[iid] = 0
                continue

            if state == STOPPED_CLEAN:
                self._watching.pop(iid, None)
                self._instances.pop(iid, None)
                self._auto_restart.pop(iid, None)
                self._crash_count.pop(iid, None)
                self._miss_count.pop(iid, None)
                self._java_miss_count.pop(iid, None)
                self._watch_generation.pop(iid, None)
                self._heartbeat_state.pop(iid, None)
                continue

            if state == RUNNING and previous_state == STOP_REQUESTED:
                # The external Stop failed and its caller confirmed that the
                # session is still running. Resume the watch that was paused
                # for that stop request. A clean stop followed by a new
                # external start is deliberately not auto-watched until the
                # normal GUI readiness signal calls watch().
                self._watching[iid] = True
                self._miss_count[iid] = 0
                self._java_miss_count[iid] = 0

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
                # A failed intentional Stop may temporarily register this
                # server with auto-restart disabled while a wrapper script
                # finishes its countdown. Restore the user's saved policy only
                # after Java itself is confirmed back in the foreground.
                self._auto_restart[iid] = bool(instance.auto_restart)
                continue
            java_misses = self._java_miss_count.get(iid, 0) + 1
            self._java_miss_count[iid] = java_misses
            if java_misses >= CRASH_CONFIRM_POLLS:
                self._handle_crash(iid)

        self._stop_polling_if_idle()

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
        self._heartbeat_state[iid] = CRASHED_HANDLED

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
        summary = inspect_instance_world_start(instance)
        if summary.state in {"unsafe", "unavailable"} or summary.requires_confirmation:
            reason = (
                "World selection needs owner review before automatic restart: "
                + (summary.error or summary.world_name)
            )
            self.restart_failed.emit(iid, reason)
            return
        ok, msg = self._tmux.start(instance)
        if ok:
            self._watching[iid] = True   # resume monitoring
            self._miss_count[iid] = 0
            self._heartbeats.mark_running(iid, instance.tmux_session)
            self._heartbeat_state[iid] = RUNNING
            if self._poll_timer is not None and not self._poll_timer.isActive():
                self._poll_timer.start(POLL_INTERVAL_MS)
            self.restarted.emit(iid)
        else:
            self.restart_failed.emit(iid, msg)
