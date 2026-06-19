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
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QWidget, QStatusBar, QLabel,
    QMessageBox, QApplication,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from ..process.tmux_manager import TmuxManager
from . import theme
from .sidebar import Sidebar
from .instance_panel import InstancePanel
from .add_dialog import AddInstanceDialog

HEALTH_CHECK_INTERVAL_MS = 5_000


class MainWindow(QMainWindow):
    """Crucible main window."""

    def __init__(self, manager: InstanceManager):
        super().__init__()
        self._manager = manager
        self._tmux    = TmuxManager()

        self.setWindowTitle("Crucible — Minecraft Server Manager")
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)  # drag in Prism instances / packs / server dirs

        self._build_ui()
        self._populate_sidebar()
        self._start_health_timer()

        # Auto-select first instance
        if manager.instances:
            self._sidebar.select_by_id(manager.instances[0].id)

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
        status_map = self._tmux.status_map(self._manager.instances)
        self._sidebar.populate(self._manager.instances, status_map)
        self._update_status_bar()

    # Health check timer

    def _start_health_timer(self) -> None:
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(HEALTH_CHECK_INTERVAL_MS)
        self._health_timer.timeout.connect(self._health_check)
        self._health_timer.start()

    def _health_check(self) -> None:
        status_map = self._tmux.status_map(self._manager.instances)
        self._sidebar.update_all_statuses(status_map)

        # Update panel if the selected instance changed status
        selected = self._sidebar.selected_instance()
        if selected:
            new_status = status_map.get(selected.id, "stopped")
            self._panel.update_status(new_status)

        self._update_status_bar()

    # Status bar

    def _update_status_bar(self) -> None:
        n = len(self._manager.instances)
        self._sb_instances.setText(
            f"{n} instance{'s' if n != 1 else ''}"
        )
        if self._tmux.tmux_available():
            self._sb_tmux.setText("tmux ✓")
            self._sb_tmux.setStyleSheet(f"color: {theme.GREEN};")
        else:
            self._sb_tmux.setText("tmux not found")
            self._sb_tmux.setStyleSheet(f"color: {theme.RED};")

    # Event handlers

    def _on_instance_selected(self, instance: ServerInstance) -> None:
        self._panel.load(instance)

    def _on_add_requested(self) -> None:
        dlg = AddInstanceDialog(self._manager, self)
        if dlg.exec() and dlg.result_instance:
            inst   = dlg.result_instance
            status = self._tmux.get_status(inst)
            self._sidebar.add_instance(inst, status)
            self._sidebar.select_by_id(inst.id)
            self._update_status_bar()

    def _on_status_changed(self, instance_id: str, status: str) -> None:
        self._sidebar.update_status(instance_id, status)

    def _on_remove_requested(self, instance) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Remove Instance")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"Remove \"{instance.name}\" from Crucible?")
        box.setInformativeText(
            "\u2022 Remove from list keeps the server files on disk "
            "(you can re-add them later).\n"
            "\u2022 Delete files too permanently removes the entire server "
            f"folder:\n   {instance.path}\n\nThis cannot be undone.")
        remove_btn = box.addButton("Remove from list",
                                   QMessageBox.ButtonRole.AcceptRole)
        delete_btn = box.addButton("Delete files too",
                                   QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()

        if clicked is cancel_btn or clicked is None:
            return

        path = getattr(instance, "path", "") or ""

        if clicked is delete_btn:
            # Second confirmation for the irreversible option.
            confirm = QMessageBox.warning(
                self, "Delete files from disk",
                f"Permanently delete ALL files for \"{instance.name}\"?\n\n"
                f"{path}\n\nThis includes worlds, configs and backups. "
                "This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        # Always unregister first so the registry never points at deleted files.
        self._manager.remove_instance(instance.id)
        self._sidebar.remove_instance(instance.id)

        if clicked is delete_btn and path:
            try:
                shutil.rmtree(path, ignore_errors=False)
            except OSError as e:
                QMessageBox.critical(
                    self, "Delete failed",
                    f"Removed \"{instance.name}\" from the list, but the files "
                    f"could not be fully deleted:\n\n{e}\n\n"
                    "You may need to delete the folder manually.")
            else:
                self.statusBar().showMessage(
                    f"Deleted \"{instance.name}\" and its files from disk", 5000)

        self._update_status_bar()

    # Drag and drop import

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
        """Import a dropped Prism instance folder, pack archive, or server dir."""
        if not path.exists():
            return
        name = path.stem if path.is_file() else path.name
        archive = path.is_file() and path.suffix.lower() in (".zip", ".mrpack")
        is_prism = path.is_dir() and (
            (path / "mmc-pack.json").exists() or (path / "instance.cfg").exists())
        is_server = path.is_dir() and (
            (path / "server.properties").exists()
            or (path / "eula.txt").exists()
            or (path / "mods").is_dir())

        try:
            if archive or is_prism:
                from ..importers.prism import import_prism_source
                target = Path.home() / "CrucibleServers" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    import_prism_source(str(path), str(target))
                finally:
                    QApplication.restoreOverrideCursor()
                inst = self._manager.add_instance(str(target), name)
            elif is_server:
                inst = self._manager.add_instance(str(path), name)
            else:
                QMessageBox.information(
                    self, "Drag and drop",
                    "Drop a Prism/MultiMC instance folder, a .zip/.mrpack "
                    "modpack, or a Minecraft server folder to import it.")
                return
        except ValueError as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return
        except Exception as e:  # noqa: BLE001 - surface import failure to user
            QMessageBox.critical(self, "Import failed",
                                 f"Could not import \"{name}\":\n\n{e}")
            return

        status = self._tmux.get_status(inst)
        self._sidebar.add_instance(inst, status)
        self._sidebar.select_by_id(inst.id)
        self._update_status_bar()

    # Close

    def closeEvent(self, event: QCloseEvent) -> None:
        self._health_timer.stop()
        self._panel.closeEvent(event)
        super().closeEvent(event)
