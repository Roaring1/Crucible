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
    QCheckBox, QFileDialog, QMessageBox, QApplication,
)

from ..exporters import client_export
from . import theme

_FORMATS = [
    ("Prism / MultiMC (.zip)", "prism", "zip"),
    ("Modrinth pack (.mrpack)", "mrpack", "mrpack"),
    ("CurseForge (.zip)", "curseforge", "zip"),
]


class _ExportWorker(QObject):
    done = pyqtSignal(object)

    def __init__(self, instance, out_path, fmt, include_config):
        super().__init__()
        self._a = (instance, out_path, fmt, include_config)

    def run(self):
        inst, out, fmt, cfg = self._a
        try:
            result = client_export.export(inst, out, fmt=fmt, include_config=cfg)
        except Exception as exc:
            result = client_export.ExportResult(error=f"Unexpected export error: {exc}")
        self.done.emit(result)


class ClientExportDialog(QDialog):
    def __init__(self, instance, parent=None):
        super().__init__(parent)
        self._instance = instance
        self._thread = None
        self._worker = None
        self._busy = False
        self.setWindowTitle("Create client instance")
        self.resize(470, 250)
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        head = QLabel(f"Make a playable client from \u201c{self._instance.name}\u201d")
        head.setStyleSheet(f"font-weight:700; color:{theme.TEXT};")
        head.setWordWrap(True)
        lay.addWidget(head)

        info = QLabel(
            "Bundles this server's mods and shared config into a client pack. "
            "The actual mod files are included, so nothing needs re-downloading.")
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

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        lay.addWidget(self._status)

        btns = QHBoxLayout()
        btns.addStretch()
        self._export_btn = QPushButton("Export\u2026")
        self._export_btn.setObjectName("PrimaryButton")
        self._export_btn.clicked.connect(self._do_export)
        btns.addWidget(self._export_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        btns.addWidget(self._close_btn)
        lay.addLayout(btns)

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
        self._status.setText("Building\u2026")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._thread = QThread()
        self._worker = _ExportWorker(self._instance, out, fmt,
                                     self._cfg.isChecked())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._thread_finished)
        self._thread.start()

    def _on_done(self, res):
        QApplication.restoreOverrideCursor()
        if res.ok:
            self._status.setText("\u2713 " + res.summary())
            QMessageBox.information(self, "Client instance",
                                    "Done!\n\n" + res.summary() +
                                    f"\n\nSaved to:\n{res.path}")
        else:
            self._status.setText("\u26a0 " + res.summary())
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
