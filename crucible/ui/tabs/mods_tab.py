"""
crucible/ui/tabs/mods_tab.py

Mods tab: table of enabled/disabled mods with filter, enable/disable toggle,
and add-from-file support (drag-and-drop + file picker).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...data.instance_model import ServerInstance
from ...mods.mod_manager import ModEntry, ModManager
from .. import theme
from ..thread_lifecycle import connect_thread_cleanup

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
    _COL_SIDE    = 3

    def __init__(self, instance_manager=None, parent=None):
        super().__init__(parent)
        # The registry manager (optional): only needed so a pack update can
        # write the new version back. Distinct from self._manager, which is
        # this tab's ModManager.
        self._instance_manager = instance_manager
        self._manager: ModManager | None  = None
        self._mods:    list[ModEntry]     = []
        self._thread:  QThread | None     = None
        self._worker:  _InspectWorker | None = None
        self._net_thread: QThread | None     = None
        self._net_worker: _NetWorker | None  = None
        self._inspect_generation = 0
        self._inspect_pending = False
        self._added_mod_snapshot: dict[str, dict] = {}
        # Keep direct identity-to-item lookup for asynchronous metadata results.
        # QTableWidgetItem.row() tracks sorting, avoiding a full table scan for
        # every inspected JAR while preserving object-identity checks.
        self._mod_name_items: dict[int, QTableWidgetItem] = {}

        self._build_ui()
        self.setAcceptDrops(True)

    # UI

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)
        scroll.setWidget(content)
        root_layout.addWidget(scroll)

        # Search and add are the top-level inventory controls.
        add_row = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setAccessibleName("Filter installed mods by name, ID or filename")
        self._filter.setPlaceholderText("Filter installed mods by name, ID, or filename…")
        self._filter.textChanged.connect(self._apply_filter)
        add_row.addWidget(self._filter, stretch=1)

        add_btn = QPushButton("Add local JAR…")
        add_btn.setAccessibleName("Add a local JAR to the selected server")
        add_btn.setToolTip("Copy one local .jar file into this server's mods folder. Does not search online.")
        add_btn.clicked.connect(self._pick_file)
        add_row.addWidget(add_btn)

        addmod_btn = QPushButton("Find a mod…")
        addmod_btn.setAccessibleName("Find and install a compatible mod online")
        addmod_btn.setToolTip(
            "Search the configured mod provider and install a compatible release with required dependencies."
        )
        addmod_btn.clicked.connect(self._open_add_mod)
        add_row.addWidget(addmod_btn)
        layout.addLayout(add_row)

        # Actions are grouped by scope. Split the groups across two readable
        # rows instead of squeezing three headings and their buttons together.
        from PyQt6.QtWidgets import QGroupBox
        inventory_row = QHBoxLayout()
        inventory_row.setSpacing(8)

        inventory_group = QGroupBox("Installed mod inventory")
        inventory_group.setAccessibleName("Installed mod inventory actions")
        inventory_layout = QHBoxLayout(inventory_group)
        inventory_layout.setContentsMargins(10, 22, 10, 8)
        inventory_layout.setSpacing(6)

        identify_btn = QPushButton("Identify installed mods")
        identify_btn.setAccessibleName("Identify installed mods using exact provider file matches")
        identify_btn.setToolTip(
            "Hash and identify all installed manageable JARs against exact online file records. "
            "Unmatched custom builds are left unlinked; no name-only matching."
        )
        identify_btn.clicked.connect(self._identify_online)
        inventory_layout.addWidget(identify_btn)

        verify_btn = QPushButton("Verify dependencies")
        verify_btn.setAccessibleName("Verify dependencies of installed mods")
        verify_btn.setToolTip(
            "Check dependencies of already-linked installed mods. May download missing dependencies; "
            "the server must be stopped."
        )
        verify_btn.clicked.connect(self._verify_deps)
        inventory_layout.addWidget(verify_btn)

        refresh_btn = QPushButton("Refresh mod list")
        refresh_btn.setAccessibleName("Refresh installed mod inventory")
        refresh_btn.setToolTip("Re-read files and JAR metadata. Does not change mod files.")
        refresh_btn.clicked.connect(self.refresh)
        inventory_layout.addWidget(refresh_btn)
        inventory_row.addWidget(inventory_group, 3)

        compatibility_group = QGroupBox("Server compatibility")
        compatibility_group.setAccessibleName("Selected server compatibility")
        compatibility_layout = QHBoxLayout(compatibility_group)
        compatibility_layout.setContentsMargins(10, 22, 10, 8)
        platform_btn = QPushButton("Set server version…")
        platform_btn.setAccessibleName("Describe the selected server Minecraft and loader versions")
        platform_btn.setToolTip(
            "Describe the Minecraft and loader versions already installed. This does not upgrade Minecraft."
        )
        platform_btn.clicked.connect(self._edit_platform)
        compatibility_layout.addWidget(platform_btn)
        inventory_row.addWidget(compatibility_group, 1)
        layout.addLayout(inventory_row)

        pack_group = QGroupBox("Whole server pack")
        pack_group.setAccessibleName("Whole server pack actions")
        pack_layout = QHBoxLayout(pack_group)
        pack_layout.setContentsMargins(10, 22, 10, 8)
        pack_layout.setSpacing(6)

        client_btn = QPushButton("Share client pack…")
        client_btn.setAccessibleName("Build and share the selected server client pack")
        client_btn.setToolTip(
            "Build and review the client pack for this server, then offer the configured upload flow. "
            "This is not a single-mod action."
        )
        client_btn.clicked.connect(self._open_share_players)
        pack_layout.addWidget(client_btn)

        advanced_btn = QPushButton("Advanced client export…")
        advanced_btn.setAccessibleName("Export the selected server client pack to an archive")
        advanced_btn.setToolTip(
            "Export this server's client pack to a local archive format. Does not change server mods."
        )
        advanced_btn.clicked.connect(self._open_make_client)
        pack_layout.addWidget(advanced_btn)

        update_btn = QPushButton("Update entire modpack…")
        update_btn.setToolTip(
            "Update this server's whole modpack, not the selected mod. Shows a plan before applying changes."
        )
        update_btn.setAccessibleName("Update the entire selected server modpack")
        update_btn.clicked.connect(self._open_pack_update)
        pack_layout.addWidget(update_btn)

        self._dl_btn = QPushButton("Download listed mods…")
        self._dl_btn.setAccessibleName("Download files listed by the selected server pack")
        self._dl_btn.setToolTip(
            "Fetch files listed by this pack's download index. This operates on the whole server pack."
        )
        self._dl_btn.clicked.connect(self._download_from_pack)
        self._dl_btn.hide()
        pack_layout.addWidget(self._dl_btn)
        layout.addWidget(pack_group)

        # Dense, sortable inventory: no file-size column and no repeated delete
        # buttons. Full details and selected-mod actions live below the list.
        self._table = QTableWidget()
        self._table.setAccessibleName("Installed mods for the selected server")
        self._table.setAccessibleDescription(
            "Sortable inventory. Select one row to inspect exact file details and "
            "use the selected-mod actions below. State controls toggle only that mod."
        )
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(
            ["State", "Mod", "Version", "Runs on"]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self._COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_ENABLED, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self._COL_VERSION, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self._COL_SIDE, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(self._COL_ENABLED, 92)
        self._table.setColumnWidth(self._COL_VERSION, 160)
        self._table.setColumnWidth(self._COL_SIDE, 126)
        self._table.setMinimumHeight(180)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._table, stretch=1)

        # Visible, persistent selected-mod details and actions; no RMB required.
        details = QWidget()
        details.setObjectName("SelectedModDetails")
        details.setAccessibleName("Selected mod details and actions")
        details.setStyleSheet(
            f"QWidget#SelectedModDetails {{ background: {theme.MANTLE}; "
            f"border: 1px solid {theme.SURFACE1}; border-radius: 5px; }}"
        )
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(12, 8, 12, 8)
        details_layout.setSpacing(5)

        detail_header = QHBoxLayout()
        self._detail_title = QLabel("Select a mod to inspect it")
        self._detail_title.setAccessibleName("Selected mod title")
        self._detail_title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 13px; font-weight: 700;"
        )
        detail_header.addWidget(self._detail_title, 1)

        self._change_selected_btn = QPushButton("Change selected mod version…")
        self._change_selected_btn.setAccessibleName("Change the selected mod version")
        self._change_selected_btn.setToolTip(
            "Resolve the selected mod's exact online identity and review a compatible replacement. "
            "The original JAR is retained in mod-history."
        )
        self._change_selected_btn.clicked.connect(self._change_selected_version)
        self._change_selected_btn.setEnabled(False)
        detail_header.addWidget(self._change_selected_btn)

        self._remove_selected_btn = QPushButton("Remove selected mod…")
        self._remove_selected_btn.setAccessibleName("Remove only the selected mod")
        self._remove_selected_btn.setObjectName("DangerButton")
        self._remove_selected_btn.setToolTip(
            "Remove only the selected mod JAR. The server must be stopped; this cannot be undone."
        )
        self._remove_selected_btn.clicked.connect(self._remove_selected_mod)
        self._remove_selected_btn.setEnabled(False)
        detail_header.addWidget(self._remove_selected_btn)
        details_layout.addLayout(detail_header)

        self._detail_summary = QLabel(
            "Select one row to see its mod ID, filename, version, side evidence, and file size."
        )
        self._detail_summary.setAccessibleName("Selected mod file details")
        self._detail_summary.setWordWrap(True)
        self._detail_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._detail_summary.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px;"
        )
        details_layout.addWidget(self._detail_summary)
        layout.addWidget(details)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(self._count_label)

        self._drop_hint = QLabel("Drop .jar files here to add mods")
        self._drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setStyleSheet(
            f"color: {theme.SURFACE2}; font-size: 11px; padding: 3px;"
        )
        layout.addWidget(self._drop_hint)

    # Public API

    def _open_add_mod(self) -> None:
        if not self._require_stopped():
            return
        if getattr(self, "_instance", None) is None:
            return
        from ..add_mod_dialog import AddModDialog
        dlg = AddModDialog(self._instance, self)
        dlg.exec()
        self.refresh()

    def _open_share_players(self) -> None:
        if getattr(self, "_instance", None) is None:
            return
        from ..share_players_dialog import SharePlayersDialog
        SharePlayersDialog(
            self._instance, self, manager=self._instance_manager
        ).exec()

    def _open_make_client(self) -> None:
        if getattr(self, "_instance", None) is None:
            return
        from ..client_export_dialog import ClientExportDialog
        ClientExportDialog(self._instance, self, manager=self._instance_manager).exec()

    def load(self, instance: ServerInstance) -> None:
        """Switch to displaying mods for the given instance."""
        self._manager = ModManager(instance)
        self._instance = instance
        # Reveal the download button only when a pack index is present.
        try:
            from ...importers.downloader import has_downloadable_index
            self._dl_btn.setVisible(has_downloadable_index(instance.path))
        except (ImportError, OSError, ValueError, TypeError):
            self._dl_btn.hide()
        self.refresh()

    def _open_pack_update(self) -> None:
        inst = getattr(self, "_instance", None)
        if inst is None:
            return
        try:
            from ..pack_update_dialog import PackUpdateDialog
            dlg = PackUpdateDialog(inst, manager=self._instance_manager, parent=self)
            dlg.start_check()
            dlg.exec()
        except Exception as exc:  # noqa: BLE001 - report dialog construction/start failures without killing the GUI event loop.
            QMessageBox.warning(
                self, "Update pack",
                f"Could not open the modpack updater:\n{exc}"
            )
        # The mod folder is a different folder after a swap.
        self.refresh()

    def _download_from_pack(self) -> None:
        if not self._require_stopped():
            return
        inst = getattr(self, "_instance", None)
        if inst is None:
            return
        try:
            from ..download_dialog import DownloadModsDialog
            dlg = DownloadModsDialog(inst.path, inst.name, parent=self)
            dlg.start()
            dlg.exec()
        except Exception as exc:  # noqa: BLE001 - report dialog construction/start failures without killing the GUI event loop.
            QMessageBox.warning(self, "Download mods", f"Could not start download:\n{exc}")
        self.refresh()

    def refresh(self) -> None:
        if self._manager is None:
            return
        self._inspect_generation += 1
        self._mods = self._manager.list_mods()
        # Worker callbacks and selection changes share this request-scoped
        # identity snapshot. Do not reread added-mods.json once per JAR.
        from ...mods.modrinth import added_mods
        self._added_mod_snapshot = added_mods(self._instance.path)
        self._populate_table(self._mods)
        self._apply_filter(self._filter.text())
        self._update_count()
        self._start_inspect_pass()

    # Table population

    def _populate_table(self, mods: list[ModEntry]) -> None:
        self._table.setSortingEnabled(False)
        self._mod_name_items.clear()
        self._table.setRowCount(0)
        self._table.setRowCount(len(mods))

        for row, mod in enumerate(mods):
            is_bundled = getattr(mod, "bundled", False)

            if is_bundled:
                bundled_label = QLabel("Bundled")
                bundled_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                bundled_label.setToolTip(
                    f"Nested library inside mods/{mod.path.parent.name}/. "
                    "It is managed by its parent modpack and cannot be toggled or removed here."
                )
                bundled_label.setStyleSheet(
                    f"color: {theme.SUBTEXT}; font-size: 10px; font-weight: 600;"
                )
                self._table.setCellWidget(row, self._COL_ENABLED, bundled_label)
            else:
                state_btn = QPushButton("Enabled" if mod.enabled else "Disabled")
                state_btn.setCheckable(True)
                state_btn.setChecked(mod.enabled)
                state_btn.setFixedHeight(25)
                state_btn.setObjectName("ModEnabledButton")
                state_btn.setStyleSheet(
                    f"QPushButton {{ color: {theme.GREEN if mod.enabled else theme.SUBTEXT}; "
                    f"background: {theme.SURFACE0}; border: 1px solid "
                    f"{theme.GREEN if mod.enabled else theme.SURFACE1}; "
                    "border-radius: 4px; padding: 2px 5px; font-size: 10px; font-weight: 600; }"
                    f"QPushButton:checked {{ color: {theme.GREEN}; }}"
                    f"QPushButton#ModEnabledButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
                )
                state_btn.setAccessibleName(
                    f"{'Disable' if mod.enabled else 'Enable'} {mod.display_name}"
                )
                state_btn.setToolTip(
                    f"Click to {'disable' if mod.enabled else 'enable'} this mod. "
                    "Changes require the server to be stopped."
                )
                state_btn.toggled.connect(
                    lambda checked, selected=mod: self._set_enabled(selected, checked)
                )
                self._table.setCellWidget(row, self._COL_ENABLED, state_btn)

            display = mod.display_name
            name_item = QTableWidgetItem(display)
            name_item.setToolTip(f"{mod.filename}\n{mod.path}")
            name_item.setData(Qt.ItemDataRole.UserRole, mod)
            if is_bundled:
                name_item.setForeground(Qt.GlobalColor.darkCyan)
            elif not mod.enabled:
                name_item.setForeground(Qt.GlobalColor.gray)
            self._table.setItem(row, self._COL_NAME, name_item)
            self._mod_name_items[id(mod)] = name_item

            ver_item = QTableWidgetItem(mod.version or "—")
            ver_item.setForeground(Qt.GlobalColor.gray)
            ver_item.setToolTip(mod.version or "Version not declared in JAR metadata.")
            self._table.setItem(row, self._COL_VERSION, ver_item)

            side_item = QTableWidgetItem("Not declared")
            side_item.setToolTip(
                "Execution side is shown only when the JAR explicitly declares it. "
                "Unknown does not imply compatibility."
            )
            self._table.setItem(row, self._COL_SIDE, side_item)
            self._render_side(row, mod)
            self._table.setRowHeight(row, 33)

        self._table.setSortingEnabled(True)
        if self._table.rowCount():
            self._table.selectRow(0)
        self._on_selection_changed()

    def _render_side(self, row, mod):
        item = self._table.item(row, self._COL_SIDE)
        if item:
            label = {
                "client": "Client only",
                "server": "Server only",
                "both": "Both sides",
            }.get(mod.side, "Not declared")
            item.setText(label)
            item.setToolTip(
                "Reported by the JAR's explicit loader metadata. "
                "Not declared means Crucible cannot infer compatibility."
            )
            if mod.side == "client":
                from PyQt6.QtGui import QColor
                item.setForeground(QColor(theme.YELLOW))

    def _apply_filter(self, text: str) -> None:
        text = text.lower().strip()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_NAME)
            if item is None:
                continue
            mod = item.data(Qt.ItemDataRole.UserRole)
            haystack = " ".join((
                item.text(),
                str(getattr(mod, "mod_id", "")),
                str(getattr(mod, "filename", "")),
                str(getattr(mod, "version", "")),
            )).lower()
            self._table.setRowHidden(row, bool(text) and text not in haystack)

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
        """Compatibility wrapper for callers that still supply a row index."""
        self._set_enabled(mod, enable)

    def _set_enabled(self, mod: ModEntry, enable: bool) -> None:
        if not self._require_stopped():
            self.refresh()
            return
        if self._manager is None or getattr(mod, "bundled", False):
            return
        try:
            if enable:
                self._manager.enable(mod)
            else:
                self._manager.disable(mod)
            selected_name = mod.filename
            self.refresh()
            for row in range(self._table.rowCount()):
                item = self._table.item(row, self._COL_NAME)
                current = item.data(Qt.ItemDataRole.UserRole) if item else None
                if current is not None and current.filename == selected_name:
                    self._table.selectRow(row)
                    break
        except OSError as exc:
            QMessageBox.critical(self, "Mod state", f"Could not change mod state:\n{exc}")
            self.refresh()

    def _delete_mod(self, mod: ModEntry, _row: int) -> None:
        if not self._require_stopped():
            return
        if self._manager is None:
            return
        reply = QMessageBox.question(
            self,
            "Remove selected mod",
            f"Remove this JAR from the server's mods folder?\n\n{mod.filename}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self._manager.delete(mod)
                self.refresh()
            except OSError as exc:
                QMessageBox.critical(self, "Remove failed", f"Could not remove the selected JAR:\n{exc}")

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

        menu = QMenu(self)
        # Essential operations are already directly visible: enabled state is
        # a named cell control, version replacement/removal live in the
        # selected-mod detail panel, and dependency checks are in the inventory
        # action group. Keep the context menu to the one contextual browse action.
        menu.addAction("Open on Modrinth").triggered.connect(
            lambda: self._open_mod_page(mod))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _set_enabled(self, mod: ModEntry, enable: bool) -> None:
        if not self._require_stopped():
            return
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

    def _edit_platform(self):
        if self._instance_manager is None or not self._require_stopped():
            return
        from PyQt6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout
        inst = self._instance
        dlg = QDialog(self)
        dlg.setWindowTitle("Server version · compatibility filters")
        form = QFormLayout(dlg)
        note = QLabel("These fields describe the files already installed. They do not upgrade Minecraft or convert worlds.")
        note.setWordWrap(True)
        form.addRow(note)
        mc = QLineEdit(inst.minecraft_version or "")
        loader = QComboBox()
        loader.addItems(["fabric", "forge", "neoforge", "quilt", "vanilla"])
        loader.setCurrentText(inst.loader or "vanilla")
        version = QLineEdit(inst.loader_version or "")
        form.addRow("Minecraft version", mc)
        form.addRow("Loader", loader)
        form.addRow("Exact loader version", version)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if not dlg.exec():
            return
        if not mc.text().strip():
            QMessageBox.warning(self, "Minecraft version required", "Enter the version actually installed, such as 1.21.1.")
            return
        old = (inst.minecraft_version, inst.loader, inst.loader_version)
        inst.minecraft_version, inst.loader, inst.loader_version = mc.text().strip(), loader.currentText(), version.text().strip()
        try:
            self._instance_manager.update_instance(inst)
        except Exception as exc:
            inst.minecraft_version, inst.loader, inst.loader_version = old
            QMessageBox.warning(self, "Could not save server version", str(exc))

    def _selected_mod(self):
        row = self._table.currentRow()
        item = self._table.item(row, self._COL_NAME) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_selection_changed(self) -> None:
        mod = self._selected_mod()
        if mod is None:
            self._detail_title.setText("Select a mod to inspect it")
            self._detail_summary.setText(
                "Select one row to see its mod ID, filename, version, side evidence, and file size."
            )
            self._change_selected_btn.setEnabled(False)
            self._remove_selected_btn.setEnabled(False)
            return

        bundled = bool(getattr(mod, "bundled", False))
        self._detail_title.setText(mod.display_name)
        side = {
            "client": "Client only",
            "server": "Server only",
            "both": "Both sides",
        }.get(mod.side, "Not declared")
        identity = "not linked to an exact online project"
        record = self._added_mod_snapshot.get(mod.filename, {})
        if record.get("project_id"):
            identity = f"linked online as {record.get('project_id')}"
        self._detail_summary.setText(
            f"Mod ID: {mod.mod_id or 'not declared'}    ·    "
            f"Version: {mod.version or 'not declared'}    ·    "
            f"Runs on: {side}    ·    Size: {mod.size_mb}\n"
            f"File: {mod.filename}    ·    {identity}\n"
            f"Location: {mod.path}"
        )
        self._detail_summary.setToolTip(
            f"{mod.description}\n{mod.path}" if mod.description else str(mod.path)
        )
        self._change_selected_btn.setEnabled(not bundled)
        self._remove_selected_btn.setEnabled(not bundled)

    def _change_selected_version(self):
        mod = self._selected_mod()
        if mod is None:
            QMessageBox.information(
                self, "Select a mod", "Select one manageable mod in the inventory first."
            )
            return
        if mod.bundled:
            QMessageBox.information(
                self, "Bundled library",
                "This nested library is managed by its parent modpack."
            )
            return
        self._change_version(mod)

    def _remove_selected_mod(self) -> None:
        mod = self._selected_mod()
        row = self._table.currentRow()
        if mod is None:
            QMessageBox.information(
                self, "Select a mod", "Select one manageable mod in the inventory first."
            )
            return
        if getattr(mod, "bundled", False):
            QMessageBox.information(
                self, "Bundled library",
                "This nested library is managed by its parent modpack."
            )
            return
        self._delete_mod(mod, row)

    def _require_stopped(self):
        from ...process.tmux_manager import TmuxManager
        inst = getattr(self, "_instance", None)
        if inst is None:
            return False
        status = TmuxManager().get_status(inst)
        if status != "stopped":
            QMessageBox.warning(self, "Stop the server first", "Mod changes require a confirmed stopped server. Current status: " + status)
            return False
        return True

    def _identify_online(self):
        inst = getattr(self, "_instance", None)
        if inst is None:
            return
        from ...mods.versions import identify_files
        paths = [m.path for m in self._mods if not m.bundled]
        path = inst.path
        def done(found):
            QMessageBox.information(self, "Online identity", f"{len(found)} exact file match(es). Custom builds without an exact match remain unlinked; name similarity is not proof of identity.")
            self.refresh()
        self._run_net(lambda: identify_files(path, paths), done, busy="Matching file hashes…")

    def _change_version(self, mod):
        if not self._require_stopped():
            return
        from ...mods import modrinth
        from ...mods.versions import (
            identify_files,
            list_versions,
            replace_files,
            resolve_selected,
        )
        inst = self._instance
        path, loader, mc = inst.path, inst.loader, inst.minecraft_version
        def work():
            record = modrinth.added_mods(path)
            meta = record.get(mod.filename) or record.get(mod.filename.removesuffix(".disabled"))
            if not meta:
                meta = identify_files(path, [mod.path]).get(mod.filename)
            if not meta:
                return []
            return list_versions(meta["project_id"], loader=loader, mc_version=mc)
        def choose(options):
            if not options:
                QMessageBox.information(self, "No compatible releases", "No exact online identity or compatible release was found. Custom builds are never replaced with a similarly named project. Use Add .jar for a manually obtained version.")
                return
            labels = [f"{v.version_number}  ·  {v.filename}  ·  {v.version_id}" for v in options]
            selected, ok = QInputDialog.getItem(self, "Change version · upgrades & downgrades", "Compatible releases (newest first):", labels, 0, False)
            if not ok:
                return
            chosen = options[labels.index(selected)]
            def preview():
                return resolve_selected(chosen, loader=loader, mc_version=mc)
            def confirm(files):
                text = "Replace " + mod.filename + " with:\n\n" + "\n".join("• " + f.filename for f in files) + "\n\nRequired dependencies are included. Originals are saved under .crucible/mod-history. Disabled mods stay disabled. Back up your world before changing content mods; this action does not migrate worlds."
                if QMessageBox.question(self, "Review mod change", text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                    return
                if not self._require_stopped():
                    return
                def install():
                    from ...process.tmux_manager import TmuxManager
                    if TmuxManager().get_status(inst) != "stopped":
                        raise RuntimeError("Server status changed; no mod files were changed.")
                    def guard():
                        if TmuxManager().get_status(inst) != "stopped":
                            raise RuntimeError("Server started while downloading. Original mods were kept.")
                    return replace_files(path, mod.filename, files, before_commit=guard)
                def finished(result):
                    if result.failed:
                        QMessageBox.warning(self, "Mod change not applied", "\n".join(result.failed))
                    else:
                        QMessageBox.information(self, "Mod version changed", result.summary() + "\nOriginals retained in .crucible/mod-history.")
                    self.refresh()
                self._run_net(install, finished, busy="Verifying and replacing mod files…")
            self._run_net(preview, confirm, busy="Resolving exact dependencies…")
        self._run_net(work, choose, busy="Finding compatible versions…")

    # Network-backed actions (run off the UI thread)

    def _run_net(self, fn, on_done, busy: str = "Working…") -> None:
        if self._net_thread is not None and self._net_thread.isRunning():
            QMessageBox.information(
                self, "Please wait",
                "Another Modrinth action is still running. Try again in a moment.")
            return
        self._count_label.setText(busy)
        self._net_thread = QThread()
        self._net_worker = _NetWorker(fn)
        self._net_worker.moveToThread(self._net_thread)
        self._net_thread.finished.connect(self._net_worker.deleteLater)
        self._net_thread.started.connect(self._net_worker.run)
        outcome = []
        self._net_worker.done.connect(lambda result: outcome.append(result))
        self._net_worker.failed.connect(self._on_net_error)
        self._net_worker.done.connect(self._net_thread.quit)
        self._net_worker.failed.connect(self._net_thread.quit)

        def _cleanup():
            self._net_thread = None
            self._net_worker = None
            if outcome:
                on_done(outcome[0])
        connect_thread_cleanup(self._net_thread, _cleanup)
        self._net_thread.start()

    def _on_net_error(self, msg: str) -> None:
        self._update_count()
        QMessageBox.warning(
            self, "Modrinth",
            f"Could not complete the request:\n{msg}\n\n"
            "Check your internet connection and try again.")

    def _update_mod(self, mod: ModEntry) -> None:
        self._change_version(mod)

    def _verify_deps(self) -> None:
        if not self._require_stopped():
            return
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
                lines.append("No missing dependencies were found among linked mods. Unlinked/custom mods are not verified.")
            else:
                lines.append(
                    f"Installed {len(missing)} missing dependency(ies):")
                lines.extend(f"  • {m.filename}" for m in missing)
                if installed is not None:
                    lines.append("")
                    lines.append(installed.summary())
            if notes:
                lines.append("")
                lines.append("Notes:")
                lines.extend(f"  • {n}" for n in notes)
            QMessageBox.information(
                self, "Verify dependencies", "\n".join(lines))
            if missing:
                self.refresh()

        self._run_net(work, done, busy="Verifying dependencies…")

    def _open_mod_page(self, mod: ModEntry) -> None:
        from urllib.parse import quote

        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        url = getattr(mod, "url", "") or ""
        if not url:
            term = getattr(mod, "name", "") or mod.filename
            url = "https://modrinth.com/mods?q=" + quote(term)
        QDesktopServices.openUrl(QUrl(url))

    # Add from file / drag-drop

    def _pick_file(self) -> None:
        if not self._require_stopped():
            return
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
        if not self._require_stopped():
            return
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
        net_busy = bool(self._net_thread and self._net_thread.isRunning())
        inspect_busy = bool(self._thread and self._thread.isRunning())
        return net_busy or inspect_busy

    # Background jar inspection

    @staticmethod
    def _inspection_jobs(mods: list[ModEntry]) -> list[tuple[int, ModEntry]]:
        """Select manageable entries that still need metadata inspection.

        Bundled nested libraries are informational-only rows managed by their
        parent pack. Inspecting every nested dependency adds archive I/O and
        worker signals without enabling any action in Crucible.
        """
        return [
            (row, mod)
            for row, mod in enumerate(mods)
            if not getattr(mod, "bundled", False)
            and not mod.name
            and not mod.version
        ]

    def _start_inspect_pass(self) -> None:
        """Inspect current rows; stale worker results can never retarget a new table."""
        if self._manager is None or not self._mods:
            return
        if self._thread is not None and self._thread.isRunning():
            self._inspect_pending = True
            return
        jobs = self._inspection_jobs(self._mods)
        if not jobs:
            return
        generation = self._inspect_generation
        thread = QThread()
        worker = _InspectWorker(self._manager, jobs, generation)
        self._thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        thread.finished.connect(worker.deleteLater)
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
        connect_thread_cleanup(thread, _cleanup)
        thread.start()

    def _on_inspect_result(self, generation: int, row: int, mod: ModEntry) -> None:
        if generation != self._inspect_generation:
            return
        # The item object remains associated with the mod after sorting, and
        # its row() reports its current position. Verify identity as a guard
        # against a stale/replaced item before applying worker output.
        item = self._mod_name_items.get(id(mod))
        if item is None or item.data(Qt.ItemDataRole.UserRole) is not mod:
            return
        current = item.row()
        if current < 0:
            return
        self._table.setSortingEnabled(False)
        if mod.name:
            item.setText(mod.name)
        meta = self._added_mod_snapshot.get(mod.filename, {})
        identity = (
            "Exact online match" if meta.get("project_id")
            else "Not linked online · use Identify installed mods"
        )
        item.setToolTip(
            f"Filename: {mod.filename}\nMod ID: {mod.mod_id or 'not declared'}"
            f"\n{mod.description}\n{identity}\n{mod.path}"
        )
        version = self._table.item(current, self._COL_VERSION)
        if version and mod.version:
            version.setText(mod.version)
            version.setToolTip(mod.version)
        self._render_side(current, mod)
        self._table.setSortingEnabled(True)
        if self._selected_mod() is mod:
            self._on_selection_changed()
