"""
crucible/ui/main_window.py

Top-level application window.

Layout
──────
  QSplitter (horizontal)
    │
    ├── Sidebar (240px, resizable) ── server artwork, names, and text status
    │
    └── InstancePanel (stretches) ── header + tabbed content

Health check: QTimer fires every 5s, calls tmux.status_map() once,
pushes updates to sidebar and instance panel.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSettings,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QFontMetrics,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QSystemTrayIcon,
)

from ..data.instance_manager import InstanceManager, validate_delete_target
from ..data.instance_model import ServerInstance
from ..process.tmux_manager import TmuxManager
from . import theme
from .instance_panel import InstancePanel
from .sidebar import Sidebar

HEALTH_CHECK_INTERVAL_MS = 5_000


class _HealthWorker(QObject):
    finished = pyqtSignal(object)

    def __init__(self, tmux: TmuxManager):
        super().__init__()
        self._tmux = tmux

    @pyqtSlot(object)
    def run(self, instances) -> None:
        # Work from a snapshot supplied with each request. The worker and its
        # QThread are persistent for the full GUI lifetime; only this job is
        # repeated. This avoids destroying/replacing QThread wrappers from a
        # queued callback while Qt is inside a nested event loop (notably
        # QDrag.exec when a sidebar row is moved).
        self.finished.emit(self._tmux.status_map(list(instances)))


class _LoadingRepairWorker(QObject):
    """Run loading diagnosis and quarantine work away from the Qt GUI thread."""

    finished = pyqtSignal(str, str, object, str)

    def __init__(self, instance: ServerInstance, phase: str, tmux: TmuxManager):
        super().__init__()
        self._instance = instance
        self._phase = phase
        self._tmux = tmux

    @pyqtSlot()
    def run(self) -> None:
        try:
            status = self._tmux.get_status(self._instance)
            if status != "stopped":
                self.finished.emit(
                    self._instance.id, self._phase, {"status": status}, ""
                )
                return

            from ..diagnostics import loadcheck as lc

            result = lc.autofix_loading(
                self._instance.path, apply=self._phase == "apply"
            )
            self.finished.emit(self._instance.id, self._phase, result, "")
        except Exception as exc:  # noqa: BLE001 - report failures at the GUI boundary.
            self.finished.emit(self._instance.id, self._phase, None, str(exc))


class MainWindow(QMainWindow):
    """Crucible main window."""

    health_check_requested = pyqtSignal(object)

    def __init__(self, manager: InstanceManager):
        super().__init__()
        self._manager = manager
        self._tmux = TmuxManager()
        self._settings = QSettings("Crucible", "Crucible")
        self._keep_in_tray = self._settings.value("ui/keep_in_tray", False, type=bool)
        self._tray_controller = None
        self._tray_action = None
        self._quit_requested = False
        self._status_map: dict[str, str] = {}
        self._transient_status: dict[str, str] = {}
        self._reduce_motion = self._settings.value(
            "ui/reduce_motion", False, type=bool
        )
        theme.set_motion_enabled(not self._reduce_motion)
        self._identity_transition = None
        # One persistent health thread for the entire window lifetime.
        # Previously every 5-second poll created a QThread and a queued
        # finished callback nulled the last Python reference. During a sidebar
        # drag, QDrag.exec runs a nested Qt event loop; that callback could be
        # delivered in the middle of the drag and PyQt destroyed a QThread
        # whose native thread had not fully unwound, causing Qt's fatal abort.
        self._health_thread = QThread(self)
        self._health_thread.setObjectName("CrucibleHealthThread")
        self._health_worker = _HealthWorker(self._tmux)
        self._health_worker.moveToThread(self._health_thread)
        self.health_check_requested.connect(self._health_worker.run)
        self._health_worker.finished.connect(self._apply_health_status)
        self._health_busy = False
        self._health_thread.start()
        self._external_registry_warning_shown = False
        self._drop_validation_thread: QThread | None = None
        self._drop_validation_worker: ServerValidationWorker | None = None
        self._drop_validation_result: tuple[ServerInstance, list[str]] | None = None
        self._loading_repair_thread: QThread | None = None
        self._loading_repair_worker: _LoadingRepairWorker | None = None
        self._loading_repair_instance: ServerInstance | None = None
        self._loading_repair_pending_apply: ServerInstance | None = None

        self.setWindowTitle("Crucible — Minecraft Server Manager")
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)  # drag in Prism instances / packs / server dirs

        self._build_ui()
        self._build_view_menu()
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)
        self._populate_sidebar()
        self._start_health_timer()
        QTimer.singleShot(0, self._restore_tray_preference)

        # Auto-select first instance
        if manager.instances:
            self._sidebar.select_by_id(manager.instances[0].id)

        # Detect and report instances that went down together with the
        # whole host (power loss, hard freeze) since Crucible last ran.
        # Deferred one tick so the window is on screen before any dialog.
        QTimer.singleShot(0, self._run_crash_recovery)

    # UI construction

    def _build_ui(self) -> None:
        # Central widget: splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(True)
        self._splitter.setHandleWidth(5)
        self.setCentralWidget(self._splitter)

        # Left: sidebar
        self._sidebar = Sidebar()
        self._sidebar.instance_selected.connect(self._on_instance_selected)
        self._sidebar.add_requested.connect(self._on_add_requested)
        self._sidebar.remove_requested.connect(self._on_remove_requested)
        self._sidebar.rename_requested.connect(self._on_rename_requested)
        self._sidebar.appearance_requested.connect(self._on_appearance_requested)
        self._sidebar.shortcuts_requested.connect(self._on_shortcuts_requested)
        self._sidebar.inspect_logs_requested.connect(self._on_inspect_logs_requested)
        self._sidebar.fix_loading_requested.connect(self._on_fix_loading_requested)
        self._sidebar.order_changed.connect(self._on_order_changed)
        self._sidebar.paths_dropped.connect(self._on_paths_dropped)
        self._splitter.addWidget(self._sidebar)

        # Right: instance panel (must be created before wiring sidebar RMB signals)
        self._panel = InstancePanel(self._manager)
        self._panel.status_changed.connect(self._on_status_changed)
        self._panel.action_feedback_requested.connect(self._on_panel_action_feedback)
        self._panel.fix_loading_requested.connect(self._on_fix_loading_requested)
        self._panel.inspect_logs_requested.connect(self._on_inspect_logs_requested)
        self._splitter.addWidget(self._panel)

        # Wire the remaining server-rail lifecycle shortcuts now that the
        # selected-server panel exists. Diagnostics actions live in Diagnostics.
        self._sidebar.start_requested.connect(self._panel._do_start_for)
        self._sidebar.stop_requested.connect(self._panel._do_stop_for)

        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([240, 960])

        # The persistent instance/tmux footer has been removed. Genuine
        # action feedback appears briefly in the selected-server header.

    def _build_view_menu(self) -> None:
        menu = self.menuBar().addMenu("&View")
        menu.setAccessibleName("View options")
        self._reduce_motion_action = QAction("Reduce motion", self)
        self._reduce_motion_action.setCheckable(True)
        self._reduce_motion_action.setChecked(self._reduce_motion)
        self._reduce_motion_action.setStatusTip(
            "Use immediate server and workspace changes without animated transitions."
        )
        self._reduce_motion_action.setToolTip(
            "Disable selection and server-identity movement animations."
        )
        self._reduce_motion_action.toggled.connect(self._set_reduce_motion)
        menu.addAction(self._reduce_motion_action)

        settings_menu = self.menuBar().addMenu("&Settings")
        self._tray_action = QAction(
            "Keep Crucible running in the system tray when its window closes", self
        )
        self._tray_action.setCheckable(True)
        self._tray_action.setChecked(self._keep_in_tray)
        self._tray_action.setStatusTip(
            "Closing the window hides Crucible in the tray; Quit Crucible exits the manager without stopping Minecraft."
        )
        self._tray_action.toggled.connect(self._on_keep_in_tray_toggled)
        settings_menu.addAction(self._tray_action)

    def _restore_tray_preference(self) -> None:
        """Restore tray residency only when the active desktop provides a tray."""
        if self._keep_in_tray:
            if not self._enable_system_tray(initial=True):
                # Do not keep a checked preference that this desktop session
                # cannot honor. The user can re-enable it when a tray is
                # available again.
                self._keep_in_tray = False
                self._settings.setValue("ui/keep_in_tray", False)
                if self._tray_action is not None:
                    self._tray_action.blockSignals(True)
                    self._tray_action.setChecked(False)
                    self._tray_action.blockSignals(False)
                    self._tray_action.setEnabled(
                        QSystemTrayIcon.isSystemTrayAvailable()
                    )
        elif self._tray_action is not None:
            available = QSystemTrayIcon.isSystemTrayAvailable()
            self._tray_action.setEnabled(available)
            if not available:
                self._tray_action.setToolTip(
                    "This desktop session does not currently expose a system tray."
                )

    def _on_keep_in_tray_toggled(self, enabled: bool) -> None:
        if enabled:
            if not self._enable_system_tray(initial=False):
                if self._tray_action is not None:
                    self._tray_action.blockSignals(True)
                    self._tray_action.setChecked(False)
                    self._tray_action.blockSignals(False)
                self._keep_in_tray = False
                self._settings.setValue("ui/keep_in_tray", False)
                return
            self._keep_in_tray = True
            self._settings.setValue("ui/keep_in_tray", True)
            return

        self._keep_in_tray = False
        self._settings.setValue("ui/keep_in_tray", False)
        if self._tray_controller is not None:
            self._tray_controller.shutdown()
            self._tray_controller.deleteLater()
            self._tray_controller = None
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(True)

    def _enable_system_tray(self, *, initial: bool) -> bool:
        from .system_tray import SystemTrayController

        if self._tray_controller is not None:
            self._tray_controller.show()
            return True
        if not QSystemTrayIcon.isSystemTrayAvailable():
            if self._tray_action is not None:
                self._tray_action.setEnabled(False)
                self._tray_action.setToolTip(
                    "This desktop session does not currently expose a system tray."
                )
            if not initial:
                QMessageBox.information(
                    self,
                    "System tray unavailable",
                    "Crucible cannot find a system tray in this desktop session. "
                    "The window will close normally instead.",
                )
            return False
        try:
            self._tray_controller = SystemTrayController(
                self,
                instances=lambda: list(self._manager.instances),
                statuses=lambda: dict(self._status_map),
                open_ui=self._show_from_tray,
                open_server=self._open_server_from_tray,
                lifecycle=self._run_tray_lifecycle,
                quit_app=self._quit_from_tray,
                gui_pid=os.getpid(),
            )
        except Exception as exc:  # noqa: BLE001 - a broken optional tray must never prevent the main UI opening
            self._tray_controller = None
            if not initial:
                QMessageBox.warning(
                    self, "Could not create system tray icon", str(exc)
                )
            return False
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)
        if self._tray_action is not None:
            self._tray_action.setEnabled(True)
            self._tray_action.setToolTip(
                "Closing the window hides Crucible in the tray. Quit Crucible exits the manager; Minecraft servers keep running."
            )
        return True

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_server_from_tray(self, instance: ServerInstance) -> None:
        self._show_from_tray()
        if self._panel.current_instance_id() == instance.id:
            self._panel.focus_console()
            return
        self._sidebar.select_by_id(instance.id)
        # A same-row selection does not emit instance_selected, so explicitly
        # focus the console after Qt has applied the selection.
        QTimer.singleShot(0, self._panel.focus_console)

    def _tray_message(self, title: str, message: str, *, warning: bool = False) -> None:
        if self._tray_controller is not None:
            self._tray_controller.show_message(title, message, warning=warning)
        elif self.isVisible():
            QMessageBox.warning(self, title, message) if warning else QMessageBox.information(
                self, title, message
            )

    def _run_tray_lifecycle(self, instance: ServerInstance, action: str) -> None:
        """Dispatch only current, registered tray targets through the shared worker."""
        registered = next(
            (item for item in self._manager.instances if item.id == instance.id),
            None,
        )
        if (
            registered is None
            or str(registered.path) != str(instance.path)
            or str(registered.tmux_session) != str(instance.tmux_session)
        ):
            self._tray_message(
                "Server action unavailable",
                "This tray entry is stale because the registered server changed. "
                "Reopen the tray menu and try again.",
                warning=True,
            )
            return
        # Use the current registry object, never the potentially stale object
        # captured when Qt built the native menu.
        instance = registered

        expected = "stopped" if action == "start" else "running"
        current = self._status_map.get(instance.id, "unknown")
        if current != expected:
            self._tray_message(
                "Server action unavailable",
                f"{instance.name} is {current.replace('_', ' ')}. "
                f"{action.title()} requires {expected.title()} to be confirmed.",
                warning=True,
            )
            return
        if self._panel.has_active_operations():
            self._tray_message(
                "Operation in progress",
                f"Wait for the current Crucible operation to finish before changing {instance.name}.",
                warning=True,
            )
            return

        # Reuse the existing InstancePanel lifecycle worker. Start can require
        # the Forge startup question, so restore the main window for that
        # interaction. A graceful Stop on the already-selected server can
        # complete from the tray without opening the full UI.
        if action == "start":
            self._show_from_tray()
            if self._panel.current_instance_id() != instance.id:
                self._sidebar.select_by_id(instance.id)
            self._panel._do_start_for(instance, status_hint=current)
        elif action == "stop":
            self._panel._do_stop_for(
                instance,
                status_hint=current,
                graceful_only=True,
            )
        else:
            self._tray_message(
                "Server action unavailable",
                "That server action is not supported.",
                warning=True,
            )

    def _on_panel_action_feedback(
        self, title: str, message: str, warning: bool
    ) -> None:
        self._tray_message(title, message, warning=warning)

    def _quit_from_tray(self) -> None:
        if self._panel.has_active_operations():
            self._show_from_tray()
            QMessageBox.information(
                self,
                "Operation in progress",
                "Wait for the current Crucible operation to finish before quitting. "
                "Minecraft servers will remain independent of Crucible.",
            )
            return
        self._quit_requested = True
        closed = self.close()
        if closed:
            from PyQt6.QtWidgets import QApplication
            QApplication.quit()
        else:
            self._quit_requested = False

    def _on_shortcuts_requested(self, instance: ServerInstance) -> None:
        from .shortcuts_dialog import ShortcutsDialog

        saved_directory = str(
            self._settings.value("shortcuts/desktop_directory", "") or ""
        )
        dialog = ShortcutsDialog(
            instance,
            saved_desktop_directory=saved_directory,
            parent=self,
        )
        if not dialog.exec():
            return
        new_directory = dialog.created_desktop_directory
        if new_directory:
            if saved_directory and Path(saved_directory).expanduser() != Path(new_directory):
                try:
                    from ..desktop_shortcuts import ShortcutError, remove_shortcuts
                    remove_shortcuts(
                        instance.id,
                        desktop_directories=(saved_directory,),
                        include_applications=False,
                    )
                except (ShortcutError, OSError) as exc:
                    QMessageBox.warning(
                        self,
                        "Old desktop shortcuts could not be removed",
                        f"The new shortcuts were created, but the previous desktop location needs cleanup: {exc}",
                    )
            self._settings.setValue("shortcuts/desktop_directory", new_directory)

    def _set_reduce_motion(self, enabled: bool) -> None:
        self._reduce_motion = bool(enabled)
        self._settings.setValue("ui/reduce_motion", self._reduce_motion)
        theme.set_motion_enabled(not self._reduce_motion)
        self._sidebar.set_motion_enabled(not self._reduce_motion)
        if self._reduce_motion:
            self._cancel_identity_transition()

    def _cancel_identity_transition(self) -> None:
        transition = self._identity_transition
        self._identity_transition = None
        if transition is None:
            return
        overlay, animation, target, original_pixmap = transition
        animation.stop()
        current_instance = self._panel._instance
        if current_instance is not None and target is self._panel._identity_icon:
            # A newer selection may already have loaded while the previous
            # shared-element animation was in flight. Restore the current
            # server, not the image that was current when the old animation began.
            self._panel._set_identity_icon(current_instance)
        else:
            target.setPixmap(original_pixmap)
        overlay.deleteLater()

    def _animate_server_identity(self, instance_id: str) -> None:
        """Carry the selected server's artwork from its rail row into the workspace header."""
        self._cancel_identity_transition()
        if not theme.MOTION_ENABLED or self.isMinimized():
            return
        row = self._sidebar._row_widgets.get(instance_id)
        target = self._panel._identity_icon
        if row is None or not row.isVisible() or not target.isVisible():
            return
        source = row._icon
        pixmap = source.pixmap()
        if pixmap is None or pixmap.isNull():
            return

        start_pos = source.mapTo(self, QPoint(0, 0))
        end_pos = target.mapTo(self, QPoint(0, 0))
        start_rect = QRect(start_pos, source.size())
        end_rect = QRect(end_pos, target.size())
        original_pixmap = target.pixmap()
        if original_pixmap is None or original_pixmap.isNull():
            return

        overlay = QLabel(self)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        overlay.setStyleSheet("background: transparent; border: none;")
        overlay.setPixmap(pixmap)
        overlay.setScaledContents(True)
        overlay.setGeometry(start_rect)
        target.setPixmap(QPixmap())  # preserve its fixed geometry while the image travels
        overlay.show()
        overlay.raise_()

        animation = QPropertyAnimation(overlay, b"geometry", self)
        animation.setDuration(205)
        animation.setStartValue(start_rect)
        animation.setEndValue(end_rect)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._identity_transition = (
            overlay, animation, target, original_pixmap
        )

        def finish() -> None:
            current = self._identity_transition
            if current is None or current[1] is not animation:
                return
            self._identity_transition = None
            target.setPixmap(original_pixmap)
            overlay.deleteLater()

        animation.finished.connect(finish)
        animation.start()

    # Population

    def _populate_sidebar(self) -> None:
        # The first status is not guessed: a background tmux check starts as
        # soon as the UI is built. Until it returns, every registered server
        # reads "Checking" rather than a misleading provisional Stopped.
        initial = "checking" if self._tmux.tmux_available() else "tmux_missing"
        status_map = {inst.id: initial for inst in self._manager.instances}
        self._status_map = dict(status_map)
        self._sidebar.populate(self._manager.instances, status_map)
        self._update_status_bar()

    # Crash recovery (whole-host crashes, e.g. power loss, that no live
    # Watchdog could have witnessed because Crucible itself was not running)

    def _run_crash_recovery(self) -> None:
        from ..process.crash_recovery import reconcile as reconcile_crash_recovery

        if not self._tmux.tmux_available():
            return
        try:
            reports = reconcile_crash_recovery(self._manager.instances, self._tmux)
        except Exception as exc:
            print(f"[crucible] crash recovery check failed: {exc}")
            return
        if not reports:
            return

        # Startup discovery is read-only: opening Crucible never starts a
        # Minecraft server, including after a host reboot. A crash report is
        # surfaced to the owner; they choose when to press Start.
        not_restarted = [report.instance_name for report in reports]

        lines = [
            (
                "Crucible found that one or more servers were still marked running "
                "the last time it saw them, but the system has since rebooted "
                "without a normal stop ever being recorded. This points to the "
                "whole PC losing power or freezing, not a Crucible or server bug."
            ),
            "",
        ]
        for report in reports:
            lines.append(f"• {report.message}")
            if report.log_evidence:
                lines.append(f"  {report.log_evidence}")
        if not_restarted:
            lines.append("")
            lines.append(
                "No Minecraft servers were started during startup recovery. "
                "Review their state, then start each server when you are ready: "
                + ", ".join(not_restarted)
            )

        QMessageBox.warning(self, "Recovered from an unexpected shutdown", "\n".join(lines))
        self._populate_sidebar()
        # Do NOT call self._health_check() here. Forcing a second health-check
        # thread churn moments after the one _start_health_timer() already
        # kicked off can race with that first QThread's teardown and hit Qt's
        # fatal "QThread: Destroyed while thread is still running" abort. The
        # existing 5s self._health_timer will pick up the new state shortly
        # on its own -- no need to force it.

    # Health check timer

    def _start_health_timer(self) -> None:
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(HEALTH_CHECK_INTERVAL_MS)
        self._health_timer.timeout.connect(self._health_check)
        self._health_timer.start()
        self._health_check()

    def _health_check(self) -> None:
        # The CLI and GUI share this registry. Its intentional last_started-only
        # lifecycle writes are safe to reconcile in place; real config changes,
        # additions/removals, malformed files and races still require a restart.
        registry_changed = self._manager.registry_changed_externally()
        if (
            registry_changed
            and not self._manager.reconcile_external_last_started()
        ):
            if not self._external_registry_warning_shown:
                self._external_registry_warning_shown = True
                QMessageBox.warning(
                    self,
                    "Server registry changed outside Crucible",
                    "~/.config/crucible/instances.json was edited, replaced, or "
                    "deleted by another program. Crucible will keep its current "
                    "in-memory list for safety. Finish active work, close Crucible, "
                    "and reopen it to load the external change. No server files "
                    "have been deleted by this warning.",
                )
        else:
            self._external_registry_warning_shown = False
        if self._health_busy:
            return
        self._health_busy = True
        self.health_check_requested.emit(list(self._manager.instances))

    @pyqtSlot(object)
    def _apply_health_status(self, status_map) -> None:
        self._health_busy = False
        self._status_map = dict(status_map)
        # Lifecycle workers may outlive a single status sample, particularly
        # the non-selected server Stop dispatched from the tray. Preserve
        # their explicit Starting/Stopping intent until the worker completes.
        self._status_map.update(self._transient_status)
        # Preserve lifecycle intent from the selected panel while a server
        # start/stop worker is still resolving. The session may appear before
        # Minecraft has reached its ready state.
        selected = self._panel.current_instance_id()
        if selected and self._panel._current_status in {"starting", "stopping"}:
            self._status_map[selected] = self._panel._current_status
        # The shared lifecycle worker owns these transient states until it
        # reports completion; periodic polling must not re-enable actions early.
        self._sidebar.update_all_statuses(self._status_map)
        selected = self._sidebar.selected_instance()
        if (
            selected
            and self._panel.current_instance_id() == selected.id
            and self._panel._current_status in {"starting", "stopping"}
        ):
            self._status_map[selected.id] = self._panel._current_status
            self._sidebar.update_status(selected.id, self._panel._current_status)
        if selected and selected.id in self._status_map:
            self._panel.update_status(self._status_map[selected.id])
        if self._tray_controller is not None:
            self._tray_controller.refresh_server_menu()
        self._update_status_bar()

    # Status bar

    def _update_status_bar(self) -> None:
        """Compatibility no-op: the old permanent instance/tmux footer is gone."""
        return

    def _on_inspect_logs_requested(self, instance: ServerInstance) -> None:
        """Open the existing read-only log/crash inspection view from Diagnostics."""
        self._show_loading_details(
            instance, "Review the latest available server log and crash details. No files will be changed."
        )

    # Event handlers

    def _on_instance_selected(self, instance: ServerInstance) -> None:
        previous_id = self._panel.current_instance_id()
        status_hint = self._sidebar.status_for(instance.id)
        if not self._panel.load(instance, status_hint=status_hint):
            if previous_id:
                QTimer.singleShot(0, lambda: self._sidebar.select_by_id(previous_id))
            return
        if previous_id is not None and previous_id != instance.id:
            self._animate_server_identity(instance.id)

    def _on_appearance_requested(self, instance: ServerInstance) -> None:
        """Change only the selected server's Crucible rail image."""
        from ..data import server_icons

        box = QMessageBox(self)
        box.setWindowTitle("Server appearance")
        box.setAccessibleName("Change selected server appearance")
        box.setText(f"Choose the image shown beside {instance.name}.")
        box.setInformativeText(
            "This is stored in Crucible's private application data. It does not "
            "modify the server folder, modpack, or files shared with players."
        )
        choose = box.addButton(
            "Choose custom image…", QMessageBox.ButtonRole.AcceptRole
        )
        choose.setAccessibleName("Choose a custom image for the selected server")
        reset = box.addButton(
            "Use pack image / built-in icon", QMessageBox.ButtonRole.ActionRole
        )
        reset.setAccessibleName(
            "Use the detected server pack image or a built-in icon"
        )
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        cancel.setAccessibleName("Cancel server appearance change")

        # QMessageBox can underestimate the width of custom button rows when
        # a global stylesheet is active. Its computed width used to be 352px
        # while its layout hint was 524px, shrinking the two long actions to
        # 106px each. Size each button from its rendered text before showing.
        appearance_buttons = box.buttons()
        for button in appearance_buttons:
            metrics = QFontMetrics(button.font())
            button.setMinimumWidth(metrics.horizontalAdvance(button.text()) + 40)
            button.setMinimumHeight(max(36, metrics.height() + 14))
        required_row_width = (
            sum(button.minimumWidth() for button in appearance_buttons)
            + max(0, len(appearance_buttons) - 1) * 6
            + 56
        )
        box.setMinimumWidth(max(box.sizeHint().width(), required_row_width))
        box.setMinimumHeight(max(box.sizeHint().height(), 180))
        box.adjustSize()
        box.setDefaultButton(choose)
        box.exec()
        picked = box.clickedButton()

        if picked is cancel or picked is None:
            return
        try:
            if picked is choose:
                filename, _ = QFileDialog.getOpenFileName(
                    self,
                    "Choose server image",
                    str(Path.home()),
                    "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
                )
                if not filename:
                    return
                server_icons.set_custom_icon(instance.id, filename)
                feedback = "Custom server image saved"
            elif picked is reset:
                server_icons.clear_custom_icon(instance.id)
                feedback = "Using server pack image or built-in icon"
            else:
                return
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Server appearance", str(exc))
            return

        self._sidebar.refresh_instance_icon(instance.id)
        if self._tray_controller is not None:
            self._tray_controller.refresh_server_menu(force=True)
        if self._panel.current_instance_id() == instance.id:
            self._panel.show_feedback(feedback)

    def _on_rename_requested(self, instance) -> None:
        from dataclasses import replace
        name, ok = QInputDialog.getText(self, "Rename server", "Server name:", text=instance.name)
        if not ok:
            return
        name = name.strip()
        if not name or any(ord(c) < 32 for c in name):
            QMessageBox.warning(self, "Rename server", "Use a non-empty name without control characters.")
            return
        updated = replace(instance, name=name)
        try:
            self._manager.update_instance(updated)
        except Exception as exc:
            QMessageBox.warning(self, "Could not rename server", str(exc))
            return
        # Refresh the existing row/header without reloading tabs or discarding edits.
        instance.name = updated.name
        item = self._sidebar._items.get(updated.id)
        if item is not None:
            item.instance.name = updated.name
            item._refresh()
        if self._panel.current_instance_id() == updated.id:
            self._panel._instance.name = updated.name
            self._panel._name_label.setText(updated.name)
        if self._panel.current_instance_id() == updated.id:
            self._panel.show_feedback(f'Renamed to “{name}” · files and session unchanged')

    def _show_registered_instance(self, instance: ServerInstance) -> bool:
        """Show a newly registered server without probing tmux on the GUI thread."""
        already_listed = instance.id in self._sidebar._items
        if already_listed:
            if instance.id in self._status_map:
                self._sidebar.update_status(
                    instance.id, self._status_map[instance.id]
                )
        else:
            self._sidebar.add_instance(instance, "checking")
        self._sidebar.select_by_id(instance.id)
        # A registration result may refer to a live imported server. Let the
        # existing health worker establish its real state instead of performing
        # a synchronous tmux/procfs scan after the dialog closes.
        self._health_check()
        return already_listed

    def _on_add_requested(self) -> None:
        """Offer the three common server setup routes without a stacked chooser dialog."""
        menu = QMenu(self)
        create_action = menu.addAction("Create a server…")
        create_action.setStatusTip(
            "Install a clean Minecraft server with Vanilla, Fabric, Forge, NeoForge or Quilt."
        )
        pack_action = menu.addAction("Install a modpack…")
        pack_action.setStatusTip(
            "Browse Modrinth and install a modpack as a server."
        )
        import_action = menu.addAction("Import existing files…")
        import_action.setStatusTip(
            "Import a Prism instance or ZIP/.mrpack, or register a server folder already on disk."
        )
        anchor = self._sidebar._add_btn
        chosen = menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))
        if chosen is None:
            return

        if chosen is create_action:
            from .new_server_dialog import NewServerDialog
            dlg = NewServerDialog(self._manager, self)
        elif chosen is pack_action:
            from .modpack_dialog import ModpackDialog
            dlg = ModpackDialog(self._manager, self)
        else:
            from .add_dialog import AddInstanceDialog

            dlg = AddInstanceDialog(self._manager, self)

        if dlg.exec() and dlg.result_instance is not None:
            inst = dlg.result_instance
            already_listed = self._show_registered_instance(inst)
            message = (
                f'Selected existing server "{inst.name}"'
                if already_listed
                else f'Added "{inst.name}" to the server list'
            )
            self._panel.show_feedback(message)
            self._update_status_bar()
    def _on_order_changed(self, order: list) -> None:
        """Persist the new sidebar order after an internal drag-reorder."""
        try:
            self._manager.reorder([str(i) for i in order])
            self._manager.save()
        except Exception:  # noqa: BLE001 - reordering must never crash the UI
            pass

    def _on_paths_dropped(self, paths: list) -> None:
        """Import Prism/.mrpack/.zip/server folders dropped onto the sidebar."""
        for p in paths:
            if p:
                self._import_dropped_path(Path(p))

    def _show_loading_details(self, instance, summary):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtWidgets import (
            QDialog,
            QHBoxLayout,
            QPlainTextEdit,
            QPushButton,
            QVBoxLayout,
        )

        from ..diagnostics.loadcheck import latest_crash_text
        text, source = latest_crash_text(instance.path)
        dlg = QDialog(self)
        dlg.setWindowTitle("Loading diagnosis · " + instance.name)
        dlg.resize(820, 560)
        layout = QVBoxLayout(dlg)
        heading = QLabel(summary)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText("Source: " + (source or "No log found") + "\n\n" + (text[-24000:] if text else "Start the server once to capture a log, then check again."))
        layout.addWidget(details, 1)
        row = QHBoxLayout()
        for label, path in (("Open log", source), ("Open server folder", instance.path)):
            button = QPushButton(label)
            button.setEnabled(bool(path))
            button.clicked.connect(lambda _, p=path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            row.addWidget(button)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        row.addWidget(close)
        layout.addLayout(row)
        dlg.exec()

    def _on_fix_loading_requested(self, instance: ServerInstance) -> None:
        """Start a background, read-only diagnosis of a server loading crash."""
        if self._loading_repair_thread is not None:
            self.statusBar().showMessage(
                "A loading diagnosis or repair is already in progress."
            )
            return
        self._start_loading_repair(instance, "diagnose")

    def _start_loading_repair(self, instance: ServerInstance, phase: str) -> None:
        thread = QThread(self)
        thread.setObjectName("CrucibleLoadingRepairThread")
        worker = _LoadingRepairWorker(instance, phase, self._tmux)
        worker.moveToThread(thread)
        self._loading_repair_thread = thread
        self._loading_repair_worker = worker
        self._loading_repair_instance = instance
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_loading_repair_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        from .thread_lifecycle import connect_thread_cleanup

        def clear_worker() -> None:
            if self._loading_repair_thread is not thread:
                return
            self._loading_repair_thread = None
            self._loading_repair_worker = None
            self._loading_repair_instance = None
            self._start_pending_loading_repair_if_idle()

        connect_thread_cleanup(thread, clear_worker)
        thread.start()
        self.statusBar().showMessage(
            "Checking server logs and mod archives in the background..."
        )

    def _start_pending_loading_repair_if_idle(self) -> None:
        """Start a confirmed apply phase after any previous QThread has stopped."""
        if self._loading_repair_thread is not None:
            return
        pending = self._loading_repair_pending_apply
        if pending is None:
            return
        self._loading_repair_pending_apply = None
        current = next(
            (item for item in self._manager.instances if item.id == pending.id),
            None,
        )
        if current is not None and current.path == pending.path:
            self._start_loading_repair(current, "apply")

    def _on_loading_repair_finished(
        self, instance_id: str, phase: str, result, error: str
    ) -> None:
        """Handle a completed worker result without trusting stale instance state."""
        instance = next(
            (item for item in self._manager.instances if item.id == instance_id),
            None,
        )
        worker_instance = self._loading_repair_instance
        if (
            instance is None
            or worker_instance is None
            or instance.path != worker_instance.path
        ):
            self.statusBar().showMessage(
                "Loading repair cancelled because the server entry changed."
            )
            return

        if error:
            self._show_loading_details(
                instance, "Loading diagnosis or repair failed: " + error
            )
            return
        if isinstance(result, dict) and "status" in result:
            if phase == "diagnose":
                QMessageBox.warning(
                    self,
                    "Stop the server first",
                    "Loading repairs require a confirmed stopped server. "
                    "Current status: " + str(result["status"]),
                )
            else:
                QMessageBox.warning(
                    self,
                    "Status changed",
                    "The server is no longer confirmed stopped. No repair was applied.",
                )
            return

        if phase == "diagnose":
            self._present_loading_diagnosis(instance, result)
        elif phase == "apply":
            self._present_loading_repair_result(instance, result)

    def _present_loading_diagnosis(self, instance: ServerInstance, res) -> None:
        diag = res.diagnosis
        if not diag.found_crash:
            self._show_loading_details(
                instance,
                "No server log was found. Start once to collect one, then re-check Setup and this diagnosis.",
            )
            return
        if diag.is_clean:
            self._show_loading_details(
                instance,
                "No safe automatic repair was identified. Review the latest log below; no files have been changed.",
            )
            return

        summary = diag.human_summary()
        culprits = res.quarantined
        if not culprits:
            extra = ""
            if res.unresolved:
                extra = "\n\nCould not locate jars for: " + ", ".join(res.unresolved)
            self._show_loading_details(
                instance,
                summary + extra + "\nNo safe automatic repair is available; no files were changed.",
            )
            return

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Fix loading errors")
        msg.setText(summary)
        msg.setInformativeText(
            "Disable (quarantine) these client-only mod(s) so the server can "
            "start?\n\n  • " + "\n  • ".join(culprits)
            + "\n\nThey will be renamed to *.jar.disabled and can be re-enabled "
            "from the Mods tab at any time."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        # Wait until this diagnosis thread has fully stopped before starting
        # the apply worker. A modal confirmation can run a nested Qt event
        # loop, so starting a replacement QThread directly here races cleanup.
        self._loading_repair_pending_apply = instance
        self.statusBar().showMessage(
            "Confirmation received. Rechecking server state before applying..."
        )
        # The modal confirmation runs a nested event loop. The diagnosis
        # thread may already have stopped and cleaned itself up while the
        # prompt was open, so handle both the already-idle and still-busy case.
        self._start_pending_loading_repair_if_idle()

    def _present_loading_repair_result(self, instance: ServerInstance, applied) -> None:
        done = applied.quarantined
        tail = ""
        if applied.unresolved:
            tail = (
                "\n\nCould not find jars for: "
                + ", ".join(applied.unresolved)
                + "\nRemove these from mods/ manually."
            )
        QMessageBox.information(
            self,
            "Fix loading errors",
            f"Disabled {len(done)} client-only mod(s):\n  • "
            + "\n  • ".join(done)
            + tail
            + "\n\nYou can start the server again now.",
        )

    def _on_export_requested(self, instance: ServerInstance) -> None:
        """Open the review-first player-sharing flow for this server."""
        from .share_players_dialog import SharePlayersDialog
        SharePlayersDialog(instance, self, manager=self._manager).exec()

    def _on_status_changed(self, instance_id: str, status: str) -> None:
        effective = status
        if (
            self._panel.current_instance_id() == instance_id
            and self._panel._current_status in {"starting", "stopping"}
        ):
            effective = self._panel._current_status
        self._status_map[instance_id] = effective
        if effective in {"starting", "stopping"}:
            self._transient_status[instance_id] = effective
        else:
            self._transient_status.pop(instance_id, None)
        self._sidebar.update_status(instance_id, effective)
        if self._tray_controller is not None:
            self._tray_controller.refresh_server_menu()

    def _on_remove_requested(self, instance) -> None:
        # Destructive decisions never trust the periodically cached sidebar status.
        # Probe the exact tmux target now and fail closed on timeout/error.
        safe, reason = self._tmux.safe_to_remove(instance)
        if not safe:
            QMessageBox.warning(
                self, "Cannot safely remove server",
                f"Crucible will not remove {instance.name} from its registry or "
                f"delete its files because {reason}. Stop the server completely "
                "and retry. No files or registry rows were changed.",
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle("Remove Instance")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f'Remove "{instance.name}" from Crucible?')
        box.setInformativeText(
            "• Remove from list keeps every server file on disk.\n"
            "• Delete server files permanently removes worlds, mods, and configs "
            f"from:\n   {instance.path}\n\n"
            "Backups in ~/.local/share/crucible-backups are kept."
        )
        remove_btn = box.addButton("Remove from list", QMessageBox.ButtonRole.AcceptRole)
        delete_btn = box.addButton("Delete server files", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return
        if not self._panel.prepare_to_remove(instance):
            return
        original_index = next(
            (i for i, item in enumerate(self._manager.instances) if item.id == instance.id),
            len(self._manager.instances),
        )
        if clicked is remove_btn:
            try:
                self._manager.remove_instance(instance.id)
            except Exception as exc:
                QMessageBox.critical(self, "Remove failed", f"Could not update the instance registry:\n{exc}")
                return
            self._sidebar.remove_instance(instance.id)
        elif clicked is delete_btn:
            try:
                target = validate_delete_target(instance.path)
            except ValueError as exc:
                QMessageBox.critical(
                    self, "Unsafe delete blocked",
                    f"Crucible refused to recursively delete:\n\n{instance.path}\n\n"
                    f"{exc}\n\nRemove it from the list instead and inspect the path manually.",
                )
                return
            confirm = QMessageBox.warning(
                self, "Permanently delete server files",
                f'Permanently delete all server files for "{instance.name}"?\n\n'
                f"{target}\n\nWorlds, mods, and configs will be deleted. External "
                "Crucible backups are kept. This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            quarantine = target.with_name(f".{target.name}.crucible-delete-{uuid.uuid4().hex}")
            try:
                target.rename(quarantine)
            except OSError as exc:
                QMessageBox.critical(self, "Delete failed", f"Could not safely stage the server folder for deletion:\n{exc}")
                return
            try:
                self._manager.remove_instance(instance.id)
            except Exception as exc:
                if all(item.id != instance.id for item in self._manager.instances):
                    self._manager.instances.insert(original_index, instance)
                    try:
                        self._manager.save()
                    except OSError:
                        pass
                try:
                    quarantine.rename(target)
                except OSError:
                    pass
                QMessageBox.critical(self, "Delete cancelled", f"Could not update the registry, so Crucible restored the folder:\n{exc}")
                return
            try:
                shutil.rmtree(quarantine, ignore_errors=False)
            except OSError as exc:
                try:
                    if quarantine.exists() and not target.exists():
                        quarantine.rename(target)
                except OSError:
                    pass
                if all(item.id != instance.id for item in self._manager.instances):
                    self._manager.instances.insert(original_index, instance)
                    try:
                        self._manager.save()
                    except OSError:
                        pass
                QMessageBox.critical(
                    self, "Delete incomplete",
                    f"Deletion failed and Crucible restored the instance where possible:\n{exc}\n\nInspect: {target}",
                )
                return
            self._sidebar.remove_instance(instance.id)
            print(f'[crucible] Deleted "{instance.name}" server files; external backups kept')
        self._transient_status.pop(instance.id, None)
        if self._sidebar.selected_instance() is None:
            self._panel.clear()
        self._update_status_bar()

    # Drag and drop import

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            from PyQt6.QtWidgets import QWidget
            if isinstance(watched, QWidget) and (watched is self or self.isAncestorOf(watched)):
                mime = event.mimeData()
                paths = [u.toLocalFile() for u in mime.urls()] if mime.hasUrls() else []
                # Let internal reorders and mod JAR drops keep their own handlers.
                if not mime.hasFormat("application/x-crucible-instance") and paths and all(Path(p).is_dir() or Path(p).suffix.lower() in (".zip", ".mrpack") for p in paths if p):
                    if not all(paths):
                        return False
                    event.acceptProposedAction()
                    if event.type() == QEvent.Type.Drop:
                        QTimer.singleShot(0, lambda items=list(paths): self._on_paths_dropped(items))
                    return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.toLocalFile()]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        for p in paths:
            self._import_dropped_path(Path(p))

    def _import_dropped_path(self, path: Path) -> None:
        """Import a dropped Prism/archive through the async Add Server flow."""
        if not path.exists():
            return
        archive = path.is_file() and path.suffix.lower() in (".zip", ".mrpack")
        is_prism = path.is_dir() and (
            (path / "mmc-pack.json").exists() or (path / "instance.cfg").exists()
        )
        is_server = path.is_dir() and (
            (path / "server.properties").exists()
            or (path / "eula.txt").exists()
            or (path / "mods").is_dir()
        )

        if archive or is_prism:
            from .add_dialog import AddInstanceDialog

            dlg = AddInstanceDialog(self._manager, self)
            QTimer.singleShot(0, lambda: dlg.begin_import_source(str(path)))
            if dlg.exec() and dlg.result_instance is not None:
                self._show_registered_instance(dlg.result_instance)
                self._update_status_bar()
            return

        if not is_server:
            QMessageBox.information(
                self, "Drag and drop",
                "Drop a Prism/MultiMC instance folder, a .zip/.mrpack modpack, "
                "or a Minecraft server folder to import it.",
            )
            return
        # A dropped server can contain a very large nested mods/ tree. Keep its
        # validation off the GUI thread, then commit the already-checked result.
        self._start_dropped_server_validation(path)

    def _start_dropped_server_validation(self, path: Path) -> None:
        from .instance_validation import ServerValidationWorker
        from .thread_lifecycle import connect_thread_cleanup

        if self._drop_validation_thread is not None:
            self.statusBar().showMessage(
                "A dropped server folder is already being checked.", 5000
            )
            return

        candidate = ServerInstance(
            str(path), path.name or "Imported server"
        )
        thread = QThread(self)
        thread.setObjectName("CrucibleDroppedServerValidation")
        worker = ServerValidationWorker(candidate)
        self._drop_validation_thread = thread
        self._drop_validation_worker = worker
        self._drop_validation_result = None
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_dropped_server_validated)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        connect_thread_cleanup(
            thread, lambda: self._dropped_server_validation_thread_finished(thread)
        )
        self.statusBar().showMessage("Checking dropped server folder…")
        thread.start()

    @pyqtSlot(object, object)
    def _on_dropped_server_validated(self, candidate, problems) -> None:
        self._drop_validation_result = (candidate, list(problems))

    def _dropped_server_validation_thread_finished(self, thread: QThread) -> None:
        if self._drop_validation_thread is not thread:
            return
        result = self._drop_validation_result
        self._drop_validation_thread = None
        self._drop_validation_worker = None
        self._drop_validation_result = None
        if result is None:
            self.statusBar().showMessage("Dropped server validation returned no result.", 8000)
            return

        candidate, problems = result
        try:
            inst = self._manager.add_instance(
                candidate.path, candidate.name, validation_problems=problems
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        # The validation worker has already completed the expensive folder
        # scan. Let the existing health worker determine whether an imported
        # server is already running; do not add a synchronous tmux/procfs probe
        # to the GUI-thread completion callback.
        self._sidebar.add_instance(inst, "checking")
        self._sidebar.select_by_id(inst.id)
        self._health_check()
        self._update_status_bar()
        message = (
            f"Added {inst.name}. Review Diagnostics for folder warnings."
            if problems
            else f"Added {inst.name}."
        )
        self.statusBar().showMessage(message, 8000)

    # Close

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._drop_validation_thread is not None:
            self.statusBar().showMessage(
                "Wait for the dropped server folder check to finish before closing."
            )
            event.ignore()
            return
        if (
            self._loading_repair_thread is not None
            or self._loading_repair_pending_apply is not None
        ):
            self.statusBar().showMessage(
                "Wait for the loading diagnosis or repair to finish before closing."
            )
            event.ignore()
            return
        if self._panel.has_active_operations():
            QMessageBox.information(
                self, "Operation in progress",
                "Crucible is still starting, stopping, restarting, or backing up a server. "
                "Wait for that operation to finish before closing.",
            )
            event.ignore()
            return

        if (
            not self._quit_requested
            and self._tray_controller is not None
            and self._tray_controller.visible
            and QSystemTrayIcon.isSystemTrayAvailable()
        ):
            # A normal window close hides the UI only. The same window,
            # status worker and tray controller remain alive; Minecraft
            # sessions are never modified by this transition.
            event.ignore()
            self.hide()
            return

        self._cancel_identity_transition()
        self._health_timer.stop()
        if self._health_thread.isRunning():
            self._health_thread.quit()
            if not self._health_thread.wait(3000):
                self._health_timer.start()
                QMessageBox.information(
                    self, "Health check finishing",
                    "Wait a moment for the tmux health check to finish, then close again.",
                )
                event.ignore()
                return
        if self._tray_controller is not None:
            self._tray_controller.shutdown()
        self._panel.shutdown()
        super().closeEvent(event)
