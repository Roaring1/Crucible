"""
crucible/ui/client_export_dialog.py

One-click "make a client instance" dialog. Pick a format, choose where to save,
and Crucible bundles the server's mods (+ shared config) into a client pack you
can import into Prism Launcher, the Modrinth App, or CurseForge.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QCheckBox, QFileDialog, QMessageBox, QApplication, QLineEdit,
)

from ..exporters import client_export
from . import theme
from .thread_lifecycle import connect_thread_cleanup

_FORMATS = [
    ("Prism / MultiMC (.zip)", "prism", "zip"),
    ("Modrinth pack (.mrpack)", "mrpack", "mrpack"),
    ("CurseForge (.zip)", "curseforge", "zip"),
]


class _ExportWorker(QObject):
    done = pyqtSignal(object)

    def __init__(self, instance, out_path, fmt, include_config, client_source=""):
        super().__init__()
        self._a = (instance, out_path, fmt, include_config, client_source)

    def run(self):
        inst, out, fmt, cfg, source = self._a
        try:
            result = client_export.export(inst, out, fmt=fmt, include_config=cfg, client_source=source)
        except Exception as exc:
            result = client_export.ExportResult(error=f"Unexpected export error: {exc}")
        self.done.emit(result)


class ClientExportDialog(QDialog):
    def __init__(self, instance, parent=None, manager=None):
        super().__init__(parent)
        self._instance = instance
        self._instance_manager = manager
        self._thread = None
        self._worker = None
        self._busy = False
        self.setWindowTitle("Create client instance")
        self.resize(620, 390)
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        head = QLabel(f"Create a client pack from “{self._instance.name}”")
        head.setStyleSheet(f"font-weight:700; color:{theme.TEXT};")
        head.setWordWrap(True)
        lay.addWidget(head)

        info = QLabel(
            "Bundles this server's mods and shared config into a client pack. "
            "Mod JARs are included; the launcher still downloads Minecraft and its loader.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        lay.addWidget(info)

        row = QHBoxLayout()
        row.addWidget(QLabel("Format:"))
        self._fmt = QComboBox()
        for label, _, _ in _FORMATS:
            self._fmt.addItem(label)
        row.addWidget(self._fmt, 1)
        lay.addLayout(row)

        self._cfg = QCheckBox("Include config/ and kubejs/ (recommended)")
        self._cfg.setChecked(True)
        lay.addWidget(self._cfg)
        source_row = QHBoxLayout()
        self._source = QLineEdit()
        self._source.setPlaceholderText("Optional: matching Prism instance for client-only mods")
        suggested = getattr(self._instance, "prism_source", "") or ""
        if Path(suggested).is_dir() and (Path(suggested)/"mmc-pack.json").is_file():
            self._source.setText(suggested)
        source_row.addWidget(self._source, 1)
        pick = QPushButton("Browse…")
        pick.clicked.connect(self._pick_source)
        source_row.addWidget(pick)
        lay.addLayout(source_row)

        self._remember_source = QCheckBox("Remember this Prism source for future exports")
        self._remember_source.setChecked(True)
        self._remember_source.setToolTip(
            "After a successful export, save this matching instance path in Crucible's server registry."
        )
        if self._instance_manager is None:
            self._remember_source.setEnabled(False)
            self._remember_source.setToolTip(
                "Open the exporter from Crucible's server manager to save a source path."
            )
        lay.addWidget(self._remember_source)
        caution = QLabel("Client-only extras are added only from a matching Prism instance. Unknown compatibility is reported, never guessed. Test in Prism before sharing; respect mod licenses.")
        caution.setWordWrap(True)
        lay.addWidget(caution)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        lay.addWidget(self._status)

        btns = QHBoxLayout()
        btns.addStretch()
        self._export_btn = QPushButton("Export…")
        self._export_btn.setObjectName("PrimaryButton")
        self._export_btn.clicked.connect(self._do_export)
        btns.addWidget(self._export_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        btns.addWidget(self._close_btn)
        lay.addLayout(btns)

    def _pick_source(self):
        path = QFileDialog.getExistingDirectory(self, "Choose matching Prism instance")
        if path:
            self._source.setText(path)

    def _do_export(self):
        idx = self._fmt.currentIndex()
        _, fmt, ext = _FORMATS[idx]
        default = f"{self._instance.name}-client.{ext}"
        out, _ = QFileDialog.getSaveFileName(
            self, "Save client instance", str(Path.home() / default), f"*.{ext}")
        if not out:
            return
        self._busy = True
        self._export_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._status.setText("Building…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._thread = QThread()
        self._worker = _ExportWorker(self._instance, out, fmt,
                                     self._cfg.isChecked(), self._source.text().strip())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        connect_thread_cleanup(self._thread, self._thread_finished)
        self._thread.start()

    def _remember_client_source_after_success(self) -> str:
        """Persist a successfully validated source profile through the registry manager."""
        if not self._remember_source.isChecked() or self._instance_manager is None:
            return ""
        raw = self._source.text().strip()
        if not raw:
            return ""
        source = Path(raw).expanduser()
        pack_file = source / "mmc-pack.json"
        if source.is_symlink() or not source.is_dir() or pack_file.is_symlink() or not pack_file.is_file():
            return (
                "The export succeeded, but this Prism source was not saved. "
                "Choose a real, fully installed Prism instance containing mmc-pack.json."
            )
        try:
            canonical = str(source.resolve(strict=True))
        except OSError as exc:
            return f"The export succeeded, but Crucible could not resolve the Prism source path: {exc}"
        previous = getattr(self._instance, "prism_source", "") or ""
        if previous:
            try:
                if str(Path(previous).expanduser().resolve()) == canonical:
                    return ""
            except OSError:
                pass
        try:
            self._instance.prism_source = canonical
            self._instance_manager.update_instance(self._instance)
        except Exception as exc:
            self._instance.prism_source = previous
            return f"The export succeeded, but Crucible could not remember this Prism source: {exc}"
        return "Saved this matching Prism source for future exports."

    def _on_done(self, res):
        QApplication.restoreOverrideCursor()
        if res.ok:
            remember_status = self._remember_client_source_after_success()
            self._status.setText("✓ " + res.summary())
            details = "Done!\n\n" + res.summary() + f"\n\nSaved to:\n{res.path}\n"
            if res.archive_sha256:
                details += f"\nArchive SHA-256:\n{res.archive_sha256}\n"
            if res.manifest_path:
                details += f"\nManifest inside archive:\n{res.manifest_path}\n"
            details += "\n" + "\n".join(res.warnings)
            if remember_status:
                details += "\n\n" + remember_status
            QMessageBox.information(self, "Client instance", details)
        else:
            self._status.setText("⚠ " + res.summary())
            QMessageBox.warning(self, "Client instance", res.summary())
    def _thread_finished(self):
        self._thread = None
        self._worker = None
        self._busy = False
        self._export_btn.setEnabled(True)
        self._close_btn.setEnabled(True)

    def reject(self):
        if self._busy:
            self._status.setText("Export is still running — wait for it to finish before closing.")
            return
        super().reject()

    def closeEvent(self, event):
        if self._busy:
            self._status.setText("Export is still running — wait for it to finish before closing.")
            event.ignore()
            return
        super().closeEvent(event)
