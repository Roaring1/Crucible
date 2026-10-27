"""
crucible/ui/add_dialog.py

Dialog for adding a new server instance.

Layout favours the *recommended* path — importing a Prism instance or modpack
archive — with manual folder registration available below for advanced users.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import zipfile

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QScrollArea, QWidget,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QDialogButtonBox, QMessageBox, QCheckBox, QFrame, QToolButton,
    QProgressBar, QPlainTextEdit,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from ..importers.prism import import_prism_source
from . import theme
from .thread_lifecycle import connect_thread_cleanup


class _PrismImportWorker(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(object, str)

    def __init__(self, source: str, target: str):
        super().__init__()
        self._source = source
        self._target = target

    def run(self) -> None:
        try:
            source_path = Path(self._source)
            is_archive = source_path.is_file() and source_path.suffix.lower() in {".zip", ".mrpack"}
            has_pack_index = False
            if is_archive:
                try:
                    with zipfile.ZipFile(source_path) as archive:
                        has_pack_index = any(
                            not member.is_dir()
                            and len(PurePosixPath(member.filename).parts) == 1
                            and PurePosixPath(member.filename).name
                            in {"modrinth.index.json", "manifest.json"}
                            for member in archive.infolist()
                        )
                except (OSError, zipfile.BadZipFile):
                    # The importer will give the user the more specific archive error.
                    has_pack_index = False
            self.log.emit(
                "Copying pack files and checking the server-mod list…"
                if has_pack_index
                else "Copying the selected files…"
            )
            info = import_prism_source(
                self._source,
                self._target,
                overwrite=False,
                # Only attempt downloads when an archive actually contains an
                # index. Installed Prism instances already have mod jars, and
                # full server-pack ZIPs often have no index to download from.
                download_mods=has_pack_index,
                download_log_cb=self.log.emit,
                download_progress_cb=lambda done, total: self.progress.emit(done, total),
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
        self._import_pending: tuple[str, str] | None = None
        self._import_result: tuple[object, str] | None = None
        self._pending_import_registration: tuple[str, str, object] | None = None
        self._pending_manual_registration = False
        self._destination_parent = Path.home() / "CrucibleServers"
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
            "Copy a Prism instance or modpack archive into a new server folder. "
            "Crucible tries to download server mods listed by the pack and reports "
            "anything it cannot fetch. For a server folder already in place, expand "
            "the registration option below. Importing does not start the server."
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

        destination_row = QHBoxLayout()
        destination_title = QLabel("Save imported files to:")
        destination_title.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        destination_row.addWidget(destination_title)
        self._destination_label = QLabel(str(self._destination_parent))
        self._destination_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._destination_label.setToolTip(str(self._destination_parent))
        self._destination_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 11px;")
        destination_row.addWidget(self._destination_label, stretch=1)
        destination_change = QPushButton("Change…")
        destination_change.setToolTip("Choose a different parent folder for imported servers")
        destination_change.clicked.connect(self._browse_destination_parent)
        destination_row.addWidget(destination_change)
        layout.addLayout(destination_row)

        import_hint = QLabel(
            "Crucible creates a new folder from the source name and never overwrites existing files."
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
        self._path_edit.textChanged.connect(self._sync_add_button)
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
        form.addRow("Server name:", self._name_edit)

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

        self._import_progress = QProgressBar()
        self._import_progress.setRange(0, 0)
        self._import_progress.setTextVisible(True)
        self._import_progress.setFormat("Preparing import…")
        self._import_progress.hide()
        layout.addWidget(self._import_progress)

        self._import_log = QPlainTextEdit()
        self._import_log.setReadOnly(True)
        self._import_log.setMaximumHeight(120)
        self._import_log.setStyleSheet(
            f"background: {theme.MANTLE}; color: {theme.SUBTEXT}; "
            f"font-family: monospace; font-size: 11px; border-radius: 4px;"
        )
        self._import_log.hide()
        layout.addWidget(self._import_log)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._add_button = btns.button(QDialogButtonBox.StandardButton.Ok)
        self._add_button.setText("Add server")
        self._add_button.setObjectName("PrimaryButton")
        self._add_button.setEnabled(False)
        self._cancel_button = btns.button(QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _toggle_advanced_registration(self, expanded: bool) -> None:
        self._advanced_body.setVisible(expanded)
        self._advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._sync_add_button()
        self.adjustSize()

    def _sync_add_button(self, *_args) -> None:
        """Only enable the commit action when an import or folder is ready."""
        ready_to_add = (
            self._pending_import_registration is not None
            or bool(self._path_edit.text().strip())
        )
        self._add_button.setEnabled(ready_to_add)

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

    def _browse_destination_parent(self) -> None:
        """Change the default import parent only when the user asks to."""
        current = str(self._destination_parent.expanduser())
        parent = QFileDialog.getExistingDirectory(
            self,
            "Choose where to save imported servers",
            current,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not parent:
            return
        self._destination_parent = Path(parent)
        self._destination_label.setText(str(self._destination_parent))
        self._destination_label.setToolTip(str(self._destination_parent))

    def _choose_prism_target(self, suggested_name: str) -> str:
        """Derive a new folder from the source using the chosen default parent."""
        parent = self._destination_parent.expanduser()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._warn_label.setText(
                f"Cannot use destination folder {parent}: {exc}. "
                "Choose another folder with Change…"
            )
            self._warn_label.show()
            return ""

        name = self._safe_folder_name(suggested_name) or "server"
        registered_paths = {
            Path(instance.path).expanduser().resolve()
            for instance in self._manager.instances
        }
        target = parent / name
        suffix = 2
        while (
            target.exists()
            or target.is_symlink()
            or target.resolve() in registered_paths
        ):
            target = parent / f"{name}-{suffix}"
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

        self._import_pending = (source, target)
        self._set_import_controls_enabled(False)
        self._import_progress.setFormat("Copying files and checking server mods…")
        self._import_progress.show()
        self._import_log.clear()
        self._import_log.appendPlainText("Starting import…")
        self._import_log.show()
        self._warn_label.setText(
            "Copying files and checking the pack's server-mod list. "
            "This can take a while for large packs."
        )
        self._warn_label.show()

        self._import_thread = QThread()
        self._import_worker = _PrismImportWorker(source, target)
        self._import_worker.log.connect(self._on_import_log)
        self._import_worker.progress.connect(self._on_import_progress)
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
        self._import_progress.hide()
        if error or info is None or pending is None:
            message = error or "The import did not return a result."
            target_note = (
                f"\n\nSome copied files may remain in: {pending[1]}"
                if pending is not None
                else ""
            )
            self._warn_label.setText(
                "Import failed. No server was added to Crucible.\n\n"
                + message
                + target_note
                + "\n\nCheck the log below, then choose the source again to retry."
            )
            self._warn_label.show()
            self._import_log.appendPlainText("Import failed: " + message)
            self._sync_add_button()
            return

        source, target = pending
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
        self._advanced_body.setEnabled(False)
        self._path_edit.setText(target)
        self._name_edit.setText(candidate.name)
        self._ver_edit.setText(candidate.version)
        self.result_instance = candidate
        self._sync_add_button()

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

    def _on_import_log(self, message: str) -> None:
        self._import_log.appendPlainText(message)
        scrollbar = self._import_log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_import_progress(self, completed: int, total: int) -> None:
        if total <= 0:
            return
        self._import_progress.setRange(0, total)
        self._import_progress.setValue(completed)
        self._import_progress.setFormat(f"Downloading server mods: %v / %m")

    def _set_import_controls_enabled(self, enabled: bool) -> None:
        self._import_folder_btn.setEnabled(enabled)
        self._import_archive_btn.setEnabled(enabled)
        self._advanced_toggle.setEnabled(enabled)
        self._advanced_body.setEnabled(enabled)
        self._cancel_button.setEnabled(enabled)
        self._sync_add_button()

    def _import_thread_finished(self) -> None:
        result = self._import_result
        self._import_thread = None
        self._import_worker = None
        self._import_result = None
        self._set_import_controls_enabled(True)
        if result is not None:
            self._complete_prism_import(*result)
        self._import_pending = None

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
