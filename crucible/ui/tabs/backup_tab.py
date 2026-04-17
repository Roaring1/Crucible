"""
crucible/ui/tabs/backup_tab.py

Backup management tab.

Layout:
  ┌──────────────────────────────────────────────────────┐
  │  World: <level-name>   Backups: 4  (1.8 GB)         │
  │                             [📁 Open Folder] [💾 Backup Now] │
  ├──────────────────────────────────────────────────────┤
  │  [████████████░░░░] 63%  Creating backup…           │  (hidden normally)
  ├──────────────────────────────────────────────────────┤
  │  Filename              │ Size  │ Created    │        │
  │  ─────────────────────────────────────────────────   │
  │  Midtech_20250416.zip  │ 450MB │ Apr 16 14:23 │ [×] │
  └──────────────────────────────────────────────────────┘
  Keep at most: [10 ▲▼] backups
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSpinBox, QMessageBox,
)

from ...data.instance_model import ServerInstance
from ...data.backup_manager import BackupManager, BackupWorker, BackupEntry
from .. import theme


class BackupTab(QWidget):
    """World backup management for one server instance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
        self._manager:  BackupManager | None  = None
        self._thread:   QThread | None        = None
        self._worker:   BackupWorker | None   = None
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ── Info / action row ──
        info_row = QHBoxLayout()

        self._world_label = QLabel("World: —")
        self._world_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        self._size_label  = QLabel("")
        self._size_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")

        self._open_btn   = QPushButton("📁  Open Folder")
        self._backup_btn = QPushButton("💾  Backup Now")
        self._backup_btn.setObjectName("PrimaryButton")

        self._open_btn.clicked.connect(self._open_folder)
        self._backup_btn.clicked.connect(self._start_backup)

        info_row.addWidget(self._world_label)
        info_row.addSpacing(16)
        info_row.addWidget(self._size_label)
        info_row.addStretch()
        info_row.addWidget(self._open_btn)
        info_row.addWidget(self._backup_btn)
        layout.addLayout(info_row)

        # ── Backup storage path label ──
        self._path_label = QLabel("")
        self._path_label.setStyleSheet(
            f"color: {theme.SURFACE2}; font-size: 11px; font-family: monospace;"
        )
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)

        # ── Progress (hidden normally) ──
        self._progress_lbl = QLabel("Creating backup…")
        self._progress_lbl.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 12px;"
        )
        self._progress     = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress_lbl.hide()
        self._progress.hide()
        layout.addWidget(self._progress_lbl)
        layout.addWidget(self._progress)

        # ── Table ──
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Filename", "Size", "Created", ""])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(3, 36)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table, stretch=1)

        # ── Prune setting ──
        prune_row = QHBoxLayout()
        prune_row.addWidget(QLabel("Keep at most:"))
        self._prune_spin = QSpinBox()
        self._prune_spin.setRange(1, 100)
        self._prune_spin.setValue(10)
        self._prune_spin.setFixedWidth(64)
        prune_row.addWidget(self._prune_spin)
        prune_row.addWidget(QLabel("backups  (older ones deleted automatically)"))
        prune_row.addStretch()
        layout.addLayout(prune_row)

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._manager  = BackupManager(instance)
        self._refresh()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        if not self._manager or not self._instance:
            return

        # World label — read level-name from server.properties
        props = Path(self._instance.path) / "server.properties"
        level_name = "world"
        if props.exists():
            for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("level-name="):
                    level_name = line.split("=", 1)[1].strip()
                    break
        self._world_label.setText(f"World: {level_name}")

        backups     = self._manager.list_backups()
        total_bytes = self._manager.total_size_bytes()
        self._size_label.setText(
            f"Backups: {len(backups)}  ({_human_size(total_bytes)})"
        )
        self._path_label.setText(
            f"Stored in: {self._manager.backup_dir()}"
        )

        self._table.setRowCount(len(backups))
        for row, entry in enumerate(backups):
            self._table.setItem(row, 0, QTableWidgetItem(entry.filename))
            size_item = QTableWidgetItem(entry.size_display)
            size_item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            )
            self._table.setItem(row, 1, size_item)
            self._table.setItem(
                row, 2,
                QTableWidgetItem(entry.created_at.strftime("%b %d  %H:%M"))
            )
            del_btn = QPushButton("×")
            del_btn.setFixedWidth(28)
            del_btn.setObjectName("DangerButton")
            del_btn.clicked.connect(
                lambda _checked=False, e=entry: self._confirm_delete(e)
            )
            self._table.setCellWidget(row, 3, del_btn)
            self._table.setRowHeight(row, 30)

    def _start_backup(self) -> None:
        if not self._manager:
            return
        if self._thread and self._thread.isRunning():
            return

        self._backup_btn.setEnabled(False)
        self._progress.setValue(0)
        self._progress_lbl.show()
        self._progress.show()

        self._thread = QThread()
        self._worker = BackupWorker(self._manager)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_done(self, _path: str) -> None:
        self._progress_lbl.hide()
        self._progress.hide()
        self._backup_btn.setEnabled(True)
        self._thread.quit()
        self._thread.wait()
        self._manager.prune_old(self._prune_spin.value())
        self._refresh()

    def _on_failed(self, error: str) -> None:
        self._progress_lbl.hide()
        self._progress.hide()
        self._backup_btn.setEnabled(True)
        if self._thread:
            self._thread.quit()
        QMessageBox.critical(self, "Backup Failed", error)

    def _confirm_delete(self, entry: BackupEntry) -> None:
        reply = QMessageBox.question(
            self, "Delete Backup",
            f"Permanently delete:\n{entry.filename}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._manager.delete_backup(entry)
            self._refresh()

    def _open_folder(self) -> None:
        if self._manager:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._manager.backup_dir()))
            )


def _human_size(b: int) -> str:
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f} GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.0f} MB"
    return f"{b / 1024:.0f} KB"
