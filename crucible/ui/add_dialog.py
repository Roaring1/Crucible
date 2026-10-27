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
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QScrollArea, QWidget,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QDialogButtonBox, QMessageBox, QCheckBox, QFrame, QToolButton,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from ..importers.prism import import_prism_source
from . import theme
from .thread_lifecycle import connect_thread_cleanup


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
                overwrite=False,
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
        self._pending_import_registration: tuple[str, str, object] | None = None
        self._pending_manual_registration = False
        self._download_offer: tuple[str, str] | None = None
        self.setWindowTitle("Import existing files")
        # Widened from 560 -- at 560px, the register-existing-server form's
        # "Server path:" row (label + line edit + Browse… button) and the
        # other field rows clipped their text/placeholders and were hard to
        # read.
        self.setMinimumWidth(680)
        self.resize(740, 420)
        self.setMinimumHeight(360)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        layout = QVBoxLayout(body)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 16, 20, 12)

        title = QLabel("Import a server setup")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT};"
        )
        layout.addWidget(title)

        sub = QLabel(
            "Copy an installed Prism instance or a modpack file into a new server "
            "folder. To add a server folder that is already in its final location, "
            "expand the registration option below. Importing does not start the server."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(sub)

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self._import_folder_btn = QPushButton("Import Prism instance…")
        import_folder_btn = self._import_folder_btn
        import_folder_btn.setAccessibleName("Import Prism instance")
        import_folder_btn.setMinimumHeight(34)
        import_folder_btn.setToolTip(
            "Copy an already-installed Prism or MultiMC instance into a server folder."
        )
        import_folder_btn.clicked.connect(self._import_prism_folder)
        source_row.addWidget(import_folder_btn)

        self._import_archive_btn = QPushButton("Import ZIP / .mrpack…")
        import_archive_btn = self._import_archive_btn
        import_archive_btn.setAccessibleName("Import modpack archive")
        import_archive_btn.setMinimumHeight(34)
        import_archive_btn.setToolTip(
            "Copy a Modrinth, CurseForge, or other supported modpack archive into a server folder."
        )
        import_archive_btn.clicked.connect(self._import_prism_archive)
        source_row.addWidget(import_archive_btn)
        layout.addLayout(source_row)

        import_hint = QLabel(
            "Choose a destination parent folder. Crucible names the new folder from "
            "the source and picks a free name if that folder already exists."
        )
        import_hint.setWordWrap(True)
        import_hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(import_hint)

        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setText("Register a server folder already on disk")
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.setChecked(False)
        self._advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._advanced_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._advanced_toggle.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: 600; padding: 5px 0;"
        )
        self._advanced_toggle.toggled.connect(self._toggle_advanced_registration)
        layout.addWidget(self._advanced_toggle)

        self._advanced_body = QWidget()
        self._advanced_body.setVisible(False)
        advanced_layout = QVBoxLayout(self._advanced_body)
        advanced_layout.setContentsMargins(0, 2, 0, 0)
        advanced_layout.setSpacing(8)

        advanced_hint = QLabel(
            "Use this only when the server files are already where you want them. "
            "Crucible records the folder; it does not copy, move, or delete its files."
        )
        advanced_hint.setWordWrap(True)
        advanced_hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        advanced_layout.addWidget(advanced_hint)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        advanced_layout.addLayout(form)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("/path/to/my-server")
        self._path_edit.setMinimumWidth(320)
        self._path_edit.setMinimumHeight(32)
        self._path_edit.textChanged.connect(self._auto_fill_name)
        path_row.addWidget(self._path_edit, stretch=1)

        browse_btn = QPushButton("Browse…")
        browse_btn.setMinimumWidth(80)
        browse_btn.setMinimumHeight(32)
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        form.addRow("Server folder:", path_row)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("My server")
        self._name_edit.setMinimumWidth(320)
        self._name_edit.setMinimumHeight(32)
        form.addRow("Display name:", self._name_edit)

        self._ver_edit = QLineEdit()
        self._ver_edit.setPlaceholderText("Version (optional)")
        self._ver_edit.setMinimumWidth(320)
        self._ver_edit.setMinimumHeight(32)
        form.addRow("Version:", self._ver_edit)

        self._session_edit = QLineEdit()
        self._session_edit.setPlaceholderText("Leave blank to use the server name")
        self._session_edit.setMinimumWidth(320)
        self._session_edit.setMinimumHeight(32)
        form.addRow("tmux session (optional):", self._session_edit)

        session_hint = QLabel(
            "Most users should leave the session blank. Enter a name only if this "
            "server already runs in a specifically named tmux session."
        )
        session_hint.setWordWrap(True)
        session_hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        advanced_layout.addWidget(session_hint)
        layout.addWidget(self._advanced_body)

        self._warn_label = QLabel("")
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet(
            f"color: {theme.YELLOW}; font-size: 12px; background: {theme.SURFACE0}; "
            f"border-radius: 4px; padding: 6px;"
        )
        self._warn_label.hide()
        layout.addWidget(self._warn_label)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._add_button = btns.button(QDialogButtonBox.StandardButton.Ok)
        self._add_button.setText("Add server")
        self._add_button.setObjectName("PrimaryButton")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _toggle_advanced_registration(self, expanded: bool) -> None:
        self._advanced_body.setVisible(expanded)
        self._advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.adjustSize()

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
        """Choose the parent folder and derive a safe, collision-free destination."""
        base = Path.home() / "CrucibleServers"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            base = Path.home()

        parent = QFileDialog.getExistingDirectory(
            self,
            "Choose where to create the server folder",
            str(base),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not parent:
            return ""

        name = self._safe_folder_name(suggested_name) or "server"
        target = Path(parent) / name
        suffix = 2
        while target.exists():
            target = Path(parent) / f"{name}-{suffix}"
            suffix += 1
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
        if self._pending_import_registration is not None:
            self._warn_label.setText(
                "An import is already ready. Click Add server to finish it, or Cancel "
                "before starting another import. Copied files are left on disk if you cancel."
            )
            self._warn_label.show()
            return
        suggested = Path(source).stem or Path(source).name or "Prism Server"
        target = self._choose_prism_target(suggested)
        if not target:
            return

        # Download prompting happens after this import window closes.
        want_download = False
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
        connect_thread_cleanup(self._import_thread, self._import_thread_finished)
        self._import_thread.start()

    @pyqtSlot(object, str)
    def _on_prism_imported(self, info, error: str) -> None:
        self._import_result = (info, error)

    def _complete_prism_import(self, info, error: str) -> None:
        pending = self._import_pending
        if error or info is None or pending is None:
            self._warn_label.hide()
            QMessageBox.critical(
                self, "Could not import server", error or "Import failed"
            )
            return

        source, target, want_download = pending
        name = Path(target).name or info.name
        candidate = ServerInstance(
            path=target,
            name=name,
            version=info.version_label,
            pack_source=info.source_type,
            minecraft_version=info.minecraft_version,
            loader=info.loader,
            loader_version=info.loader_version,
            prism_source=source,
        )
        self._pending_import_registration = (source, target, info)
        self._import_folder_btn.setEnabled(False)
        self._import_archive_btn.setEnabled(False)
        self._advanced_toggle.setEnabled(False)
        self._path_edit.setText(target)
        self._name_edit.setText(candidate.name)
        self._ver_edit.setText(candidate.version)
        self.result_instance = candidate
        if not want_download:
            self._download_offer = (target, candidate.name)

        warnings = info.warnings + candidate.validate()
        destination_note = (
            f"Files copied to:\n{target}\n\n"
            "Click Add server to track this folder. Cancel leaves the copied files on disk.\n\n"
        )
        if warnings:
            text = destination_note + "Import ready, with warnings:\n• " + "\n• ".join(warnings)
        else:
            text = destination_note + "Import complete."
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
        host = self.parentWidget() or self
        resp = QMessageBox.question(
            host, "Download mods?",
            "This pack shipped a mod download list instead of the actual files.\n\n"
            "Try to download the server mods now? (needs internet)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            from .download_dialog import DownloadModsDialog
            dlg = DownloadModsDialog(target, server_name, parent=host)
            dlg.start()
            dlg.exec()
        except Exception as exc:
            QMessageBox.warning(self, "Download mods", f"Could not start download:\n{exc}")

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
        # Imported pack files are already copied; only register them when the
        # user chooses Add server. Cancel leaves those files untouched on disk.
        if self._pending_import_registration is not None:
            source, target, info = self._pending_import_registration
            try:
                inst = self._manager.add_instance(
                    target,
                    Path(target).name or info.name,
                    info.version_label,
                    pack_source=info.source_type,
                    minecraft_version=info.minecraft_version,
                    loader=info.loader,
                    loader_version=info.loader_version,
                    prism_source=source,
                )
            except Exception as exc:
                QMessageBox.warning(self, "Could not add server", str(exc))
                return
            self.result_instance = inst
            self._pending_import_registration = None
            self.accept()
            return

        # A manual folder with validation warnings needs an explicit second
        # click, but it is not written to the registry until that click.
        if self._pending_manual_registration and self.result_instance is not None:
            path = self._path_edit.text().strip()
            name = self._name_edit.text().strip() or Path(path).name
            version = self._ver_edit.text().strip()
            session = self._session_edit.text().strip()
            try:
                inst = self._manager.add_instance(
                    path, name, version, tmux_session=session
                )
            except ValueError as exc:
                QMessageBox.warning(self, "Could not add server", str(exc))
                return
            self.result_instance = inst
            self._pending_manual_registration = False
            self._add_button.setText("Add server")
            self.accept()
            return

        if self.result_instance is not None:
            self.accept()
            return

        path = self._path_edit.text().strip()
        name = self._name_edit.text().strip() or Path(path).name
        version = self._ver_edit.text().strip()
        session = self._session_edit.text().strip()

        if not path:
            QMessageBox.warning(
                self,
                "No server folder selected",
                "Import a Prism instance or pack file, or expand the registration "
                "section and choose an existing server folder.",
            )
            return

        candidate = ServerInstance(
            path=path, name=name, version=version, tmux_session=session
        )
        problems = candidate.validate()
        if problems:
            self.result_instance = candidate
            self._pending_manual_registration = True
            self._add_button.setText("Add anyway")
            self._warn_label.setText(
                "Crucible found these issues:\n• "
                + "\n• ".join(problems)
                + "\n\nAdd anyway registers the folder without changing its files."
            )
            self._warn_label.show()
            return

        try:
            inst = self._manager.add_instance(
                path, name, version, tmux_session=session
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Already registered", str(exc))
            return

        self.result_instance = inst
        self.accept()

    def offer_missing_mod_download(self) -> None:
        """Offer missing-mod downloads after the import dialog has fully closed."""
        pending, self._download_offer = self._download_offer, None
        if pending is None:
            return
        target, server_name = pending
        self._maybe_offer_download(target, server_name)

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
