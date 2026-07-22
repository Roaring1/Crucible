"""
crucible/ui/add_dialog.py

Dialog for adding a new server instance.

Layout favours the *recommended* path — importing a Prism instance or modpack
archive — with manual folder registration available below for advanced users.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QDialogButtonBox, QMessageBox, QCheckBox, QFrame, QInputDialog,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from ..importers.prism import import_prism_source
from . import theme


class _PrismImportWorker(QObject):
    finished = pyqtSignal(object, str)

    def __init__(self, source: str, target: str, download_mods: bool):
        super().__init__()
        self._source = source
        self._target = target
        self._download_mods = download_mods

    def run(self) -> None:
        try:
            info = import_prism_source(
                self._source,
                self._target,
                overwrite=True,
                download_mods=self._download_mods,
            )
            self.finished.emit(info, "")
        except Exception as exc:
            self.finished.emit(None, str(exc))


class AddInstanceDialog(QDialog):
    """
    Modal dialog that lets the user import or register a server directory.

    On accept(), the instance is added to the manager and available
    via .result_instance.
    """

    def __init__(self, manager: InstanceManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.result_instance: ServerInstance | None = None
        self._import_thread: QThread | None = None
        self._import_worker: _PrismImportWorker | None = None
        self._import_pending: tuple[str, str, bool] | None = None
        self._import_result: tuple[object, str] | None = None
        self.setWindowTitle("Add Server Instance")
        self.setMinimumWidth(560)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 16, 20, 16)

        # ---- Easiest: create a brand-new server -------------------------
        new_title = QLabel("Create a new server  —  easiest")
        new_title.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT};"
        )
        layout.addWidget(new_title)

        new_sub = QLabel(
            "No modpack? Spin up a fresh server in seconds. Vanilla is the simplest "
            "— just pick a Minecraft version and go. Fabric, Forge, NeoForge and "
            "Quilt are available too."
        )
        new_sub.setWordWrap(True)
        new_sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(new_sub)

        new_btn = QPushButton("✨  Create a new server…")
        new_btn.setObjectName("PrimaryButton")
        new_btn.setFixedHeight(34)
        new_btn.clicked.connect(self._create_new_server)
        layout.addWidget(new_btn)

        sep0 = QFrame()
        sep0.setFrameShape(QFrame.Shape.HLine)
        sep0.setStyleSheet(f"color: {theme.SURFACE1};")
        layout.addWidget(sep0)

        # ---- Recommended: import from Prism / modpack -------------------
        rec_title = QLabel("Import a modpack  —  recommended")
        rec_title.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT};"
        )
        layout.addWidget(rec_title)

        rec_sub = QLabel(
            "The easiest way to get started. Point Crucible at a Prism Launcher "
            "instance or a modpack archive (.zip / .mrpack from Modrinth or "
            "CurseForge) and it will set up a server folder for you — copying only "
            "server-safe files and generating a start script."
        )
        rec_sub.setWordWrap(True)
        rec_sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(rec_sub)

        import_row = QHBoxLayout()
        import_folder_btn = QPushButton("📦  Import Prism Instance…")
        import_folder_btn.setObjectName("PrimaryButton")
        import_folder_btn.setFixedHeight(34)
        import_folder_btn.clicked.connect(self._import_prism_folder)
        import_row.addWidget(import_folder_btn)
        import_archive_btn = QPushButton("🗃  Import Modpack Archive…")
        import_archive_btn.setObjectName("PrimaryButton")
        import_archive_btn.setFixedHeight(34)
        import_archive_btn.clicked.connect(self._import_prism_archive)
        import_row.addWidget(import_archive_btn)
        layout.addLayout(import_row)

        self._dl_check = QCheckBox(
            "After importing, try to download the pack's mods (needs internet)"
        )
        self._dl_check.setChecked(False)
        self._dl_check.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        self._dl_check.setToolTip(
            "Some packs only ship a list of mods, not the files. If checked, "
            "Crucible will try to download them after import. CurseForge packs "
            "may need a free API key (CURSEFORGE_API_KEY)."
        )
        layout.addWidget(self._dl_check)

        import_hint = QLabel(
            "Tip: importing a *fully installed* Prism instance is the most reliable "
            "option — it already contains the mod jars and the server loader."
        )
        import_hint.setWordWrap(True)
        import_hint.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 11px;")
        layout.addWidget(import_hint)

        # ---- Divider ----------------------------------------------------
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.SURFACE1};")
        layout.addWidget(sep)

        # ---- Advanced: register an existing folder ----------------------
        adv_title = QLabel("Or register an existing server folder")
        adv_title.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {theme.TEXT};"
        )
        layout.addWidget(adv_title)

        adv_sub = QLabel(
            "Already have a server set up? Point Crucible at it. "
            "Files on disk are never modified by this operation."
        )
        adv_sub.setWordWrap(True)
        adv_sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(adv_sub)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addLayout(form)

        # Path field
        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("/path/to/my-server")
        self._path_edit.textChanged.connect(self._auto_fill_name)
        path_row.addWidget(self._path_edit, stretch=1)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)

        form.addRow("Server path:", path_row)

        # Name field
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("My Server")
        form.addRow("Display name:", self._name_edit)

        self._ver_edit = QLineEdit()
        self._ver_edit.setPlaceholderText("e.g. 2.8.4  (optional)")
        form.addRow("Version:", self._ver_edit)

        # Session field
        self._session_edit = QLineEdit()
        self._session_edit.setPlaceholderText("auto-derived from name  (e.g. my-server)")
        form.addRow("tmux session:", self._session_edit)

        hint = QLabel(
            "Leave session blank to auto-derive from the name.  "
            "If a tmux session already exists (e.g. 'gtnh'), enter it here to match."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(hint)

        # Validation warning area
        self._warn_label = QLabel("")
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet(
            f"color: {theme.YELLOW}; font-size: 12px; background: {theme.SURFACE0}; "
            f"border-radius: 4px; padding: 6px;"
        )
        self._warn_label.hide()
        layout.addWidget(self._warn_label)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # Helpers

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Server Directory", str(Path.home())
        )
        if path:
            self._path_edit.setText(path)

    def _auto_fill_name(self, text: str) -> None:
        """If name is empty/auto, fill it with the directory name."""
        if not self._name_edit.text():
            self._name_edit.setText(Path(text).name)

    def _choose_prism_target(self, suggested_name: str) -> str:
        """Let the user pick a parent folder and name a NEW destination folder.

        getExistingDirectory can't create folders easily, so we ask for a parent
        directory and then a folder name, and create it ourselves.
        """
        base = Path.home() / "CrucibleServers"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            base = Path.home()

        parent = QFileDialog.getExistingDirectory(
            self,
            "Choose where to create the new server folder",
            str(base),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not parent:
            return ""

        name, ok = QInputDialog.getText(
            self, "Name the server folder",
            "Folder name for the new server:",
            text=self._safe_folder_name(suggested_name),
        )
        if not ok:
            return ""
        name = self._safe_folder_name(name) or self._safe_folder_name(suggested_name) or "server"
        target = Path(parent) / name

        if target.exists() and any(target.iterdir()):
            resp = QMessageBox.question(
                self, "Folder not empty",
                f"{target} already exists and is not empty.\n\n"
                "Import into it anyway? Existing files may be overwritten.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return ""
        return str(target)

    @staticmethod
    def _safe_folder_name(name: str) -> str:
        keep = "-_. "
        cleaned = "".join(c for c in (name or "") if c.isalnum() or c in keep).strip()
        return cleaned.replace(" ", "-")

    def begin_import_source(self, source: str) -> None:
        """Public drag/drop entry point; prompts for destination then imports."""
        self._finish_prism_import(source)

    def _finish_prism_import(self, source: str) -> None:
        if self._import_thread is not None and self._import_thread.isRunning():
            return
        suggested = Path(source).stem or Path(source).name or "Prism Server"
        target = self._choose_prism_target(suggested)
        if not target:
            return

        want_download = self._dl_check.isChecked()
        self._import_pending = (source, target, want_download)
        self.setEnabled(False)
        self._warn_label.setText(
            "Importing… Large packs can take a while. This window will stay open "
            "until file copying finishes safely."
        )
        self._warn_label.show()

        self._import_thread = QThread()
        self._import_worker = _PrismImportWorker(source, target, want_download)
        self._import_worker.moveToThread(self._import_thread)
        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.finished.connect(self._on_prism_imported)
        self._import_worker.finished.connect(self._import_thread.quit)
        self._import_worker.finished.connect(self._import_worker.deleteLater)
        self._import_thread.finished.connect(self._import_thread.deleteLater)
        self._import_thread.finished.connect(self._import_thread_finished)
        self._import_thread.start()

    @pyqtSlot(object, str)
    def _on_prism_imported(self, info, error: str) -> None:
        self._import_result = (info, error)

    def _complete_prism_import(self, info, error: str) -> None:
        pending = self._import_pending
        if error or info is None or pending is None:
            self._warn_label.hide()
            QMessageBox.critical(self, "Prism Import Failed", error or "Import failed")
            return
        source, target, want_download = pending
        try:
            name = info.name or Path(target).name
            inst = self._manager.add_instance(
                target,
                name,
                info.version_label,
                pack_source=info.source_type,
                minecraft_version=info.minecraft_version,
                loader=info.loader,
                loader_version=info.loader_version,
                prism_source=source,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Prism Import Failed", str(exc))
            return

        self._path_edit.setText(target)
        self._name_edit.setText(inst.name)
        self._ver_edit.setText(inst.version)
        self.result_instance = inst
        if not want_download:
            self._maybe_offer_download(target, inst.name)

        warnings = info.warnings + inst.validate()
        if warnings:
            text = "Import complete, with warnings:\n• " + "\n• ".join(warnings)
        else:
            text = "✓ Import complete. Click OK to add this server to the sidebar."
        self._warn_label.setText(text)
        self._warn_label.show()

    def _import_thread_finished(self) -> None:
        result = self._import_result
        self._import_thread = None
        self._import_worker = None
        self._import_result = None
        self.setEnabled(True)
        if result is not None:
            self._complete_prism_import(*result)
        self._import_pending = None

    def _maybe_offer_download(self, target: str, server_name: str) -> None:
        try:
            from ..importers.downloader import has_downloadable_index
            if not has_downloadable_index(target):
                return
        except Exception:
            return
        resp = QMessageBox.question(
            self, "Download mods?",
            "This pack shipped a mod download list instead of the actual files.\n\n"
            "Try to download the server mods now? (needs internet)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            from .download_dialog import DownloadModsDialog
            dlg = DownloadModsDialog(target, server_name, parent=self)
            dlg.start()
            dlg.exec()
        except Exception as exc:
            QMessageBox.warning(self, "Download mods", f"Could not start download:\n{exc}")

    def _create_new_server(self) -> None:
        try:
            from .new_server_dialog import NewServerDialog
            dlg = NewServerDialog(self._manager, parent=self)
        except Exception as exc:
            QMessageBox.warning(self, "Create server", f"Could not open the creator:\n{exc}")
            return
        if dlg.exec() and dlg.result_instance is not None:
            self.result_instance = dlg.result_instance
            self.accept()

    def _import_prism_folder(self) -> None:
        source = QFileDialog.getExistingDirectory(
            self, "Select Prism instance folder", str(Path.home())
        )
        if source:
            self._finish_prism_import(source)

    def _import_prism_archive(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Select Prism / Modrinth / CurseForge pack",
            str(Path.home()),
            "Modpack archives (*.zip *.mrpack);;All files (*)",
        )
        if source:
            self._finish_prism_import(source)

    def _on_accept(self) -> None:
        # Second OK click after warnings were shown: instance already registered, just close.
        if self.result_instance is not None:
            self.accept()
            return

        path    = self._path_edit.text().strip()
        name    = self._name_edit.text().strip() or Path(path).name
        version = self._ver_edit.text().strip()
        session = self._session_edit.text().strip()

        if not path:
            QMessageBox.warning(
                self, "Nothing to add",
                "Import a modpack above, or enter the path to an existing server folder.",
            )
            return

        try:
            inst = self._manager.add_instance(path, name, version, tmux_session=session)
        except ValueError as exc:
            QMessageBox.warning(self, "Already Registered", str(exc))
            return

        # Show validation warnings but don't block
        problems = inst.validate()
        if problems:
            self._warn_label.setText(
                "⚠  Registered with warnings:\n• " + "\n• ".join(problems)
            )
            self._warn_label.show()
            self.result_instance = inst
            # Don't call accept() yet -- allow user to read warnings.
            # Clicking OK again will hit the early-return guard above and accept.
            return

        self.result_instance = inst
        self.accept()
    def _import_running(self) -> bool:
        return bool(self._import_thread and self._import_thread.isRunning())

    def reject(self) -> None:
        if self._import_running():
            self._warn_label.setText("Import is still copying files — wait for it to finish before closing.")
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._import_running():
            self._warn_label.setText("Import is still copying files — wait for it to finish before closing.")
            event.ignore()
            return
        super().closeEvent(event)
