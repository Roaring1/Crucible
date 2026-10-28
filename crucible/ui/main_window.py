"""
crucible/ui/main_window.py

Top-level application window.

Layout
──────
  QSplitter (horizontal)
    │
    ├── Sidebar (240px, fixed) ── instance list, status dots
    │
    └── InstancePanel (stretches) ── header + tabbed content

Health check: QTimer fires every 5s, calls tmux.status_map() once,
pushes updates to sidebar and instance panel.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, Qt, QObject, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QStatusBar, QLabel,
    QMessageBox, QInputDialog, QMenu,
)

from ..data.instance_manager import InstanceManager, validate_delete_target
from ..data.instance_model import ServerInstance
from ..process.crash_recovery import reconcile as reconcile_crash_recovery
from ..process.tmux_manager import TmuxManager
from . import theme
from .sidebar import Sidebar
from .instance_panel import InstancePanel
from .add_dialog import AddInstanceDialog

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


class MainWindow(QMainWindow):
    """Crucible main window."""

    health_check_requested = pyqtSignal(object)

    def __init__(self, manager: InstanceManager):
        super().__init__()
        self._manager = manager
        self._tmux = TmuxManager()
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

        self.setWindowTitle("Crucible — Minecraft Server Manager")
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)  # drag in Prism instances / packs / server dirs

        self._build_ui()
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)
        self._populate_sidebar()
        self._start_health_timer()

        # Auto-select first instance
        if manager.instances:
            self._sidebar.select_by_id(manager.instances[0].id)

        # Detect + self-heal instances that went down together with the
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
        self._sidebar.fix_loading_requested.connect(self._on_fix_loading_requested)
        self._sidebar.rename_requested.connect(self._on_rename_requested)
        self._sidebar.export_requested.connect(self._on_export_requested)
        self._sidebar.order_changed.connect(self._on_order_changed)
        self._sidebar.paths_dropped.connect(self._on_paths_dropped)
        self._splitter.addWidget(self._sidebar)

        # Right: instance panel (must be created before wiring sidebar RMB signals)
        self._panel = InstancePanel(self._manager)
        self._panel.status_changed.connect(self._on_status_changed)
        self._splitter.addWidget(self._panel)

        # Wire sidebar context-menu actions now that _panel exists
        self._sidebar.start_requested.connect(self._panel._do_start_for)
        self._sidebar.stop_requested.connect(self._panel._do_stop_for)
        self._sidebar.restart_requested.connect(self._panel._do_restart_for)

        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([240, 960])

        # Status bar
        sb = QStatusBar()
        sb.setFixedHeight(24)
        self.setStatusBar(sb)

        self._sb_instances = QLabel("")
        self._sb_tmux      = QLabel("")
        sb.addWidget(self._sb_instances)
        sb.addPermanentWidget(self._sb_tmux)

        self._update_status_bar()

    # Population

    def _populate_sidebar(self) -> None:
        initial = "stopped" if self._tmux.tmux_available() else "tmux_missing"
        status_map = {inst.id: initial for inst in self._manager.instances}
        self._sidebar.populate(self._manager.instances, status_map)
        self._update_status_bar()

    # Crash recovery (whole-host crashes, e.g. power loss, that no live
    # Watchdog could have witnessed because Crucible itself was not running)

    def _run_crash_recovery(self) -> None:
        if not self._tmux.tmux_available():
            return
        try:
            reports = reconcile_crash_recovery(self._manager.instances, self._tmux)
        except Exception as exc:
            print(f"[crucible] crash recovery check failed: {exc}")
            return
        if not reports:
            return

        by_id = {inst.id: inst for inst in self._manager.instances}
        restarted: list[str] = []
        not_restarted: list[str] = []

        for report in reports:
            instance = by_id.get(report.instance_id)
            if instance is not None and instance.auto_restart:
                ok, msg = self._tmux.start(instance)
                if ok:
                    instance.last_started = datetime.now().isoformat()
                    try:
                        self._manager.update_instance(instance)
                    except Exception as exc:
                        print(f"[crucible] could not persist auto-restart: {exc}")
                    restarted.append(instance.name)
                else:
                    not_restarted.append(f"{instance.name} (restart failed: {msg})")
            else:
                not_restarted.append(report.instance_name)

        lines = [
            "Crucible found that one or more servers were still marked running "
            "the last time it saw them, but the system has since rebooted "
            "without a normal stop ever being recorded. This points to the "
            "whole PC losing power or freezing, not a Crucible or server bug.",
            "",
        ]
        for report in reports:
            lines.append(f"• {report.message}")
            if report.log_evidence:
                lines.append(f"  {report.log_evidence}")
        if restarted:
            lines.append("")
            lines.append("Auto-restarted (auto-restart was enabled): " + ", ".join(restarted))
        if not_restarted:
            lines.append("")
            lines.append(
                "Not auto-restarted (enable auto-restart on the server's settings "
                "to do this automatically next time): " + ", ".join(not_restarted)
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
        self._sidebar.update_all_statuses(status_map)
        selected = self._sidebar.selected_instance()
        if selected and selected.id in status_map:
            self._panel.update_status(status_map[selected.id])
        self._update_status_bar()

    # Status bar

    def _update_status_bar(self) -> None:
        n = len(self._manager.instances)
        self._sb_instances.setText(
            f"{n} instance{'s' if n != 1 else ''}"
        )
        if not self._tmux.tmux_available():
            self._sb_tmux.setText("tmux not found — install tmux")
            self._sb_tmux.setStyleSheet(f"color: {theme.RED};")
        elif n == 0:
            self._sb_tmux.setText("Ready to add a server")
            self._sb_tmux.setStyleSheet(f"color: {theme.GREEN};")
        else:
            self._sb_tmux.setText("Server console available")
            self._sb_tmux.setStyleSheet(f"color: {theme.GREEN};")

    # Event handlers

    def _on_instance_selected(self, instance: ServerInstance) -> None:
        previous_id = self._panel.current_instance_id()
        status_hint = self._sidebar.status_for(instance.id)
        if not self._panel.load(instance, status_hint=status_hint) and previous_id:
            QTimer.singleShot(0, lambda: self._sidebar.select_by_id(previous_id))

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
        self.statusBar().showMessage(f'Renamed to “{name}” · files and session unchanged', 5000)

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
            dlg = AddInstanceDialog(self._manager, self)

        if dlg.exec() and dlg.result_instance is not None:
            inst = dlg.result_instance
            status = self._tmux.get_status(inst)
            already_listed = inst.id in self._sidebar._items
            if already_listed:
                self._sidebar.update_status(inst.id, status)
            else:
                self._sidebar.add_instance(inst, status)
            self._sidebar.select_by_id(inst.id)
            message = (
                f'Selected existing server "{inst.name}"'
                if already_listed
                else f'Added "{inst.name}" to the server list'
            )
            self.statusBar().showMessage(message, 5000)
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
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
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
        """Diagnose a start/loading crash and offer to quarantine client-only mods."""
        status = self._tmux.get_status(instance)
        if status != "stopped":
            QMessageBox.warning(self, "Stop the server first", "Loading repairs require a confirmed stopped server. Current status: " + status)
            return
        from ..diagnostics import loadcheck as lc
        res = lc.autofix_loading(instance.path, apply=False)
        diag = res.diagnosis

        if not diag.found_crash:
            self._show_loading_details(instance, "No server log was found. Start once to collect one, then re-check Setup and this diagnosis.")
            return
        if diag.is_clean:
            self._show_loading_details(instance, "No safe automatic repair was identified. Review the latest log below; no files have been changed.")
            return

        summary = diag.human_summary()
        culprits = res.quarantined  # filenames we would disable (dry run)
        if not culprits:
            extra = ""
            if res.unresolved:
                extra = ("\n\nCould not locate jars for: "
                         + ", ".join(res.unresolved))
            self._show_loading_details(instance, summary + extra + "\nNo safe automatic repair is available; no files were changed.")
            return

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Fix loading errors")
        msg.setText(summary)
        msg.setInformativeText(
            "Disable (quarantine) these client-only mod(s) so the server can "
            "start?\n\n  • " + "\n  • ".join(culprits)
            + "\n\nThey will be renamed to *.jar.disabled and can be re-enabled "
            "from the Mods tab at any time.")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        if self._tmux.get_status(instance) != "stopped":
            QMessageBox.warning(self, "Status changed", "The server is no longer confirmed stopped. No repair was applied.")
            return
        try:
            applied = lc.autofix_loading(instance.path, apply=True)
        except OSError as exc:
            self._show_loading_details(instance, "Repair could not complete: " + str(exc))
            return
        done = applied.quarantined
        tail = ""
        if applied.unresolved:
            tail = ("\n\nCould not find jars for: "
                    + ", ".join(applied.unresolved)
                    + "\nRemove these from mods/ manually.")
        QMessageBox.information(
            self, "Fix loading errors",
            f"Disabled {len(done)} client-only mod(s):\n  • "
            + "\n  • ".join(done) + tail
            + "\n\nYou can start the server again now.")

    def _on_export_requested(self, instance: ServerInstance) -> None:
        """Open the review-first player-sharing flow for this server."""
        from .share_players_dialog import SharePlayersDialog
        SharePlayersDialog(instance, self, manager=self._manager).exec()

    def _on_status_changed(self, instance_id: str, status: str) -> None:
        self._sidebar.update_status(instance_id, status)

    def _on_remove_requested(self, instance) -> None:
        # Destructive decisions never trust the periodically cached sidebar dot.
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
            self.statusBar().showMessage(f'Deleted "{instance.name}" server files; external backups kept', 5000)
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
            dlg = AddInstanceDialog(self._manager, self)
            QTimer.singleShot(0, lambda: dlg.begin_import_source(str(path)))
            if dlg.exec() and dlg.result_instance is not None:
                inst = dlg.result_instance
                status = self._tmux.get_status(inst)
                self._sidebar.add_instance(inst, status)
                self._sidebar.select_by_id(inst.id)
                self._update_status_bar()
            return

        if not is_server:
            QMessageBox.information(
                self, "Drag and drop",
                "Drop a Prism/MultiMC instance folder, a .zip/.mrpack modpack, "
                "or a Minecraft server folder to import it.",
            )
            return
        try:
            # The source folder already gives a useful default name. The user can
            # rename it from the sidebar later, so don't interrupt a drag-and-drop
            # import with a second pop-up.
            inst = self._manager.add_instance(str(path), path.name or "Imported server")
        except ValueError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        status = self._tmux.get_status(inst)
        self._sidebar.add_instance(inst, status)
        self._sidebar.select_by_id(inst.id)
        self._update_status_bar()

    # Close

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._panel.has_active_operations():
            QMessageBox.information(
                self, "Operation in progress",
                "Crucible is still starting, stopping, restarting, or backing up a server. "
                "Wait for that operation to finish before closing.",
            )
            event.ignore()
            return
        self._health_timer.stop()
        if self._health_thread.isRunning():
            self._health_thread.quit()
            if not self._health_thread.wait(3000):
                QMessageBox.information(
                    self, "Health check finishing",
                    "Wait a moment for the tmux health check to finish, then close again.",
                )
                event.ignore()
                return
        self._panel.shutdown()
        super().closeEvent(event)
