"""
crucible/ui/instance_panel.py

Right-hand panel shown when an instance is selected.
Header: name, version badge, Start/Stop/Restart/Attach buttons, status dot.
Body:   QTabWidget with Console, Mods, Notes, Info tabs.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QFrame,
    QMessageBox,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from ..process.tmux_manager import TmuxManager
from ..process.log_watcher import LogWatcher
from ..process.watchdog import Watchdog
from . import theme
from .tabs import ConsoleTab, ModsTab, NotesTab, InfoTab, ConfigTab, BackupTab, PlayersTab


class _TmuxWorker(QObject):
    """
    Runs a blocking TmuxManager call on a worker thread and emits
    finished(ok, message) back on the main thread.

    Usage:
        self._run_tmux(lambda: self._tmux.stop(inst, graceful=True), cb)
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

    def __init__(self, manager: InstanceManager, parent=None):
        super().__init__(parent)
        self._manager:  InstanceManager       = manager
        self._tmux:     TmuxManager           = TmuxManager()
        self._instance: ServerInstance | None = None
        self._watcher:  LogWatcher | None     = None
        self._w_thread: QThread | None        = None
        self._current_status: str             = "stopped"
        self._watchdog:        Watchdog | None = None
        self._wd_thread:       QThread | None  = None

        self._build_ui()
        self._show_empty()
        self._worker_threads: list[QThread] = []   # keep refs alive until done
        self._workers: list[_TmuxWorker]    = []   # CRITICAL: prevent GC before thread runs

    # ── Off-thread helper ─────────────────────────────────────────────────────

    def _run_tmux(self, fn, callback) -> None:
        """Run fn() (a blocking TmuxManager call) in a worker QThread,
        then call callback(ok: bool, msg: str) on the main thread."""
        thread = QThread()
        worker = _TmuxWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(callback)
        worker.finished.connect(thread.quit)
        # Clean up both thread and worker refs once done
        def _cleanup():
            if thread in self._worker_threads:
                self._worker_threads.remove(thread)
            if worker in self._workers:
                self._workers.remove(worker)
        thread.finished.connect(_cleanup)
        self._worker_threads.append(thread)
        self._workers.append(worker)   # MUST keep ref — PyQt6 won't
        thread.start()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header ──
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
        self._status_label.setMinimumWidth(140)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._status_label.setStyleSheet(
            f"color: {theme.SURFACE2}; font-size: 12px;"
        )
        h_layout.addWidget(self._status_label)

        # Buttons
        self._btn_start   = QPushButton("▶  Start")
        self._btn_stop    = QPushButton("■  Stop")
        self._btn_restart = QPushButton("↺  Restart")
        self._btn_attach  = QPushButton("⎋  Console")

        self._btn_start.setObjectName("PrimaryButton")
        self._btn_stop.setObjectName("DangerButton")

        for btn in (self._btn_start, self._btn_stop,
                    self._btn_restart, self._btn_attach):
            btn.setFixedHeight(32)
            h_layout.addWidget(btn)

        self._btn_start.clicked.connect(self._do_start)
        self._btn_stop.clicked.connect(self._do_stop)
        self._btn_restart.clicked.connect(self._do_restart)
        self._btn_attach.clicked.connect(self._do_attach)

        layout.addWidget(header)

        # ── Tabs ──
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)

        self._console = ConsoleTab()
        self._mods    = ModsTab()
        self._notes   = NotesTab(self._manager)
        self._info    = InfoTab()
        self._config  = ConfigTab()
        self._backup  = BackupTab()
        self._players = PlayersTab()

        self._tabs.addTab(self._console, "Console")
        self._tabs.addTab(self._mods,    "Mods")
        self._tabs.addTab(self._notes,   "Notes")
        self._tabs.addTab(self._info,    "Info")
        self._tabs.addTab(self._config,  "⚙  Config")
        self._tabs.addTab(self._backup,  "💾  Backups")
        self._tabs.addTab(self._players, "👥  Players")

        layout.addWidget(self._tabs, stretch=1)

        self._set_buttons_enabled(False)

    def _show_empty(self) -> None:
        self._name_label.setText("No server selected")
        self._ver_label.setText("")
        self._status_label.setText("")
        self._dot.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 18px;")
        self._set_buttons_enabled(False)

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, instance: ServerInstance) -> None:
        """Switch the panel to display the given instance."""
        # Flush notes before switching
        self._notes.flush()
        # Stop old log watcher
        self._stop_watcher()

        self._instance = instance
        self._name_label.setText(instance.name)
        self._ver_label.setText(instance.version)
        self._set_buttons_enabled(True)

        # Update status
        status = self._tmux.get_status(instance)
        self._update_status_display(status)

        # Start watchdog (once) and watch this instance if already running
        self._ensure_watchdog()
        if status == "running":
            self._watchdog.watch(instance, instance.auto_restart)

        # Load tabs
        self._mods.load(instance)
        self._notes.load(instance)
        self._info.load(instance, status)
        self._config.load(instance)
        self._backup.load(instance)
        self._players.load(instance)

        # Start log watcher
        self._start_watcher(instance)

    def update_status(self, status: str) -> None:
        """Called by the health-check timer in the main window."""
        self._update_status_display(status)
        if self._instance:
            self._info.load(self._instance, status)

    # ── Sidebar context-menu proxies ──────────────────────────────────────────

    def _do_start_for(self, instance: ServerInstance) -> None:
        """Start button action triggered from sidebar RMB — switch to instance first."""
        if self._instance is None or self._instance.id != instance.id:
            self.load(instance)
        self._do_start()

    def _do_stop_for(self, instance: ServerInstance) -> None:
        if self._instance is None or self._instance.id != instance.id:
            self.load(instance)
        self._do_stop()

    def _do_restart_for(self, instance: ServerInstance) -> None:
        if self._instance is None or self._instance.id != instance.id:
            self.load(instance)
        self._do_restart()

    # ── Watchdog lifecycle ────────────────────────────────────────────────────

    def _ensure_watchdog(self) -> None:
        """Start the watchdog thread once on first use."""
        if self._watchdog is not None:
            return
        self._wd_thread = QThread()
        self._watchdog  = Watchdog()
        self._watchdog.moveToThread(self._wd_thread)
        self._wd_thread.started.connect(self._watchdog.start)
        self._watchdog.crash_detected.connect(self._on_crash)
        self._watchdog.restarted.connect(self._on_auto_restarted)
        self._watchdog.restart_failed.connect(self._on_restart_failed)
        self._wd_thread.start()

    def _on_crash(self, instance_id: str) -> None:
        if self._instance and self._instance.id == instance_id:
            self._update_status_display("stopped")
            self.status_changed.emit(instance_id, "stopped")
            self._console._append_system(
                "⚠  Server session vanished unexpectedly — possible crash"
            )

    def _on_auto_restarted(self, instance_id: str) -> None:
        if self._instance and self._instance.id == instance_id:
            self._update_status_display("running")
            self.status_changed.emit(instance_id, "running")
            self._console._append_system("♻  Auto-restarted after crash")

    def _on_restart_failed(self, instance_id: str, reason: str) -> None:
        if self._instance and self._instance.id == instance_id:
            self._console._append_system(f"✗  Auto-restart failed: {reason}")

    # ── Log watcher lifecycle ─────────────────────────────────────────────────

    def _start_watcher(self, instance: ServerInstance) -> None:
        self._w_thread = QThread()
        self._watcher  = LogWatcher(instance)
        self._watcher.moveToThread(self._w_thread)
        self._w_thread.started.connect(self._watcher.start)
        self._w_thread.start()
        self._console.attach(instance, self._watcher)
        self._players.attach_watcher(self._watcher)

    def _stop_watcher(self) -> None:
        self._console.detach()
        self._players.detach_watcher()
        if self._watcher:
            self._watcher.stop()
            self._watcher = None
        if self._w_thread:
            self._w_thread.quit()
            self._w_thread.wait(2000)
            self._w_thread = None

    # ── Status display ────────────────────────────────────────────────────────

    def _update_status_display(self, status: str) -> None:
        self._current_status = status
        color = theme.STATUS_COLORS.get(status, theme.SURFACE2)
        self._dot.setStyleSheet(f"color: {color}; font-size: 18px;")

        # Use unambiguous labels so it's obvious at a glance
        label_text = {
            "running":      "● SERVER ONLINE",
            "stopped":      "○ SERVER OFFLINE",
            "tmux_missing": "⚠ TMUX MISSING",
        }.get(status, status.upper())
        self._status_label.setText(label_text)
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600;"
        )

        running = (status == "running")
        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._btn_restart.setEnabled(True)
        self._btn_attach.setEnabled(running)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in (self._btn_start, self._btn_stop,
                    self._btn_restart, self._btn_attach):
            btn.setEnabled(enabled)

    # ── Button actions ────────────────────────────────────────────────────────

    def _do_start(self) -> None:
        if not self._instance:
            return
        self._btn_start.setEnabled(False)
        self._btn_start.setText("Starting…")
        inst = self._instance

        def _on_done(ok: bool, msg: str) -> None:
            if ok:
                self._manager.update_instance(inst)
                self._update_status_display("running")
                self.status_changed.emit(inst.id, "running")
                if self._watchdog:
                    self._watchdog.watch(inst, inst.auto_restart)
            else:
                QMessageBox.critical(self, "Start Failed", msg)
                self._btn_start.setEnabled(True)
            self._btn_start.setText("▶  Start")

        self._run_tmux(lambda: self._tmux.start(inst), _on_done)

    def _do_stop(self) -> None:
        if not self._instance:
            return
        # Unwatch BEFORE stopping so the watchdog doesn't interpret
        # the clean session exit as a crash
        if self._watchdog:
            self._watchdog.unwatch(self._instance.id)
        self._btn_stop.setEnabled(False)
        self._btn_stop.setText("Stopping…")
        inst = self._instance

        def _on_done(ok: bool, msg: str) -> None:
            if ok:
                self._update_status_display("stopped")
                self.status_changed.emit(inst.id, "stopped")
                self._btn_stop.setText("■  Stop")
                self._btn_stop.setEnabled(False)
            else:
                reply = QMessageBox.question(
                    self,
                    "Stop Failed",
                    f"{msg}\n\nForce-kill? (no world save)",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    def _on_kill(ok2: bool, _msg2: str) -> None:
                        if ok2:
                            self._update_status_display("stopped")
                            self.status_changed.emit(inst.id, "stopped")
                        self._btn_stop.setText("■  Stop")
                        self._btn_stop.setEnabled(self._current_status == "running")
                    self._run_tmux(lambda: self._tmux.stop(inst, graceful=False), _on_kill)
                else:
                    self._btn_stop.setText("■  Stop")
                    self._btn_stop.setEnabled(self._current_status == "running")

        self._run_tmux(lambda: self._tmux.stop(inst, graceful=True, timeout_s=90), _on_done)

    def _do_restart(self) -> None:
        if not self._instance:
            return
        self._btn_restart.setEnabled(False)
        self._btn_restart.setText("Restarting…")
        inst = self._instance

        def _do_start_phase() -> None:
            def _on_start_done(ok: bool, msg: str) -> None:
                if ok:
                    self._manager.update_instance(inst)
                    self._update_status_display("running")
                    self.status_changed.emit(inst.id, "running")
                else:
                    QMessageBox.critical(self, "Start Failed", msg)
                self._btn_restart.setText("↺  Restart")
                self._btn_restart.setEnabled(True)
            self._run_tmux(lambda: self._tmux.start(inst), _on_start_done)

        if self._tmux.is_running(inst):
            # Unwatch so watchdog doesn't see this as a crash
            if self._watchdog:
                self._watchdog.unwatch(inst.id)

            def _on_stop_done(ok: bool, _msg: str) -> None:
                if not ok:
                    QMessageBox.warning(self, "Restart", "Server did not stop cleanly.")
                    self._btn_restart.setText("↺  Restart")
                    self._btn_restart.setEnabled(True)
                    return
                _do_start_phase()

            self._run_tmux(lambda: self._tmux.stop(inst, graceful=True, timeout_s=90), _on_stop_done)
        else:
            _do_start_phase()

    def _do_attach(self) -> None:
        if not self._instance:
            return
        ok, msg = self._tmux.attach(self._instance)
        if not ok:
            QMessageBox.warning(self, "Attach", msg)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._notes.flush()
        self._stop_watcher()
        super().closeEvent(event)
