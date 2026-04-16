"""
crucible/ui/tabs/mods_tab.py

Mods tab: table of enabled/disabled mods with filter, enable/disable toggle,
and add-from-file support (drag-and-drop + file picker).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QPushButton, QLineEdit, QAbstractItemView,
    QFileDialog, QMessageBox, QCheckBox,
)

from ...data.instance_model import ServerInstance
from ...mods.mod_manager import ModManager, ModEntry
from .. import theme


# ── Background inspection worker ──────────────────────────────────────────────

class _InspectWorker(QObject):
    """Reads mod metadata from jar files in a background thread."""
    done = pyqtSignal(int, object)  # (row_index, ModEntry)

    def __init__(self, manager: ModManager, jobs: list[tuple[int, ModEntry]]):
        super().__init__()
        self._manager = manager
        self._jobs    = jobs

    def run(self) -> None:
        for row, mod in self._jobs:
            self._manager.inspect_jar(mod)
            self.done.emit(row, mod)


# ── Main tab ──────────────────────────────────────────────────────────────────

class ModsTab(QWidget):
    """Mod management table for one server instance."""

    _COL_ENABLED = 0
    _COL_NAME    = 1
    _COL_VERSION = 2
    _COL_SIZE    = 3
    _COL_ACTIONS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager: ModManager | None = None
        self._mods:    list[ModEntry]    = []
        self._thread:  QThread | None    = None

        self._build_ui()
        self.setAcceptDrops(True)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── Toolbar ──
        toolbar = QHBoxLayout()

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter mods…")
        self._filter.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._filter, stretch=1)

        add_btn = QPushButton("Add .jar…")
        add_btn.clicked.connect(self._pick_file)
        toolbar.addWidget(add_btn)

        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedWidth(36)
        refresh_btn.setToolTip("Refresh mod list")
        refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_btn)

        layout.addLayout(toolbar)

        # ── Table ──
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(
            ["", "Mod Name", "Version", "Size", ""]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            self._COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            self._COL_ENABLED, QHeaderView.ResizeMode.Fixed
        )
        self._table.setColumnWidth(self._COL_ENABLED, 40)
        self._table.setColumnWidth(self._COL_VERSION, 120)
        self._table.setColumnWidth(self._COL_SIZE,    80)
        self._table.setColumnWidth(self._COL_ACTIONS, 100)

        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)

        layout.addWidget(self._table, stretch=1)

        # ── Footer ──
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(self._count_label)

        # ── Drop hint ──
        self._drop_hint = QLabel("Drop .jar files here to add mods")
        self._drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setStyleSheet(
            f"color: {theme.SURFACE2}; font-size: 12px; padding: 4px;"
        )
        layout.addWidget(self._drop_hint)

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, instance: ServerInstance) -> None:
        """Switch to displaying mods for the given instance."""
        self._manager = ModManager(instance)
        self.refresh()

    def refresh(self) -> None:
        if self._manager is None:
            return
        self._mods = self._manager.list_mods()
        self._populate_table(self._mods)
        self._apply_filter(self._filter.text())
        self._update_count()
        self._start_inspect_pass()

    # ── Table population ──────────────────────────────────────────────────────

    def _populate_table(self, mods: list[ModEntry]) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        self._table.setRowCount(len(mods))

        for row, mod in enumerate(mods):
            is_bundled = getattr(mod, "bundled", False)

            if is_bundled:
                # Bundled jars: informational row, no toggle/delete actions
                # Show a lock icon in the enabled column
                lock_label = QLabel("🔒")
                lock_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lock_label.setToolTip(
                    f"Bundled library inside mods/{mod.path.parent.name}/\n"
                    "Not manageable via the mods tab — edit manually."
                )
                self._table.setCellWidget(row, self._COL_ENABLED, lock_label)
            else:
                # Normal mod: enable/disable checkbox
                cb = QCheckBox()
                cb.setChecked(mod.enabled)
                cb.setToolTip("Enable / disable this mod")
                cb_container = QWidget()
                cb_layout = QHBoxLayout(cb_container)
                cb_layout.addWidget(cb)
                cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cb_layout.setContentsMargins(0, 0, 0, 0)
                cb.toggled.connect(lambda checked, m=mod, r=row: self._toggle_mod(m, r, checked))
                self._table.setCellWidget(row, self._COL_ENABLED, cb_container)

            # Name
            if is_bundled:
                display = f"{mod.display_name}  [bundled in {mod.path.parent.name}/]"
            else:
                display = mod.display_name
            name_item = QTableWidgetItem(display)
            name_item.setToolTip(str(mod.path))
            if is_bundled:
                name_item.setForeground(Qt.GlobalColor.darkCyan)
            elif not mod.enabled:
                name_item.setForeground(Qt.GlobalColor.gray)
            self._table.setItem(row, self._COL_NAME, name_item)

            # Version (filled later by inspect pass)
            ver_item = QTableWidgetItem(mod.version or "—")
            ver_item.setForeground(Qt.GlobalColor.gray)
            self._table.setItem(row, self._COL_VERSION, ver_item)

            # Size
            size_item = QTableWidgetItem(mod.size_mb)
            size_item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            )
            self._table.setItem(row, self._COL_SIZE, size_item)

            # Actions column: no button for bundled jars
            if not is_bundled:
                del_btn = QPushButton("Delete")
                del_btn.setObjectName("DangerButton")
                del_btn.setFixedHeight(26)
                del_btn.clicked.connect(lambda _, m=mod, r=row: self._delete_mod(m, r))
                self._table.setCellWidget(row, self._COL_ACTIONS, del_btn)

            self._table.setRowHeight(row, 36)

        self._table.setSortingEnabled(True)

    def _apply_filter(self, text: str) -> None:
        text = text.lower().strip()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_NAME)
            if item is None:
                continue
            match = (not text) or (text in item.text().lower())
            self._table.setRowHidden(row, not match)

    def _update_count(self) -> None:
        enabled  = sum(1 for m in self._mods if m.enabled)
        bundled  = sum(1 for m in self._mods if getattr(m, "bundled", False))
        disabled = len(self._mods) - enabled - bundled
        parts = [f"{enabled} enabled", f"{disabled} disabled"]
        if bundled:
            parts.append(f"{bundled} bundled (not manageable)")
        self._count_label.setText("  ·  ".join(parts))

    # ── Mod actions ───────────────────────────────────────────────────────────

    def _toggle_mod(self, mod: ModEntry, row: int, enable: bool) -> None:
        if self._manager is None:
            return
        try:
            if enable:
                updated = self._manager.enable(mod)
            else:
                updated = self._manager.disable(mod)
            # Update the row's mod reference and name color
            if row < len(self._mods):
                self._mods[row] = updated
            name_item = self._table.item(row, self._COL_NAME)
            if name_item:
                name_item.setForeground(
                    Qt.GlobalColor.white if enable else Qt.GlobalColor.gray
                )
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Could not toggle mod:\n{exc}")

    def _delete_mod(self, mod: ModEntry, _row: int) -> None:
        if self._manager is None:
            return
        reply = QMessageBox.question(
            self,
            "Delete Mod",
            f"Permanently delete:\n{mod.filename}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self._manager.delete(mod)
                self.refresh()
            except OSError as exc:
                QMessageBox.critical(self, "Error", f"Could not delete:\n{exc}")

    # ── Add from file / drag-drop ─────────────────────────────────────────────

    def _pick_file(self) -> None:
        if self._manager is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select mod jars", str(Path.home()),
            "Minecraft Mods (*.jar);;All files (*)"
        )
        for p in paths:
            try:
                self._manager.add_from_file(Path(p))
            except OSError as exc:
                QMessageBox.critical(self, "Error", f"Could not add {p}:\n{exc}")
        if paths:
            self.refresh()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if self._manager is None:
            return
        added = 0
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".jar":
                try:
                    self._manager.add_from_file(path)
                    added += 1
                except OSError:
                    pass
        if added:
            self.refresh()

    # ── Background jar inspection ─────────────────────────────────────────────

    def _start_inspect_pass(self) -> None:
        """Kick off background jar inspection to fill in mod names/versions."""
        if self._manager is None or not self._mods:
            return
        if self._thread and self._thread.isRunning():
            return  # Previous pass still running

        jobs = [
            (i, mod) for i, mod in enumerate(self._mods)
            if not mod.name and not mod.version
        ]
        if not jobs:
            return

        self._thread  = QThread()
        self._worker  = _InspectWorker(self._manager, jobs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_inspect_result)
        self._thread.start()

    def _on_inspect_result(self, row: int, mod: ModEntry) -> None:
        if row < self._table.rowCount():
            name_item = self._table.item(row, self._COL_NAME)
            ver_item  = self._table.item(row, self._COL_VERSION)
            if name_item and mod.name:
                name_item.setText(mod.name)
                name_item.setToolTip(f"{mod.filename}\n{mod.description}")
            if ver_item and mod.version:
                ver_item.setText(mod.version)
                ver_item.setForeground(Qt.GlobalColor.white)
