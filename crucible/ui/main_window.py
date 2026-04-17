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

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QWidget, QStatusBar, QLabel,
    QMessageBox,
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

        self.setWindowTitle("Crucible — GTNH Server Manager")
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)

        self._build_ui()
        self._populate_sidebar()
        self._start_health_timer()

        # Auto-select first instance
        if manager.instances:
            self._sidebar.select_by_id(manager.instances[0].id)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Central widget: splitter ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        # Left: sidebar
        self._sidebar = Sidebar()
        self._sidebar.instance_selected.connect(self._on_instance_selected)
        self._sidebar.add_requested.connect(self._on_add_requested)
        self._sidebar.remove_requested.connect(self._on_remove_requested)
        splitter.addWidget(self._sidebar)

        # Right: instance panel (must be created before wiring sidebar RMB signals)
        self._panel = InstancePanel(self._manager)
        self._panel.status_changed.connect(self._on_status_changed)
        splitter.addWidget(self._panel)

        # Wire sidebar context-menu actions now that _panel exists
        self._sidebar.start_requested.connect(self._panel._do_start_for)
        self._sidebar.stop_requested.connect(self._panel._do_stop_for)
        self._sidebar.restart_requested.connect(self._panel._do_restart_for)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 960])

        # ── Status bar ──
        sb = QStatusBar()
        sb.setFixedHeight(24)
        self.setStatusBar(sb)

        self._sb_instances = QLabel("")
        self._sb_tmux      = QLabel("")
        sb.addWidget(self._sb_instances)
        sb.addPermanentWidget(self._sb_tmux)

        self._update_status_bar()

    # ── Population ────────────────────────────────────────────────────────────

    def _populate_sidebar(self) -> None:
        status_map = self._tmux.status_map(self._manager.instances)
        self._sidebar.populate(self._manager.instances, status_map)
        self._update_status_bar()

    # ── Health check timer ────────────────────────────────────────────────────

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

    # ── Status bar ────────────────────────────────────────────────────────────

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

    # ── Event handlers ────────────────────────────────────────────────────────

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
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "Remove Instance",
            f"Remove \"{instance.name}\" from Crucible?\n\n"
            f"The server files on disk are NOT deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._manager.remove_instance(instance.id)
            self._sidebar.remove_instance(instance.id)
            self._update_status_bar()

    # ── Close ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        self._health_timer.stop()
        self._panel.closeEvent(event)
        super().closeEvent(event)
