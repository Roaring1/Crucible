"""
crucible/ui/tabs/world_tab.py

World identification, named "world slot" backups, and the safe world-swap
workflow (World Backup & Swap feature).

This is deliberately separate from BackupTab (which stays a simple "one zip
per click" tool): WorldTab is where a user manages MULTIPLE named saved
worlds and safely swaps the live world between them. Swapping is the
highest-risk operation in Crucible, so every precondition is enforced here
and explained in plain language before anything touches disk:

  - The server must be fully stopped (checked live via tmux; any status
    other than the exact "stopped" value blocks the swap).
  - There must be no unsaved server.properties edits (delegated to whatever
    guard is wired in via set_config_guard -- normally ConfigTab's own
    confirm_discard_or_save).
  - A safety backup of the CURRENT world is always taken automatically,
    with no way to skip it.
  - The actual swap (BackupManager.swap_world) runs in a background thread
    and rolls itself back automatically on any failure -- see
    crucible/data/backup_manager.py for that logic.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QMessageBox, QInputDialog,
)

from ...data.instance_model import ServerInstance, dimension_label
from ...data.backup_manager import (
    BackupManager, BackupWorker, BackupEntry, SwapWorker, SwapResult,
)
from ...process.tmux_manager import TmuxManager
from .. import theme


class WorldTab(QWidget):
    """Named world-slot backups and safe world swapping for one instance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
        self._manager:  BackupManager | None  = None
        self._thread:   QThread | None        = None
        self._worker = None
        self._active_manager: BackupManager | None = None
        self._op_kind: str | None = None      # "backup" | "swap"
        self._op_result = None
        self._config_guard: Callable[[], bool] | None = None
        self._build_ui()

    # Public API

    def set_config_guard(self, fn: Callable[[], bool] | None) -> None:
        """Wire a guard (e.g. ConfigTab.confirm_discard_or_save) that must
        return True before a swap is allowed to proceed. Swapping while
        there are unsaved server.properties edits could restore a world
        that no longer matches the properties the user thinks are active.
        """
        self._config_guard = fn

    def load(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._manager = BackupManager(instance)
        self._refresh()

    def has_active_operation(self) -> bool:
        return bool(self._thread and self._thread.isRunning())

    # UI construction

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        head = QLabel("World: current world")
        head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        layout.addWidget(head)

        info_row = QHBoxLayout()
        self._world_label = QLabel("World: -")
        self._world_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        info_row.addWidget(self._world_label)
        info_row.addStretch()
        info_row.addWidget(refresh_btn)
        layout.addLayout(info_row)

        self._dims_label = QLabel("")
        self._dims_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        self._dims_label.setWordWrap(True)
        layout.addWidget(self._dims_label)

        layout.addWidget(_hline())

        backup_head = QLabel("Save the current world as a new slot")
        backup_head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        layout.addWidget(backup_head)

        backup_info = QLabel(
            "Creates a full backup of the current world, including every "
            "dimension folder inside it, and gives it a name so you can find "
            "and swap back to it later."
        )
        backup_info.setWordWrap(True)
        backup_info.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(backup_info)

        backup_row = QHBoxLayout()
        self._backup_btn = QPushButton("Backup this world...")
        self._backup_btn.setObjectName("PrimaryButton")
        self._backup_btn.clicked.connect(self._start_named_backup)
        backup_row.addWidget(self._backup_btn)
        backup_row.addStretch()
        layout.addLayout(backup_row)

        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress_lbl.hide()
        self._progress.hide()
        layout.addWidget(self._progress_lbl)
        layout.addWidget(self._progress)

        layout.addWidget(_hline())

        slots_head = QLabel("Saved worlds (backups)")
        slots_head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        layout.addWidget(slots_head)

        slots_info = QLabel(
            "Named backups are never deleted automatically. Unnamed backups "
            "created elsewhere (e.g. the Backups tab) also show up here."
        )
        slots_info.setWordWrap(True)
        slots_info.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(slots_info)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Name", "Size", "Created", ""])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(3, 230)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table, stretch=1)

        layout.addWidget(_hline())

        presafe_row = QHBoxLayout()
        self._presafe_label = QLabel("")
        self._presafe_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        self._presafe_label.setWordWrap(True)
        self._cleanup_btn = QPushButton("Clean up old pre-swap folders")
        self._cleanup_btn.clicked.connect(self._cleanup_pre_swap_dirs)
        presafe_row.addWidget(self._presafe_label, stretch=1)
        presafe_row.addWidget(self._cleanup_btn)
        layout.addLayout(presafe_row)

    # Refresh

    def _refresh(self) -> None:
        if not self._manager or not self._instance:
            return
        inst = self._instance
        world_root = inst.world_root_path()

        if world_root.is_dir():
            size = inst.world_size_bytes()
            dims = inst.world_dimension_dirs()
            self._world_label.setText(f"World: {world_root.name}   ({_human_size(size)})")
            if dims:
                parts = ", ".join(f"{d.name} ({dimension_label(d.name)})" for d in dims)
                self._dims_label.setText(f"Dimensions found: Overworld, {parts}")
            else:
                self._dims_label.setText("Dimensions found: Overworld only (no Nether/End generated yet)")
        else:
            self._world_label.setText(f"World: {world_root.name}   (not generated yet)")
            self._dims_label.setText(
                "This server has never been started, so no world folder exists yet."
            )

        entries = self._manager.list_backups()
        self._table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name_item = QTableWidgetItem(entry.display_name)
            if not entry.slot_name:
                name_item.setToolTip("Unnamed auto-backup")
            self._table.setItem(row, 0, name_item)

            size_item = QTableWidgetItem(entry.size_display)
            size_item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            self._table.setItem(row, 1, size_item)

            self._table.setItem(row, 2, QTableWidgetItem(entry.created_at.strftime("%b %d  %H:%M")))

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 0, 2, 0)
            actions_layout.setSpacing(4)

            swap_btn = QPushButton("Swap to this")
            swap_btn.setAccessibleName(f"Swap to world backup {entry.display_name}")
            swap_btn.clicked.connect(lambda _checked=False, e=entry: self._confirm_and_swap(e))

            rename_btn = QPushButton("Rename")
            rename_btn.setAccessibleName(f"Rename world backup {entry.display_name}")
            rename_btn.clicked.connect(lambda _checked=False, e=entry: self._rename_entry(e))

            del_btn = QPushButton("x")
            del_btn.setFixedWidth(28)
            del_btn.setObjectName("DangerButton")
            del_btn.setAccessibleName(f"Delete world backup {entry.display_name}")
            del_btn.clicked.connect(lambda _checked=False, e=entry: self._confirm_delete(e))

            actions_layout.addWidget(swap_btn)
            actions_layout.addWidget(rename_btn)
            actions_layout.addWidget(del_btn)
            self._table.setCellWidget(row, 3, actions)
            self._table.setRowHeight(row, 32)

        pre_swap_dirs = self._manager.list_pre_swap_dirs()
        if pre_swap_dirs:
            total = sum(_dir_size(d) for d in pre_swap_dirs)
            self._presafe_label.setText(
                f"{len(pre_swap_dirs)} leftover pre-swap folder(s) from previous swaps "
                f"({_human_size(total)}). These are an extra safety net kept alongside "
                "your world folder -- once you've confirmed your current world is good, "
                "you can delete them."
            )
            self._cleanup_btn.setEnabled(True)
        else:
            self._presafe_label.setText("No leftover pre-swap folders.")
            self._cleanup_btn.setEnabled(False)

    # Backup action

    def _start_named_backup(self) -> None:
        if not self._manager or not self._instance:
            return
        if self._thread and self._thread.isRunning():
            return

        default_name = f"World backup {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        name, ok = QInputDialog.getText(
            self, "Backup this world",
            "Name this backup (leave blank for an unnamed auto-backup):",
            text=default_name,
        )
        if not ok:
            return
        slot_name = name.strip() or None

        tmux = TmuxManager()
        status = tmux.get_status(self._instance)
        if status == "unknown":
            QMessageBox.critical(
                self, "Backup cancelled",
                "Crucible could not verify whether the server is running. "
                "No backup was started; retry after the tmux status recovers.",
            )
            return
        if status == "unmanaged":
            QMessageBox.critical(
                self, "Backup cancelled",
                "A matching server process is running outside the configured "
                "tmux session. Crucible cannot flush its world safely. Stop it "
                "manually or restore the correct tmux session first.",
            )
            return
        if status == "running":
            reply = QMessageBox.warning(
                self, "Back up a running server?",
                "The server is currently running. Crucible will request a save-all "
                "before copying, but a stopped-server backup is the safest option.\n\n"
                "Continue with a live backup?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            ok2, detail = tmux.send_command_result(self._instance, "save-all flush")
            if not ok2:
                QMessageBox.critical(
                    self, "Backup cancelled",
                    f"Could not request a world save:\n{detail}",
                )
                return

        self._begin_operation("backup")
        self._active_manager = self._manager
        self._thread = QThread()
        self._worker = BackupWorker(self._active_manager, slot_name=slot_name)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_backup_done)
        self._worker.failed.connect(self._on_backup_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._thread_finished)
        self._thread.start()

    def _on_backup_done(self, path: str) -> None:
        self._op_result = ("done", path)

    def _on_backup_failed(self, error: str) -> None:
        self._op_result = ("failed", error)

    # Swap action

    def _confirm_and_swap(self, entry: BackupEntry) -> None:
        if not self._manager or not self._instance:
            return
        if self._thread and self._thread.isRunning():
            return

        tmux = TmuxManager()
        status = tmux.get_status(self._instance)
        if status != "stopped":
            QMessageBox.critical(
                self, "Swap cancelled",
                "The server must be fully stopped before swapping worlds -- "
                "swapping a world folder out from under a running server would "
                f"corrupt it. Current status: {status}.\n\n"
                "Stop the server from the header controls above, then try again.",
            )
            return

        if self._config_guard is not None and not self._config_guard():
            return  # The guard (ConfigTab) already explained why, if it blocked.

        reply = QMessageBox.warning(
            self, "Swap world?",
            f"This will replace the CURRENT world with '{entry.display_name}'.\n\n"
            "Before this happens, Crucible will:\n"
            " - Automatically back up the CURRENT world (always, cannot be skipped)\n"
            " - Move the current world folder aside rather than deleting it\n"
            " - Extract the chosen backup and verify it before finishing\n"
            " - Automatically roll back if anything goes wrong\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._begin_operation("swap")
        self._active_manager = self._manager
        self._thread = QThread()
        self._worker = SwapWorker(self._active_manager, entry)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_swap_done)
        self._worker.failed.connect(self._on_swap_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._thread_finished)
        self._thread.start()

    def _on_swap_done(self, result: SwapResult) -> None:
        self._op_result = ("done", result)

    def _on_swap_failed(self, error: str) -> None:
        self._op_result = ("failed", error)

    # Shared thread lifecycle

    def _begin_operation(self, kind: str) -> None:
        self._op_kind = kind
        self._op_result = None
        self._backup_btn.setEnabled(False)
        self._table.setEnabled(False)
        self._progress.setValue(0)
        self._progress_lbl.setText(
            "Creating backup..." if kind == "backup" else "Swapping worlds... do not close Crucible."
        )
        self._progress_lbl.show()
        self._progress.show()

    def _thread_finished(self) -> None:
        kind = self._op_kind
        result = self._op_result
        self._thread = None
        self._worker = None
        self._active_manager = None
        self._op_kind = None
        self._op_result = None
        self._progress_lbl.hide()
        self._progress.hide()
        self._backup_btn.setEnabled(True)
        self._table.setEnabled(True)

        if kind == "backup" and result and result[0] == "failed":
            QMessageBox.critical(self, "Backup Failed", result[1])
        elif kind == "swap" and result:
            if result[0] == "done":
                swap_result: SwapResult = result[1]
                QMessageBox.information(
                    self, "Swap complete",
                    f"{swap_result.message}\n\n"
                    "A safety backup of the world you just replaced was saved "
                    "automatically, and its previous folder was kept on disk as "
                    "an extra precaution (see the cleanup option below to remove "
                    "it later).",
                )
            elif result[0] == "failed":
                QMessageBox.critical(
                    self, "Swap Failed",
                    f"{result[1]}\n\nThe previous world has been restored -- nothing was lost.",
                )
        self._refresh()

    # Rename / delete / cleanup

    def _rename_entry(self, entry: BackupEntry) -> None:
        if not self._manager:
            return
        current = entry.slot_name or ""
        name, ok = QInputDialog.getText(
            self, "Rename world backup",
            "Slot name (leave blank to remove the name):",
            text=current,
        )
        if not ok:
            return
        self._manager.rename_slot(entry, name)
        self._refresh()

    def _confirm_delete(self, entry: BackupEntry) -> None:
        if not self._manager:
            return
        reply = QMessageBox.question(
            self, "Delete world backup",
            f"Permanently delete '{entry.display_name}'?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._manager.delete_backup(entry)
            self._refresh()

    def _cleanup_pre_swap_dirs(self) -> None:
        if not self._manager:
            return
        reply = QMessageBox.question(
            self, "Clean up pre-swap folders",
            "Delete leftover pre-swap folders older than 7 days? Folders newer "
            "than that are kept a little longer in case you need to recover from "
            "them manually.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted = self._manager.prune_pre_swap_dirs(keep_days=7)
            QMessageBox.information(self, "Cleanup complete", f"Deleted {deleted} old pre-swap folder(s).")
            self._refresh()


def _hline() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setStyleSheet(f"color: {theme.SURFACE1};")
    return sep


def _human_size(b: int) -> str:
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f} GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.0f} MB"
    return f"{b / 1024:.0f} KB"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for fpath in path.rglob("*"):
            try:
                if fpath.is_file() and not fpath.is_symlink():
                    total += fpath.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total
