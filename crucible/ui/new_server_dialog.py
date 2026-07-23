"""
crucible/ui/new_server_dialog.py

The easiest possible path to a running server: pick a loader (Vanilla is the
default and simplest), pick a Minecraft version from a drop-down, choose a
folder, and click Create. Crucible downloads the dedicated server program and
lays down a ready-to-run folder.

All network work happens off the UI thread, and every failure is caught and
shown in the log rather than crashing the app.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QFileDialog, QMessageBox,
    QProgressBar, QPlainTextEdit, QInputDialog,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from . import theme
from .thread_lifecycle import connect_thread_cleanup

_LOADERS = [
    ("Vanilla (no mods — easiest)", "vanilla"),
    ("Fabric", "fabric"),
    ("NeoForge", "neoforge"),
    ("Forge", "forge"),
    ("Quilt", "quilt"),
]


class _VersionWorker(QObject):
    """Fetches the Minecraft version list off the UI thread."""

    finished = pyqtSignal(object, str)  # (list[McVersion], latest_release_id)

    def __init__(self, include_snapshots: bool):
        super().__init__()
        self._snap = include_snapshots

    def run(self) -> None:
        versions, latest = [], ""
        try:
            from ..importers import serverloader as sl
            versions = sl.list_versions(include_snapshots=self._snap)
            latest = sl.latest_release()
        except Exception:
            versions, latest = [], ""
        finally:
            self.finished.emit(versions, latest)


class _CreateWorker(QObject):
    """Runs create_fresh_server off the UI thread."""

    log = pyqtSignal(str)
    finished = pyqtSignal(object)  # FreshServerResult or None

    def __init__(self, target, mc, loader, loader_version, accept_eula, cancel):
        super().__init__()
        self._args = (target, mc, loader, loader_version, accept_eula)
        self._cancel = cancel

    def run(self) -> None:
        result = None
        try:
            from ..importers import serverloader as sl
            target, mc, loader, lv, eula = self._args
            result = sl.create_fresh_server(
                target,
                minecraft_version=mc,
                loader=loader,
                loader_version=lv,
                accept_eula=eula,
                overwrite=True,
                log_cb=lambda m: self.log.emit(m),
                cancel=self._cancel,
            )
        except Exception as exc:  # last-resort guard
            try:
                self.log.emit(f"Fatal error: {exc}")
            except Exception:
                pass
        finally:
            self.finished.emit(result)


class NewServerDialog(QDialog):
    """Create a brand-new server (Vanilla/Fabric/Forge/NeoForge/Quilt)."""

    def __init__(self, manager: InstanceManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.result_instance: ServerInstance | None = None
        self._cancel_event = threading.Event()
        self._vthread: QThread | None = None
        self._vworker: _VersionWorker | None = None
        self._cthread: QThread | None = None
        self._cworker: _CreateWorker | None = None
        self._busy = False
        self._path_user_edited = False  # True once the user types/browses a folder
        self._close_requested = False
        self._accept_after_create = False

        self.setWindowTitle("Create a New Server")
        self.setMinimumWidth(560)
        self.setModal(True)
        self._build_ui()
        self._reload_versions()

    def _open_modpack_dialog(self) -> None:
        from .modpack_dialog import ModpackDialog
        dlg = ModpackDialog(self._manager, self)
        if dlg.exec() and dlg.result_instance is not None:
            # Hand the freshly-created instance back to the caller and close.
            self.result_instance = dlg.result_instance
            self.accept()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 16, 20, 16)

        title = QLabel("Create a new server")
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {theme.TEXT};")
        layout.addWidget(title)

        sub = QLabel(
            "Pick a type and a Minecraft version, choose where to put it, and "
            "click Create. Crucible downloads the official dedicated server for "
            "you. Vanilla is the simplest — just pick a version and go."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(sub)

        # Shortcut to the one-click modpack installer.
        pack_row = QHBoxLayout()
        pack_lbl = QLabel("Want a ready-made modpack instead?")
        pack_lbl.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        pack_row.addWidget(pack_lbl)
        self._modpack_btn = QPushButton("\U0001F4E6  Install a modpack instead\u2026")
        self._modpack_btn.setToolTip(
            "Browse Modrinth modpacks and install one as a server in one click.")
        self._modpack_btn.clicked.connect(self._open_modpack_dialog)
        pack_row.addWidget(self._modpack_btn)
        pack_row.addStretch()
        layout.addLayout(pack_row)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addLayout(form)

        # Loader type
        self._loader_combo = QComboBox()
        for label, _ in _LOADERS:
            self._loader_combo.addItem(label)
        self._loader_combo.currentIndexChanged.connect(self._on_loader_changed)
        form.addRow("Server type:", self._loader_combo)

        # Version + refresh
        ver_row = QHBoxLayout()
        self._ver_combo = QComboBox()
        self._ver_combo.setEditable(True)
        self._ver_combo.setMinimumWidth(220)
        ver_row.addWidget(self._ver_combo, stretch=1)
        self._snap_check = QCheckBox("Snapshots")
        self._snap_check.stateChanged.connect(lambda *_: self._reload_versions())
        ver_row.addWidget(self._snap_check)
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedWidth(34)
        self._refresh_btn.setToolTip("Refresh the version list")
        self._refresh_btn.setAccessibleName("Refresh Minecraft version list")
        self._refresh_btn.clicked.connect(self._reload_versions)
        ver_row.addWidget(self._refresh_btn)
        form.addRow("Minecraft version:", ver_row)

        self._ver_status = QLabel("Loading versions…")
        self._ver_status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        form.addRow("", self._ver_status)

        # Loader version (only for forge/neoforge/quilt/fabric)
        self._lver_edit = QLineEdit()
        self._lver_edit.setPlaceholderText("latest (leave blank)")
        self._lver_row_label = QLabel("Loader version:")
        form.addRow(self._lver_row_label, self._lver_edit)

        # Display name first -- the folder is derived from it automatically.
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("My Server")
        self._name_edit.textChanged.connect(self._on_name_changed)
        form.addRow("Display name:", self._name_edit)

        # Destination folder, placed under the name and kept in sync with it
        # until the user edits it themselves.
        dst_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(str(Path.home() / "CrucibleServers" / "my-server"))
        self._path_edit.textEdited.connect(self._on_path_edited)
        dst_row.addWidget(self._path_edit, stretch=1)
        browse = QPushButton("Browse…")
        browse.setFixedWidth(80)
        browse.clicked.connect(self._browse)
        dst_row.addWidget(browse)
        self._path_hint = QLabel("Auto-named from the display name — edit if you want a different folder.")
        self._path_hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        form.addRow("Folder:", dst_row)
        form.addRow("", self._path_hint)

        self._eula_check = QCheckBox(
            "I agree to the Minecraft EULA (minecraft.net/eula) — required to run"
        )
        self._eula_check.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(self._eula_check)

        # Log (hidden until creation starts)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(150)
        self._log.setStyleSheet(
            f"background: {theme.MANTLE}; color: {theme.TEXT}; "
            f"font-family: monospace; font-size: 11px; border-radius: 4px;"
        )
        self._log.hide()
        layout.addWidget(self._log)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)  # indeterminate
        self._bar.hide()
        layout.addWidget(self._bar)

        # Buttons
        row = QHBoxLayout()
        row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        row.addWidget(self._cancel_btn)
        self._create_btn = QPushButton("✨  Create Server")
        self._create_btn.setObjectName("PrimaryButton")
        self._create_btn.setFixedHeight(34)
        self._create_btn.clicked.connect(self._on_create)
        row.addWidget(self._create_btn)
        layout.addLayout(row)

        self._on_loader_changed()

    # ---- version loading ----
    def _current_loader(self) -> str:
        idx = self._loader_combo.currentIndex()
        return _LOADERS[idx][1] if 0 <= idx < len(_LOADERS) else "vanilla"

    def _on_loader_changed(self) -> None:
        loader = self._current_loader()
        show_lver = loader != "vanilla"
        self._lver_edit.setVisible(show_lver)
        self._lver_row_label.setVisible(show_lver)

    def _reload_versions(self) -> None:
        if self._busy or (self._vthread is not None and self._vthread.isRunning()):
            return
        self._ver_status.setText("Loading versions…")
        self._refresh_btn.setEnabled(False)
        self._snap_check.setEnabled(False)
        self._vthread = QThread(self)
        self._vworker = _VersionWorker(self._snap_check.isChecked())
        self._vworker.moveToThread(self._vthread)
        self._vthread.started.connect(self._vworker.run)
        self._vworker.finished.connect(self._on_versions_loaded)
        self._vworker.finished.connect(self._vthread.quit)
        connect_thread_cleanup(self._vthread, self._version_thread_finished)
        self._vthread.start()

    def _on_versions_loaded(self, versions, latest) -> None:
        if self._vthread is not None:
            self._vthread.quit()
        self._refresh_btn.setEnabled(True)
        self._snap_check.setEnabled(True)
        current = self._ver_combo.currentText().strip()
        self._ver_combo.clear()
        if not versions:
            self._ver_status.setText(
                "Could not load the version list (offline?). Type a version like "
                "1.21.1 manually."
            )
            if current:
                self._ver_combo.addItem(current)
            return
        ids = [v.id for v in versions]
        self._ver_combo.addItems(ids)
        # Default to latest release.
        pick = current or latest or (ids[0] if ids else "")
        if pick:
            i = self._ver_combo.findText(pick)
            if i >= 0:
                self._ver_combo.setCurrentIndex(i)
            else:
                self._ver_combo.setEditText(pick)
        self._ver_status.setText(
            f"{len(ids)} versions available" + (f" · latest release {latest}" if latest else "")
        )

    # ---- destination helpers ----
    def _browse(self) -> None:
        base = Path.home() / "CrucibleServers"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            base = Path.home()
        parent = QFileDialog.getExistingDirectory(
            self, "Choose where to create the server folder", str(base),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not parent:
            return
        suggested = self._safe(self._name_edit.text()) or "my-server"
        name, ok = QInputDialog.getText(
            self, "Name the server folder", "Folder name:", text=suggested
        )
        if not ok:
            return
        name = self._safe(name) or suggested
        self._path_edit.setText(str(Path(parent) / name))
        # An explicit browse means "use this location" -- stop auto-deriving.
        self._path_user_edited = True
        self._path_hint.setText("Custom folder set.")

    @staticmethod
    def _safe(name: str) -> str:
        keep = "-_. "
        cleaned = "".join(c for c in (name or "") if c.isalnum() or c in keep).strip()
        return cleaned.replace(" ", "-")

    def _on_path_edited(self, _text: str) -> None:
        """User typed in the folder field -- stop auto-deriving it."""
        self._path_user_edited = True
        self._path_hint.setText("Custom folder set.")

    def _on_name_changed(self, text: str) -> None:
        """Keep the folder path in sync with the display name until edited."""
        if self._path_user_edited:
            return
        safe = self._safe(text)
        if safe:
            self._path_edit.setText(str(Path.home() / "CrucibleServers" / safe))
        else:
            self._path_edit.clear()

    # ---- creation ----
    def _on_create(self) -> None:
        if self._busy:
            return
        mc = self._ver_combo.currentText().strip()
        loader = self._current_loader()
        lver = self._lver_edit.text().strip()
        path = self._path_edit.text().strip()
        name = self._name_edit.text().strip() or (Path(path).name if path else "")

        if not mc:
            QMessageBox.warning(self, "Pick a version", "Choose a Minecraft version first.")
            return
        if not path:
            QMessageBox.warning(self, "Choose a folder", "Pick where to create the server.")
            return
        if not self._eula_check.isChecked():
            resp = QMessageBox.question(
                self, "Minecraft EULA",
                "The server can't run until the Minecraft EULA is accepted.\n\n"
                "Create the folder now and accept later?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        try:
            from ..importers import serverloader as sl
            if sl.requires_java(loader):
                from ..data.instance_model import ServerInstance as _SI
                java_ok, _ = _SI.java_info()
                if not java_ok:
                    QMessageBox.warning(
                        self, "Java required",
                        f"Installing a {loader} server runs an installer that needs "
                        "Java on your PATH, which wasn't found. Vanilla and Fabric "
                        "don't need this.",
                    )
                    return
        except Exception:
            pass

        self._busy = True
        self._cancel_event.clear()
        self._create_btn.setEnabled(False)
        self._loader_combo.setEnabled(False)
        self._ver_combo.setEnabled(False)
        self._log.show()
        self._bar.show()
        self._log.appendPlainText(f"Creating {loader} server for Minecraft {mc}…")
        self._pending = (path, name, mc, loader, lver)

        self._cthread = QThread(self)
        self._cworker = _CreateWorker(
            path, mc, loader, lver, self._eula_check.isChecked(), self._cancel_event
        )
        self._cworker.moveToThread(self._cthread)
        self._cthread.started.connect(self._cworker.run)
        self._cworker.log.connect(lambda m: self._log.appendPlainText(m))
        self._cworker.finished.connect(self._on_created)
        self._cworker.finished.connect(self._cthread.quit)
        connect_thread_cleanup(self._cthread, self._create_thread_finished)
        self._cthread.start()

    def _on_created(self, result) -> None:
        if self._cthread is not None:
            self._cthread.quit()
        self._bar.hide()
        self._busy = False
        path, name, mc, loader, lver = self._pending

        if result is None:
            self._log.appendPlainText("Creation did not complete.")
            self._create_btn.setEnabled(True)
            self._loader_combo.setEnabled(True)
            self._ver_combo.setEnabled(True)
            return

        self._log.appendPlainText("")
        self._log.appendPlainText(result.summary())

        # Register the instance regardless — the folder exists and is well-formed,
        # so the user can finish a failed download later and it will just run.
        try:
            version_label = f"MC {mc}" + (f" · {loader}" if loader != "vanilla" else "")
            inst = self._manager.add_instance(
                path, name or Path(path).name, version_label,
                pack_source=f"new_{loader}",
                minecraft_version=mc, loader=("" if loader == "vanilla" else loader),
                loader_version=lver,
            )
            self.result_instance = inst
        except ValueError as exc:
            QMessageBox.warning(self, "Already registered", str(exc))
            self._create_btn.setEnabled(True)
            self._loader_combo.setEnabled(True)
            self._ver_combo.setEnabled(True)
            return

        if result.ok:
            self._accept_after_create = True
        else:
            # Folder registered but server program not installed (e.g. offline).
            QMessageBox.warning(
                self, "Server program not installed",
                "The folder was created and registered, but the server program "
                "couldn't be downloaded:\n\n"
                f"{result.failed_reason}\n\n"
                "You can retry from the instance's Setup tab when you're online.",
            )
            self._accept_after_create = True

    def _version_thread_finished(self) -> None:
        self._vthread = None
        self._vworker = None
        self._finish_deferred_close()

    def _create_thread_finished(self) -> None:
        self._cthread = None
        self._cworker = None
        if self._close_requested:
            self._finish_deferred_close()
        elif self._accept_after_create:
            super().accept()

    def _finish_deferred_close(self) -> None:
        if self._close_requested and not self._workers_running():
            super().reject()

    def _workers_running(self) -> bool:
        return bool(
            (self._cthread is not None and self._cthread.isRunning())
            or (self._vthread is not None and self._vthread.isRunning())
        )

    def _on_cancel(self) -> None:
        if self._workers_running():
            self._close_requested = True
            self._cancel_event.set()
            self._cancel_btn.setEnabled(False)
            if self._busy:
                self._log.appendPlainText(
                    "Cancelling… this window will close when the worker exits safely."
                )
            else:
                self._ver_status.setText("Closing after version lookup finishes…")
            return
        self.reject()

    def reject(self) -> None:
        if self._workers_running():
            self._on_cancel()
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._workers_running():
            self._on_cancel()
            event.ignore()
            return
        super().closeEvent(event)
