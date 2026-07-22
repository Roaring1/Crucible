"""
crucible/ui/instance_panel.py

Right-hand panel shown when an instance is selected.
Header: name, version badge, Start/Stop/Restart/Console buttons, status dot.
Body: QTabWidget with Console, Mods, Notes, Info, Config, Backups, Players.

Status state machine:
  stopped  -> starting  (tmux.start() succeeds)
  starting -> running   (log watcher sees "Done (Xs)!" line)
  running  -> stopping  (log watcher sees "Stopping the server")
  stopping -> stopped   (health check: tmux session gone)
  * -> stopped          (health check: tmux session gone at any time)
  stopped -> running    (health check: session found -- started externally)

The health check never overrides "starting" -> "running".
Only the log event does that.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, QMetaObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QMessageBox,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from ..process.tmux_manager import TmuxManager
from ..process.log_watcher import LogWatcher
from ..process.watchdog import Watchdog
from ..process.startup_patterns import RE_SERVER_DONE
from . import theme
from .tabs import ConsoleTab, ModsTab, NotesTab, InfoTab, ConfigTab, BackupTab, WorldTab, PlayersTab, SetupTab, SystemTab

# Fallback "Done (Xs)!" detector used only while "starting", read straight off
# the tmux pane. Shares the exact pattern with log_watcher's log-file parsing
# (via startup_patterns) so a startup line still promotes the status to
# "running" even if log-file parsing misses it (wrong log path for an unusual
# loader/modpack, permissions, rotation edge cases, etc.), and the two
# detection paths can never drift apart.
_RE_DONE_FALLBACK = RE_SERVER_DONE

# How often to auto-query TPS when server is running (ms)
_TPS_POLL_MS = 30_000
# While the server is in a transient "stopping"/"starting" state, poll the tmux
# session this often so the header flips to the resolved state quickly instead
# of waiting up to HEALTH_CHECK_INTERVAL_MS (5s) for the slow health check.
_TRANSITION_POLL_MS = 1_200


class _TmuxWorker(QObject):
    """
    Runs a blocking TmuxManager call on a worker thread and emits
    finished(ok, message) back on the main thread.
    """

    finished = pyqtSignal(bool, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            ok, msg = self._fn()
        except Exception as exc:
            ok, msg = False, str(exc)
        self.finished.emit(ok, msg)


class InstancePanel(QWidget):
    """
    Displays full details for one selected server instance.

    Signals
    ───────
    status_changed(instance_id, new_status)  — emitted after start/stop
    """

    status_changed = pyqtSignal(str, str)
    watchdog_watch_requested = pyqtSignal(object, bool)
    watchdog_unwatch_requested = pyqtSignal(str)

    def __init__(self, manager: InstanceManager, parent=None):
        super().__init__(parent)
        self._manager:  InstanceManager       = manager
        self._tmux:     TmuxManager           = TmuxManager()
        self._instance: ServerInstance | None = None
        self._watcher:  LogWatcher | None     = None
        self._w_thread: QThread | None        = None
        self._current_status: str             = "stopped"
        self._ip_request_generation: int       = 0
        self._manual_stop_generation: int       = 0
        self._watchdog:       Watchdog | None = None
        self._wd_thread:      QThread | None  = None
        # Expensive tabs are loaded on first view per instance, not all at once
        # during every sidebar selection.
        self._loaded_tabs: set[QWidget] = set()
        self._tps_poll_inflight = False

        self._build_ui()
        self._show_empty()
        self._worker_threads: list[QThread]    = []  # keep refs alive until done
        self._workers:        list[_TmuxWorker] = []  # CRITICAL: prevent GC before thread runs

        # Auto-TPS timer -- fires every 30s when server is running
        self._tps_timer = QTimer(self)
        self._tps_timer.setInterval(_TPS_POLL_MS)
        self._tps_timer.timeout.connect(self._auto_tps)

        # Fast transition poll -- only active while "stopping"/"starting".
        # Resolves the header to "offline"/"online" as soon as the tmux session
        # settles, so a stopping server doesn't sit on "STOPPING…" for seconds
        # after it has actually exited.
        self._transition_timer = QTimer(self)
        self._transition_timer.setInterval(_TRANSITION_POLL_MS)
        self._transition_timer.timeout.connect(self._poll_transition)
        self._transition_polling = False  # a check is currently in flight

    # Off-thread helper

    def _run_tmux(self, fn, callback) -> None:
        """Run fn() (a blocking TmuxManager call) in a worker QThread,
        then call callback(ok: bool, msg: str) on the main thread."""
        thread = QThread()
        worker = _TmuxWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(callback)
        worker.finished.connect(thread.quit)

        def _cleanup():
            if thread in self._worker_threads:
                self._worker_threads.remove(thread)
            if worker in self._workers:
                self._workers.remove(worker)

        thread.finished.connect(_cleanup)
        self._worker_threads.append(thread)
        self._workers.append(worker)   # MUST hold ref — PyQt6 won't
        thread.start()

    # UI construction

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QWidget()
        header.setStyleSheet(
            f"background-color: {theme.MANTLE}; "
            f"border-bottom: 1px solid {theme.SURFACE1};"
        )
        header.setFixedHeight(72)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(16, 8, 16, 8)
        h_layout.setSpacing(12)

        # Status dot
        self._dot = QLabel("●")
        self._dot.setFixedWidth(22)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dot.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 18px;")
        h_layout.addWidget(self._dot)

        # Name + version
        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        self._name_label = QLabel("—")
        self._name_label.setObjectName("HeaderName")
        self._ver_label  = QLabel("")
        self._ver_label.setObjectName("HeaderVersion")
        name_col.addWidget(self._name_label)
        name_col.addWidget(self._ver_label)
        h_layout.addLayout(name_col, stretch=1)

        # Status label
        self._status_label = QLabel("")
        self._status_label.setObjectName("StatusLabel")
        self._status_label.setMinimumWidth(160)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._status_label.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 12px;")
        h_layout.addWidget(self._status_label)

        # External IP copy button
        self._btn_ip = QPushButton("⧉  Copy IP")
        self._btn_ip.setFixedHeight(28)
        self._btn_ip.setFixedWidth(90)
        self._btn_ip.setToolTip("Copy external server IP to clipboard")
        self._btn_ip.setStyleSheet(
            f"QPushButton {{ background: {theme.SURFACE0}; color: {theme.SUBTEXT}; "
            f"border: 1px solid {theme.SURFACE1}; border-radius: 4px; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {theme.SURFACE1}; color: {theme.TEXT}; }}"
            f"QPushButton:pressed {{ background: {theme.SURFACE2}; }}"
        )
        self._btn_ip.clicked.connect(self._copy_external_ip)
        h_layout.addWidget(self._btn_ip)

        # Buttons -- Start/Stop are primary actions; Restart/Console are secondary.
        # Heights differ intentionally: primary pair is 32px, secondary 28px.
        self._btn_start   = QPushButton("▶  Start")
        self._btn_stop    = QPushButton("■  Stop")
        self._btn_restart = QPushButton("↺  Restart")
        self._btn_attach  = QPushButton("Console")

        self._btn_start.setObjectName("PrimaryButton")
        self._btn_stop.setObjectName("DangerButton")
        self._btn_restart.setObjectName("RestartButton")
        self._btn_attach.setObjectName("AttachButton")

        for btn in (self._btn_start, self._btn_stop):
            btn.setFixedHeight(32)
            h_layout.addWidget(btn)

        for btn in (self._btn_restart, self._btn_attach):
            btn.setFixedHeight(28)
            h_layout.addWidget(btn)

        self._btn_start.clicked.connect(self._do_start)
        self._btn_stop.clicked.connect(self._do_stop)
        self._btn_restart.clicked.connect(self._do_restart)
        self._btn_attach.clicked.connect(self._do_attach)

        layout.addWidget(header)

        # Tabs
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)

        self._setup   = SetupTab(self._manager)
        self._console = ConsoleTab()
        self._mods    = ModsTab()
        self._notes   = NotesTab(self._manager)
        self._info    = InfoTab()
        self._config  = ConfigTab()
        self._backup  = BackupTab()
        self._world   = WorldTab()
        self._players = PlayersTab()
        self._system  = SystemTab()

        # "Setup" is first so non-technical owners land on the easy checklist.
        self._tabs.addTab(self._setup,   "🧭  Setup")
        self._tabs.addTab(self._console, "Console")
        self._tabs.addTab(self._mods,    "Mods")
        self._tabs.addTab(self._notes,   "Notes")
        self._tabs.addTab(self._info,    "Info")
        self._tabs.addTab(self._config,  "⚙  Config")
        self._tabs.addTab(self._backup,  "💾  Backups")
        self._tabs.addTab(self._world,   "🌍  World")
        self._tabs.addTab(self._players, "👥  Players")
        self._tabs.addTab(self._system,  "📊  System")
        # World swaps must never proceed with unsaved server.properties edits
        # in flight -- reuse ConfigTab's own save/discard guard rather than
        # duplicating that logic.
        self._world.set_config_guard(self._config.confirm_discard_or_save)

        # Poll TPS only while the Console tab is focused (see _update_tps_polling).
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._console.lifecycle_command_sent.connect(
            self._on_console_lifecycle_command
        )

        layout.addWidget(self._tabs, stretch=1)

        self._set_buttons_enabled(False)

    def _copy_external_ip(self) -> None:
        """Fetch the public IP without letting a stale result target another server."""
        inst = self._instance
        if inst is None:
            return
        self._ip_request_generation += 1
        generation = self._ip_request_generation
        instance_id = inst.id
        try:
            port = inst.server_port()
        except Exception:
            port = "25565"
        self._btn_ip.setText("…")
        self._btn_ip.setEnabled(False)

        import threading
        def _worker():
            try:
                from ..data.netinfo import public_host
                ip = public_host(timeout=5) or ""
            except Exception:
                ip = ""
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(
                self, "_on_ip_fetched", Qt.ConnectionType.QueuedConnection,
                Q_ARG(int, generation), Q_ARG(str, instance_id),
                Q_ARG(str, ip), Q_ARG(str, port),
            )
        threading.Thread(target=_worker, daemon=True).start()

    @pyqtSlot(int, str, str, str)
    def _on_ip_fetched(self, generation: int, instance_id: str,
                       ip: str, port: str) -> None:
        if (
            generation != self._ip_request_generation
            or self._instance is None
            or self._instance.id != instance_id
        ):
            return
        self._btn_ip.setEnabled(True)
        if ip:
            addr = f"{ip}:{port}"
            try:
                QApplication.clipboard().setText(addr)
            except Exception:
                pass
            self._btn_ip.setText("✓  Copied!")
            self._btn_ip.setToolTip(f"Copied {addr} to clipboard")
        else:
            self._btn_ip.setText("✗  Failed")
            self._btn_ip.setToolTip(
                "Could not fetch your public IP (no internet?). "
                "Players on your LAN can still use your local IP."
            )
        QTimer.singleShot(
            2000, lambda: self._reset_ip_button(generation, instance_id)
        )

    def _reset_ip_button(self, generation: int, instance_id: str) -> None:
        if (
            generation == self._ip_request_generation
            and self._instance is not None
            and self._instance.id == instance_id
        ):
            self._btn_ip.setText("⧉  Copy IP")

    def _show_empty(self) -> None:
        self._name_label.setText("No server selected")
        self._ver_label.setText("")
        self._status_label.setText("")
        self._dot.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 18px;")
        self._set_buttons_enabled(False)

    # Public API

    def load(self, instance: ServerInstance, status_hint: str | None = None) -> bool:
        """Switch panels safely; return False when the old view must remain."""
        if self._instance is not None and self._instance.id == instance.id:
            return True
        if self._instance is not None and self.has_active_operations():
            QMessageBox.information(
                self, "Operation in progress",
                "Wait for the current start, stop, restart, backup, setup, or mod "
                "operation to finish before switching servers.",
            )
            return False
        if self._instance is not None and not self._config.confirm_discard_or_save():
            return False
        self._notes.flush()
        self._stop_watcher()
        self._tps_timer.stop()
        self._ip_request_generation += 1
        self._btn_ip.setEnabled(True)
        self._btn_ip.setText("⧉  Copy IP")

        self._instance = instance
        self._name_label.setText(instance.name)
        self._ver_label.setText(instance.version)
        self._set_buttons_enabled(True)

        # Sidebar health checks already know the current state. Reuse that
        # bounded background result instead of spawning a blocking tmux process
        # on every click. Direct action callers may omit the hint.
        status = status_hint if status_hint not in (None, "unknown") else self._tmux.get_status(instance)
        self._update_status_display(status)
        self._loaded_tabs.clear()

        # Start watchdog once
        self._ensure_watchdog()
        if status == "running":
            self.watchdog_watch_requested.emit(instance, instance.auto_restart)
            self._update_tps_polling()

        # Load only the visible tab. Mods, backups, player JSON, validation,
        # and process scans can be expensive on large servers and must not all
        # run synchronously merely because a sidebar row was clicked.
        self._tabs.setEnabled(True)
        self._load_current_tab()

        # Start log watcher
        self._start_watcher(instance)
        return True

    def current_instance_id(self) -> str | None:
        return self._instance.id if self._instance is not None else None

    def prepare_to_remove(self, instance: ServerInstance) -> bool:
        if self._instance is None or self._instance.id != instance.id:
            return True
        if self.has_active_operations():
            QMessageBox.information(
                self, "Operation in progress",
                "Wait for the current operation to finish before removing this server.",
            )
            return False
        if not self._config.confirm_discard_or_save():
            return False
        self._notes.flush()
        return True

    def clear(self) -> None:
        if self._instance is not None:
            self.watchdog_unwatch_requested.emit(self._instance.id)
        self._notes.flush()
        self._stop_watcher()
        self._tps_timer.stop()
        self._ip_request_generation += 1
        self._instance = None
        self._tabs.setEnabled(False)
        self._show_empty()

    def update_status(self, status: str) -> None:
        """
        Called by the 5-second health-check timer in the main window.

        Rules:
          • "starting" is never overridden by the health check — only the
            log-watcher's server_started signal promotes it to "running".
          • "stopping" is kept until the session disappears.
          • Any other transition (session vanished ��� stopped, external
            start detected → running) is applied immediately.
        """
        if self._current_status == "starting" and status == "running":
            # Session still alive but server not ready yet -- keep "starting"
            return
        if self._current_status == "stopping" and status == "running":
            # Session still alive, stop command issued -- keep "stopping"
            return

        if status == self._current_status:
            return   # nothing changed — skip filesystem scan + widget rebuild
        # If the health check now says running but we were stopped,
        # the server was probably started externally -- accept it.
        self._update_status_display(status)
        if self._instance and self._info in self._loaded_tabs:
            self._info.load(self._instance, status)

    # Sidebar context-menu proxies

    def _do_start_for(self, instance: ServerInstance) -> None:
        if self._instance is None or self._instance.id != instance.id:
            if not self.load(instance):
                return
        self._do_start()

    def _do_stop_for(self, instance: ServerInstance) -> None:
        if self._instance is None or self._instance.id != instance.id:
            if not self.load(instance):
                return
        self._do_stop()

    def _do_restart_for(self, instance: ServerInstance) -> None:
        if self._instance is None or self._instance.id != instance.id:
            if not self.load(instance):
                return
        self._do_restart()

    # Watchdog lifecycle

    def _ensure_watchdog(self) -> None:
        if self._watchdog is not None:
            return
        self._wd_thread = QThread()
        self._watchdog  = Watchdog()
        self._watchdog.moveToThread(self._wd_thread)
        self.watchdog_watch_requested.connect(self._watchdog.watch)
        self.watchdog_unwatch_requested.connect(self._watchdog.unwatch)
        self._wd_thread.started.connect(self._watchdog.start)
        self._watchdog.crash_detected.connect(self._on_crash)
        self._watchdog.restarted.connect(self._on_auto_restarted)
        self._watchdog.restart_failed.connect(self._on_restart_failed)
        self._wd_thread.start()

    def _on_crash(self, instance_id: str) -> None:
        if self._instance and self._instance.id == instance_id:
            self._update_status_display("stopped")
            self._tps_timer.stop()
            self.status_changed.emit(instance_id, "stopped")
            self._console._append_system(
                "⚠  Server session vanished unexpectedly — possible crash"
            )

    def _on_auto_restarted(self, instance_id: str) -> None:
        if self._instance and self._instance.id == instance_id:
            self._update_status_display("starting")
            self.status_changed.emit(instance_id, "running")
            self._console._append_system("♻  Auto-restarted after crash")

    def _on_restart_failed(self, instance_id: str, reason: str) -> None:
        if self._instance and self._instance.id == instance_id:
            self._console._append_system(f"✗  Auto-restart failed: {reason}")

    # Log watcher lifecycle

    def _start_watcher(self, instance: ServerInstance) -> None:
        self._w_thread = QThread()
        self._watcher  = LogWatcher(instance)
        self._watcher.moveToThread(self._w_thread)
        # Connect signals BEFORE starting the thread so the initial read
        # (fired immediately in start()) doesn't race with attach().
        self._console.attach(instance, self._watcher)
        self._players.attach_watcher(self._watcher)
        # Panel-level hooks (server state transitions)
        self._watcher.server_started.connect(self._on_log_server_started)
        self._watcher.server_stopping.connect(self._on_log_server_stopping)
        self._watcher.log_rotated.connect(self._on_log_rotated)
        self._w_thread.started.connect(self._watcher.start)
        self._w_thread.start()

    def _stop_watcher(self) -> None:
        self._console.detach()
        self._players.detach_watcher()
        if self._watcher:
            try:
                self._watcher.server_started.disconnect(self._on_log_server_started)
                self._watcher.server_stopping.disconnect(self._on_log_server_stopping)
                self._watcher.log_rotated.disconnect(self._on_log_rotated)
            except (RuntimeError, TypeError):
                pass
            if self._w_thread and self._w_thread.isRunning():
                QMetaObject.invokeMethod(
                    self._watcher, "stop", Qt.ConnectionType.BlockingQueuedConnection
                )
            self._watcher = None
        if self._w_thread:
            self._w_thread.quit()
            # A timed wait() can time out while the worker is still
            # finishing up (e.g. mid-poll). Never drop the last Python
            # reference to a QThread that is still running -- PyQt6
            # aborts the process with "QThread: Destroyed while thread
            # is still running" if we do. Fall back to an unbounded wait
            # so we only null the reference once it has truly stopped.
            if not self._w_thread.wait(2000):
                self._w_thread.wait()
            self._w_thread = None

    @pyqtSlot(str, str)
    def _on_console_lifecycle_command(self, instance_id: str, command: str) -> None:
        """Fold accepted console lifecycle commands into panel state.

        This is intent, not final truth: the log watcher and tmux health checks
        still decide when the process has actually stopped.
        """
        if self._instance is None or self._instance.id != instance_id:
            return
        if command.casefold() != "stop":
            return
        if self._watchdog:
            self.watchdog_unwatch_requested.emit(instance_id)
        if self._current_status in ("running", "starting"):
            self._update_status_display("stopping")
            self.status_changed.emit(instance_id, "stopping")
            self._tps_timer.stop()
            self._console._append_system(
                "Crucible recognized 'stop'; waiting for the tmux session to exit…"
            )
            self._manual_stop_generation += 1
            generation = self._manual_stop_generation
            QTimer.singleShot(
                120_000,
                lambda: self._check_manual_stop_timeout(instance_id, generation),
            )

    def _check_manual_stop_timeout(self, instance_id: str, generation: int) -> None:
        """Resume normal monitoring if a typed stop never actually stops."""
        if (
            generation != self._manual_stop_generation
            or self._instance is None
            or self._instance.id != instance_id
            or self._current_status != "stopping"
        ):
            return
        inst = self._instance

        def _done(running: bool | None, _msg: str) -> None:
            if (
                generation != self._manual_stop_generation
                or self._instance is not inst
                or self._current_status != "stopping"
            ):
                return
            if running is True:
                self._update_status_display("running")
                self.status_changed.emit(inst.id, "running")
                if self._watchdog:
                    self.watchdog_watch_requested.emit(inst, inst.auto_restart)
                self._console._append_system(
                    "The server did not stop after 120 seconds; monitoring resumed."
                )
            elif running is None:
                QTimer.singleShot(
                    30_000,
                    lambda: self._check_manual_stop_timeout(instance_id, generation),
                )

        self._run_tmux(lambda: (self._tmux.probe_running(inst), ""), _done)

    # Log-event handlers (called from main thread via queued signal)

    @pyqtSlot(float)
    def _on_log_server_started(self, secs: float) -> None:
        """Fired when 'Done (Xs)!' appears in the log — server is genuinely ready."""
        if self._instance:
            self._update_status_display("running")
            self.status_changed.emit(self._instance.id, "running")
            self._update_tps_polling()
            if self._watchdog:
                self.watchdog_watch_requested.emit(self._instance, self._instance.auto_restart)

    @pyqtSlot()
    def _on_log_server_stopping(self) -> None:
        """Fired when 'Stopping the server' appears — show stopping state."""
        if self._instance and self._current_status == "running":
            self._update_status_display("stopping")
            self.status_changed.emit(self._instance.id, "stopping")
            self._tps_timer.stop()

    @pyqtSlot()
    def _on_log_rotated(self) -> None:
        """Log file shrank — server restarted.  Break the stopping→running deadlock.

        The health-check guard blocks 'stopping'→'running' transitions (intentionally,
        so a graceful stop isn't overridden).  But after a restart the log file
        rotates before 'Done!' fires, so we must transition to 'starting' here to
        let the health check and server_started signal take over correctly.
        """
        if self._current_status in ("stopping", "stopped"):
            self._update_status_display("starting")
            if self._instance:
                self.status_changed.emit(self._instance.id, "running")

    # Auto-TPS

    def _console_is_focused(self) -> bool:
        """True when the Console tab is the visible/active tab in this panel."""
        return self.isVisible() and self._tabs.currentWidget() is self._console

    def _update_tps_polling(self) -> None:
        """Poll TPS only while the server runs AND the Console tab is focused.

        This keeps us from pointlessly sending /tick (or /tps) — and spamming the
        log — when the user isn't even looking at the console. The System tab is
        already focus-gated the same way via its show/hide events.
        """
        want = (
            self._current_status == "running"
            and self._instance is not None
            and self._instance.tps_command() is not None
            and self._console_is_focused()
        )
        if want:
            if not self._tps_timer.isActive():
                self._tps_timer.start()
                self._auto_tps()   # take an immediate first sample
        else:
            self._tps_timer.stop()

    def _load_current_tab(self) -> None:
        inst = self._instance
        tab = self._tabs.currentWidget()
        if inst is None or tab is None or tab in self._loaded_tabs:
            return
        status = self._current_status
        if tab is self._setup:
            self._setup.load(inst)
        elif tab is self._mods:
            self._mods.load(inst)
        elif tab is self._notes:
            self._notes.load(inst)
        elif tab is self._info:
            self._info.load(inst, status)
        elif tab is self._config:
            self._config.load(inst)
        elif tab is self._backup:
            self._backup.load(inst)
        elif tab is self._world:
            self._world.load(inst)
        elif tab is self._players:
            self._players.load(inst)
        elif tab is self._system:
            self._system.load(inst)
        # Console is attached by the watcher lifecycle rather than load().
        self._loaded_tabs.add(tab)

    def _on_tab_changed(self, _index: int) -> None:
        self._load_current_tab()
        self._update_tps_polling()

    def _auto_tps(self) -> None:
        """Periodically ask the server for TPS data, if its loader supports it.

        Vanilla/Fabric/Quilt have no TPS command, so we send nothing rather than
        spamming "Unknown or incomplete command" into their console.
        """
        if self._tps_poll_inflight:
            return
        inst = self._instance
        if inst and self._current_status == "running":
            cmd = inst.tps_command()
            if cmd:
                self._tps_poll_inflight = True

                def _done(_ok: bool, _msg: str) -> None:
                    self._tps_poll_inflight = False

                self._run_tmux(
                    lambda: (self._tmux.send_command(inst, cmd), ""), _done
                )

    # Status display

    def _update_status_display(self, status: str) -> None:
        self._current_status = status
        color = theme.STATUS_COLORS.get(status, theme.SURFACE2)
        self._dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        # Keep the console state label in sync with the panel -- single source of truth
        self._console.notify_status(status)

        label_text = {
            "running":      "● SERVER ONLINE",
            "stopped":      "○ SERVER OFFLINE",
            "starting":     "⚡ STARTING…",
            "stopping":     "◌ STOPPING…",
            "tmux_missing": "⚠ TMUX MISSING",
            "missing":      "⚠ SERVER FILES MISSING",
            "unmanaged":    "⚠ RUNNING OUTSIDE MANAGED TMUX",
            "unknown":      "? STATUS CHECK UNAVAILABLE",
        }.get(status, status.upper())

        self._status_label.setText(label_text)
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600;"
        )

        running  = (status == "running")
        starting = (status in ("starting", "stopping"))
        self._btn_start.setEnabled(status == "stopped")
        self._btn_stop.setEnabled(running or starting)
        self._btn_restart.setEnabled(status in ("running", "stopped"))
        self._btn_attach.setEnabled(running or starting)

        # Drive (or stop) the fast transition poll based on the new state.
        if status in ("starting", "stopping"):
            if not self._transition_timer.isActive():
                self._transition_timer.start()
        else:
            self._transition_timer.stop()
            self._transition_polling = False

    def _poll_transition(self) -> None:
        """Check the tmux session while in a transient state and resolve fast.

        Runs the (blocking) ``is_running`` check off the main thread. When the
        session has gone away we flip "stopping" (and a "starting" server that
        died during boot) straight to "stopped" without waiting for the slow
        5-second health check.
        """
        inst = self._instance
        if inst is None or self._current_status not in ("starting", "stopping"):
            self._transition_timer.stop()
            self._transition_polling = False
            return
        if self._transition_polling:
            return  # previous check still running -- don't stack them up
        self._transition_polling = True
        expecting = self._current_status

        def _done(is_running: bool | None, tail: str) -> None:
            self._transition_polling = False
            # Bail out if the user switched servers or the state already moved.
            if self._instance is not inst or self._current_status != expecting:
                return
            if is_running is None:
                return  # uncertain is not stopped; retry on the next timer tick
            if not is_running:
                # Session gone: a stopping server is now stopped; a starting
                # server that vanished before "Done" crashed/exited.
                self._update_status_display("stopped")
                self.status_changed.emit(inst.id, "stopped")
                if self._info in self._loaded_tabs:
                    self._info.load(inst, "stopped")
                return
            if expecting == "starting" and tail:
                # Fallback: read "Done (Xs)!" straight off the pane. Normally
                # the log watcher promotes "starting" -> "running" first, but
                # this covers cases where log-file parsing misses the line
                # (wrong log path for the loader/modpack, permissions, a log
                # rotated right at boot, etc.) so the UI never gets stuck.
                m = _RE_DONE_FALLBACK.search(tail)
                if m:
                    self._on_log_server_started(float(m.group(1)))

        def _check() -> tuple[bool | None, str]:
            running = self._tmux.probe_running(inst)
            tail = ""
            # Only pay for a pane capture while genuinely waiting on "Done":
            # this is the fallback path for when log-file parsing misses it.
            if running and expecting == "starting":
                try:
                    tail = self._tmux.capture_pane_tail(inst)
                except Exception:
                    tail = ""
            return running, tail

        self._run_tmux(_check, _done)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in (self._btn_start, self._btn_stop,
                    self._btn_restart, self._btn_attach):
            btn.setEnabled(enabled)

    # Button actions

    def _preflight_properties(self, inst) -> None:
        """Auto-repair crash-causing server.properties values before launch.

        Catches the classic blank/garbled numeric setting (e.g. server-port=)
        that makes Minecraft die with NumberFormatException before boot.
        """
        try:
            from ..data import properties as _props
            props_path = inst.path_obj / "server.properties"
            if not _props.has_blocking_errors(props_path):
                return
            res = _props.autorepair_file(props_path, only_errors=True)
            if res.changed:
                QMessageBox.information(
                    self, "Fixed server settings",
                    "Crucible auto-corrected settings that would have crashed the "
                    "server before it started:\n\n"
                    + "\n".join(f"  •  {k}: '{o}' → '{n}'" for k, o, n in res.changed)
                    + "\n\nA backup was saved as server.properties.bak.",
                )
        except Exception:
            pass

    def _do_start(self) -> None:
        if not self._instance:
            return
        self._btn_start.setEnabled(False)
        self._btn_start.setText("Starting…")
        inst = self._instance
        self._preflight_properties(inst)

        def _on_done(ok: bool, msg: str) -> None:
            if ok:
                self._manager.update_instance(inst)
                # Session created -- enter "starting" state.
                # Header will transition to "ONLINE" only when the log
                # watcher sees "Done (Xs)!" via _on_log_server_started.
                self._update_status_display("starting")
                self.status_changed.emit(inst.id, "running")
            else:
                QMessageBox.critical(self, "Start Failed", msg)
                self._btn_start.setEnabled(True)
            self._btn_start.setText("▶  Start")

        self._run_tmux(lambda: self._tmux.start(inst), _on_done)

    def _restore_after_failed_stop(self, inst: ServerInstance) -> None:
        """A failed/cancelled stop means the live server must be monitored again."""
        if self._instance is not inst:
            return
        self._update_status_display("running")
        self.status_changed.emit(inst.id, "running")
        if self._watchdog:
            self.watchdog_watch_requested.emit(inst, inst.auto_restart)

    def _do_stop(self) -> None:
        if not self._instance:
            return
        inst = self._instance
        if self._watchdog:
            self.watchdog_unwatch_requested.emit(inst.id)
        self._manual_stop_generation += 1
        self._update_status_display("stopping")
        self.status_changed.emit(inst.id, "stopping")
        self._tps_timer.stop()
        self._btn_stop.setEnabled(False)
        self._btn_stop.setText("Stopping…")

        def _on_done(ok: bool, msg: str) -> None:
            if ok:
                self._update_status_display("stopped")
                self.status_changed.emit(inst.id, "stopped")
                self._btn_stop.setText("■  Stop")
                self._btn_stop.setEnabled(False)
                return

            reply = QMessageBox.question(
                self,
                "Stop Failed",
                f"{msg}\n\nForce-kill? (no world save)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                def _on_kill(ok2: bool, msg2: str) -> None:
                    if ok2:
                        self._update_status_display("stopped")
                        self.status_changed.emit(inst.id, "stopped")
                    else:
                        self._restore_after_failed_stop(inst)
                        QMessageBox.critical(self, "Force-kill failed", msg2)
                    self._btn_stop.setText("■  Stop")
                    self._btn_stop.setEnabled(
                        self._current_status in ("running", "starting")
                    )

                self._run_tmux(
                    lambda: self._tmux.stop(inst, graceful=False), _on_kill
                )
            else:
                self._restore_after_failed_stop(inst)
                self._btn_stop.setText("■  Stop")
                self._btn_stop.setEnabled(
                    self._current_status in ("running", "starting")
                )

        self._run_tmux(
            lambda: self._tmux.stop(inst, graceful=True, timeout_s=90), _on_done
        )

    def _do_restart(self) -> None:
        if not self._instance:
            return
        self._btn_restart.setEnabled(False)
        self._btn_restart.setText("Restarting…")
        self._tps_timer.stop()
        inst = self._instance

        def _do_start_phase() -> None:
            def _on_start_done(ok: bool, msg: str) -> None:
                if ok:
                    self._manager.update_instance(inst)
                    self._update_status_display("starting")
                    self.status_changed.emit(inst.id, "running")
                else:
                    QMessageBox.critical(self, "Start Failed", msg)
                self._btn_restart.setText("↺  Restart")
                self._btn_restart.setEnabled(True)
            self._preflight_properties(inst)
            self._run_tmux(lambda: self._tmux.start(inst), _on_start_done)

        def _after_check(_ok: bool, state: str) -> None:
            if state == "unknown":
                QMessageBox.warning(
                    self, "Restart",
                    "Could not verify the tmux session. No stop or start command "
                    "was issued; retry shortly.",
                )
                self._btn_restart.setText("↺  Restart")
                self._btn_restart.setEnabled(True)
                return
            if state == "running":
                if self._watchdog:
                    self.watchdog_unwatch_requested.emit(inst.id)

                def _on_stop_done(stop_ok: bool, stop_msg: str) -> None:
                    if not stop_ok:
                        if self._watchdog:
                            self.watchdog_watch_requested.emit(
                                inst, inst.auto_restart
                            )
                        QMessageBox.warning(
                            self, "Restart", "Server did not stop cleanly:\n" + stop_msg
                        )
                        self._btn_restart.setText("↺  Restart")
                        self._btn_restart.setEnabled(True)
                        return
                    _do_start_phase()

                self._run_tmux(
                    lambda: self._tmux.stop(inst, graceful=True, timeout_s=90),
                    _on_stop_done,
                )
            else:
                _do_start_phase()

        def _probe_restart_state():
            running = self._tmux.probe_running(inst)
            state = (
                "unknown" if running is None
                else ("running" if running else "stopped")
            )
            return True, state

        self._run_tmux(_probe_restart_state, _after_check)

    def _do_attach(self) -> None:
        if not self._instance:
            return
        ok, msg = self._tmux.attach(self._instance)
        if not ok:
            QMessageBox.warning(self, "Attach", msg)

    # Cleanup

    def has_active_operations(self) -> bool:
        """True while closing could destroy a live worker or partial backup."""
        tmux_busy = any(t.isRunning() for t in self._worker_threads)
        backup_thread = getattr(self._backup, "_thread", None)
        backup_busy = bool(backup_thread and backup_thread.isRunning())
        world_busy = self._world.has_active_operation()
        setup_busy = self._setup.has_active_operation()
        mods_busy = self._mods.has_active_operation()
        return tmux_busy or backup_busy or world_busy or setup_busy or mods_busy

    def shutdown(self) -> None:
        """Stop worker-owned timers in their own threads, then join threads."""
        self._notes.flush()
        self._tps_timer.stop()
        self._transition_timer.stop()
        self._stop_watcher()
        if self._watchdog is not None and self._wd_thread is not None:
            if self._wd_thread.isRunning():
                QMetaObject.invokeMethod(
                    self._watchdog, "stop", Qt.ConnectionType.BlockingQueuedConnection
                )
            self._watchdog = None
            self._wd_thread.quit()
            # Same rationale as _stop_watcher(): a timed-out wait() does
            # NOT mean the thread stopped, so never null the reference on
            # a timeout alone -- fall back to an unbounded wait first.
            if not self._wd_thread.wait(2000):
                self._wd_thread.wait()
            self._wd_thread = None

    def closeEvent(self, event) -> None:
        if self.has_active_operations():
            event.ignore()
            return
        self.shutdown()
        super().closeEvent(event)
