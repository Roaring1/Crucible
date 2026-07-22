"""
crucible/ui/download_dialog.py

Modal dialog that downloads a pack's mods in a background thread, showing a
progress bar and a live log. Cancellable. Never raises into the UI thread —
the worker captures everything into a DownloadResult and reports it back.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QPlainTextEdit, QMessageBox,
)

from . import theme


class _DownloadWorker(QObject):
    """Runs download_pack_mods off the UI thread."""

    progress = pyqtSignal(int, int)     # completed, total
    log = pyqtSignal(str)
    finished = pyqtSignal(object)       # DownloadResult or None

    def __init__(self, target: str, cancel_event: threading.Event):
        super().__init__()
        self._target = target
        self._cancel = cancel_event

    def run(self) -> None:
        result = None
        try:
            # Imported lazily so a missing/edge-case module never breaks app start.
            from ..importers.downloader import download_pack_mods
            result = download_pack_mods(
                self._target,
                progress_cb=lambda c, t: self.progress.emit(c, t),
                log_cb=lambda msg: self.log.emit(msg),
                cancel=self._cancel,
            )
        except Exception as exc:  # absolute last-resort guard
            try:
                self.log.emit(f"Fatal download error: {exc}")
            except Exception:
                pass
        finally:
            self.finished.emit(result)


class DownloadModsDialog(QDialog):
    """Show download progress for a server's staged pack index."""

    def __init__(self, target: str | Path, server_name: str = "", parent=None):
        super().__init__(parent)
        self._target = str(target)
        self._cancel_event = threading.Event()
        self._thread: QThread | None = None
        self._worker: _DownloadWorker | None = None
        self._result = None
        self._done = False

        self.setWindowTitle("Download Server Mods")
        self.setMinimumWidth(560)
        self.setModal(True)
        self._build_ui(server_name)

    def _build_ui(self, server_name: str) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 16, 20, 16)

        title = QLabel("Downloading mods from pack index")
        title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {theme.TEXT};")
        layout.addWidget(title)

        sub = QLabel(
            (f"Server: {server_name}\n" if server_name else "")
            + "Crucible will try to fetch the mod jars listed in the pack's "
            "download index. This needs an internet connection. CurseForge packs "
            "may need a (free) API key — any files that can't be fetched are listed "
            "below so you can grab them manually."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(sub)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(220)
        self._log.setStyleSheet(
            f"background: {theme.MANTLE}; color: {theme.TEXT}; "
            f"font-family: monospace; font-size: 11px; border-radius: 4px;"
        )
        layout.addWidget(self._log)

        row = QHBoxLayout()
        row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        row.addWidget(self._cancel_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self.accept)
        row.addWidget(self._close_btn)
        layout.addLayout(row)

    def start(self) -> None:
        """Kick off the background download. Call after constructing."""
        self._append("Starting download…")
        self._thread = QThread(self)
        self._worker = _DownloadWorker(self._target, self._cancel_event)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._append)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _append(self, msg: str) -> None:
        self._log.appendPlainText(msg)

    def _on_progress(self, completed: int, total: int) -> None:
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(completed)
            self._bar.setFormat("%v / %m  (%p%)")

    def _on_cancel(self) -> None:
        if self._done:
            self.reject()
            return
        self._cancel_event.set()
        self._append("Cancelling… (finishing current file)")
        self._cancel_btn.setEnabled(False)

    def _on_finished(self, result) -> None:
        self._result = result
        self._done = True
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        if result is not None:
            self._append("")
            self._append("Result: " + result.summary())
            if getattr(result, "failed", None):
                self._append("")
                self._append("Files that could not be downloaded:")
                for name, reason in result.failed:
                    self._append(f"  • {name}: {reason}")
        else:
            self._append("Download did not complete.")
        self._cancel_btn.setText("Cancel")
        self._cancel_btn.setEnabled(False)
        self._close_btn.setEnabled(True)
        self._close_btn.setDefault(True)

    @property
    def result(self):
        return self._result

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # Don't allow closing mid-download without confirming.
        if not self._done and self._thread is not None and self._thread.isRunning():
            resp = QMessageBox.question(
                self, "Stop download?",
                "A download is still running. Stop it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._cancel_event.set()
            self._thread.quit()
            self._thread.wait(5000)
        event.accept()
