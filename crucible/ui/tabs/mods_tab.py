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


# Background inspection worker

class _InspectWorker(QObject):
    """Reads mod metadata from jar files in a background thread."""
    done = pyqtSignal(int, int, object)  # (generation, row_index, ModEntry)

    def __init__(self, manager: ModManager, jobs: list[tuple[int, ModEntry]], generation: int):
        super().__init__()
        self._manager = manager
        self._jobs = jobs
        self._generation = generation

    def run(self) -> None:
        for row, mod in self._jobs:
            self._manager.inspect_jar(mod)
            self.done.emit(self._generation, row, mod)
        self.thread().quit()  # signal the QThread event loop to exit


class _NetWorker(QObject):
    """Runs one network callable off the UI thread and reports the result."""
    done   = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001 - surface any failure to UI
            self.failed.emit(str(exc))


# Main tab

class ModsTab(QWidget):
    """Mod management table for one server instance."""

    _COL_ENABLED = 0
    _COL_NAME    = 1
    _COL_VERSION = 2
    _COL_SIZE    = 3
    _COL_ACTIONS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager: ModManager | None  = None
        self._mods:    list[ModEntry]     = []
        self._thread:  QThread | None     = None
        self._worker:  _InspectWorker | None = None
        self._net_thread: QThread | None     = None
        self._net_worker: _NetWorker | None  = None
        self._inspect_generation = 0
        self._inspect_pending = False

        self._build_ui()
        self.setAcceptDrops(True)

    # UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Toolbar
        toolbar = QHBoxLayout()

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter mods…")
        self._filter.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._filter, stretch=1)

        add_btn = QPushButton("Add .jar…")
        add_btn.clicked.connect(self._pick_file)
        toolbar.addWidget(add_btn)

        # One-click "find and install a mod" (searches Modrinth automatically).
        addmod_btn = QPushButton("✨ Add a mod")
        addmod_btn.setToolTip("Search Modrinth and install a mod (with its "
                              "dependencies) automatically.")
        addmod_btn.clicked.connect(self._open_add_mod)
        toolbar.addWidget(addmod_btn)

        # One-click "make a client instance" for friends (Prism/Modrinth/CF).
        client_btn = QPushButton("📦 Make client…")
        client_btn.setToolTip("Bundle this server's mods into a client pack "
                              "for Prism Launcher, Modrinth App, or CurseForge.")
        client_btn.clicked.connect(self._open_make_client)
        toolbar.addWidget(client_btn)

        # Shown only when the imported pack shipped a download index.
        self._dl_btn = QPushButton("⬇  Download from pack")
        self._dl_btn.setToolTip(
            "This pack listed mods to download rather than bundling them. "
            "Try to fetch them now (needs internet)."
        )
        self._dl_btn.clicked.connect(self._download_from_pack)
        self._dl_btn.hide()
        toolbar.addWidget(self._dl_btn)

        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedWidth(36)
        refresh_btn.setToolTip("Refresh mod list")
        refresh_btn.setAccessibleName("Refresh mod list")
        refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_btn)

        layout.addLayout(toolbar)

        # Table
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
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)

        layout.addWidget(self._table, stretch=1)

        # Footer
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(self._count_label)

        # Drop hint
        self._drop_hint = QLabel("Drop .jar files here to add mods")
        self._drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setStyleSheet(
            f"color: {theme.SURFACE2}; font-size: 12px; padding: 4px;"
        )
        layout.addWidget(self._drop_hint)

    # Public API

    def _open_add_mod(self) -> None:
        if getattr(self, "_instance", None) is None:
            return
        from ..add_mod_dialog import AddModDialog
        dlg = AddModDialog(self._instance, self)
        dlg.exec()
        self.refresh()

    def _open_make_client(self) -> None:
        if getattr(self, "_instance", None) is None:
            return
        from ..client_export_dialog import ClientExportDialog
        ClientExportDialog(self._instance, self).exec()

    def load(self, instance: ServerInstance) -> None:
        """Switch to displaying mods for the given instance."""
        self._manager = ModManager(instance)
        self._instance = instance
        # Reveal the download button only when a pack index is present.
        try:
            from ...importers.downloader import has_downloadable_index
            self._dl_btn.setVisible(has_downloadable_index(instance.path))
        except Exception:
            self._dl_btn.hide()
        self.refresh()

    def _download_from_pack(self) -> None:
        inst = getattr(self, "_instance", None)
        if inst is None:
            return
        try:
            from ..download_dialog import DownloadModsDialog
            dlg = DownloadModsDialog(inst.path, inst.name, parent=self)
            dlg.start()
            dlg.exec()
        except Exception as exc:
            QMessageBox.warning(self, "Download mods", f"Could not start download:\n{exc}")
        self.refresh()

    def refresh(self) -> None:
        if self._manager is None:
            return
        self._inspect_generation += 1
        self._mods = self._manager.list_mods()
        self._populate_table(self._mods)
        self._apply_filter(self._filter.text())
        self._update_count()
        self._start_inspect_pass()

    # Table population

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
                cb.setAccessibleName(f"Enable {mod.display_name}")
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
            name_item.setData(Qt.ItemDataRole.UserRole, mod)
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

    # Mod actions

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

    # Right-click context menu (Prism-style)

    def _show_context_menu(self, pos) -> None:
        if self._manager is None:
            return
        from PyQt6.QtWidgets import QMenu
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        name_item = self._table.item(row, self._COL_NAME)
        if name_item is None:
            return
        mod = name_item.data(Qt.ItemDataRole.UserRole)
        if mod is None:
            return

        is_bundled = getattr(mod, "bundled", False)
        menu = QMenu(self)
        if not is_bundled:
            if mod.enabled:
                menu.addAction("Disable").triggered.connect(
                    lambda: self._set_enabled(mod, False))
            else:
                menu.addAction("Enable").triggered.connect(
                    lambda: self._set_enabled(mod, True))
            menu.addSeparator()
            menu.addAction("Update (Modrinth)").triggered.connect(
                lambda: self._update_mod(mod))
            menu.addAction("Verify dependencies").triggered.connect(
                lambda: self._verify_deps())
            menu.addSeparator()
        menu.addAction("Open on Modrinth").triggered.connect(
            lambda: self._open_mod_page(mod))
        if not is_bundled:
            menu.addSeparator()
            menu.addAction("Delete").triggered.connect(
                lambda: self._delete_mod(mod, row))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _set_enabled(self, mod: ModEntry, enable: bool) -> None:
        if self._manager is None:
            return
        try:
            if enable:
                self._manager.enable(mod)
            else:
                self._manager.disable(mod)
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Could not toggle mod:\n{exc}")
            return
        self.refresh()

    # Network-backed actions (run off the UI thread)

    def _run_net(self, fn, on_done, busy: str = "Working\u2026") -> None:
        if self._net_thread is not None and self._net_thread.isRunning():
            QMessageBox.information(
                self, "Please wait",
                "Another Modrinth action is still running. Try again in a moment.")
            return
        self._count_label.setText(busy)
        self._net_thread = QThread()
        self._net_worker = _NetWorker(fn)
        self._net_worker.moveToThread(self._net_thread)
        self._net_thread.started.connect(self._net_worker.run)
        self._net_worker.done.connect(on_done)
        self._net_worker.failed.connect(self._on_net_error)
        self._net_worker.done.connect(self._net_thread.quit)
        self._net_worker.failed.connect(self._net_thread.quit)

        def _cleanup():
            self._net_thread = None
            self._net_worker = None
        self._net_thread.finished.connect(_cleanup)
        self._net_thread.start()

    def _on_net_error(self, msg: str) -> None:
        self._update_count()
        QMessageBox.warning(
            self, "Modrinth",
            f"Could not complete the request:\n{msg}\n\n"
            "Check your internet connection and try again.")

    def _update_mod(self, mod: ModEntry) -> None:
        inst = getattr(self, "_instance", None)
        if inst is None:
            return
        from ...mods import modrinth
        loader = inst.loader or "vanilla"
        mc = inst.minecraft_version or ""
        path = inst.path
        filename = mod.filename
        title = mod.display_name

        def work():
            new = modrinth.check_update(path, filename, loader=loader, mc_version=mc)
            if new is None:
                return ("none", None)
            return ("done", modrinth.apply_update(path, filename, new))

        def done(result):
            self._update_count()
            kind, res = result
            if kind == "none":
                QMessageBox.information(
                    self, "Update", f"{title} is already up to date.")
            else:
                QMessageBox.information(
                    self, "Update",
                    f"Updated {title}.\n\n{res.summary()}\n\n"
                    "Restart the server for changes to take effect.")
                self.refresh()

        self._run_net(work, done, busy=f"Checking for updates to {title}\u2026")

    def _verify_deps(self) -> None:
        inst = getattr(self, "_instance", None)
        if inst is None:
            return
        from ...mods import modrinth
        loader = inst.loader or "vanilla"
        mc = inst.minecraft_version or ""
        path = inst.path

        def work():
            missing, notes = modrinth.verify_dependencies(
                path, loader=loader, mc_version=mc)
            installed = modrinth.install_files(path, missing) if missing else None
            return (missing, notes, installed)

        def done(result):
            self._update_count()
            missing, notes, installed = result
            lines = []
            if not missing:
                lines.append("All required dependencies are present. \u2713")
            else:
                lines.append(
                    f"Installed {len(missing)} missing dependency(ies):")
                lines.extend(f"  \u2022 {m.filename}" for m in missing)
                if installed is not None:
                    lines.append("")
                    lines.append(installed.summary())
            if notes:
                lines.append("")
                lines.append("Notes:")
                lines.extend(f"  \u2022 {n}" for n in notes)
            QMessageBox.information(
                self, "Verify dependencies", "\n".join(lines))
            if missing:
                self.refresh()

        self._run_net(work, done, busy="Verifying dependencies\u2026")

    def _open_mod_page(self, mod: ModEntry) -> None:
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        from urllib.parse import quote
        url = getattr(mod, "url", "") or ""
        if not url:
            term = getattr(mod, "name", "") or mod.filename
            url = "https://modrinth.com/mods?q=" + quote(term)
        QDesktopServices.openUrl(QUrl(url))

    # Add from file / drag-drop

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

    def has_active_operation(self) -> bool:
        return bool(self._net_thread and self._net_thread.isRunning())

    # Background jar inspection

    def _start_inspect_pass(self) -> None:
        """Inspect current rows; stale worker results can never retarget a new table."""
        if self._manager is None or not self._mods:
            return
        if self._thread is not None and self._thread.isRunning():
            self._inspect_pending = True
            return
        jobs = [
            (i, mod) for i, mod in enumerate(self._mods)
            if not mod.name and not mod.version
        ]
        if not jobs:
            return
        generation = self._inspect_generation
        thread = QThread()
        worker = _InspectWorker(self._manager, jobs, generation)
        self._thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_inspect_result)

        def _cleanup():
            if self._thread is thread:
                self._thread = None
                self._worker = None
            pending = self._inspect_pending
            self._inspect_pending = False
            if pending:
                self._start_inspect_pass()
        thread.finished.connect(_cleanup)
        thread.start()

    def _on_inspect_result(self, generation: int, row: int, mod: ModEntry) -> None:
        if generation != self._inspect_generation:
            return
        if row < self._table.rowCount():
            name_item = self._table.item(row, self._COL_NAME)
            ver_item  = self._table.item(row, self._COL_VERSION)
            if name_item and mod.name:
                name_item.setText(mod.name)
                name_item.setToolTip(f"{mod.filename}\n{mod.description}")
            if ver_item and mod.version:
                ver_item.setText(mod.version)
                ver_item.setForeground(Qt.GlobalColor.white)
