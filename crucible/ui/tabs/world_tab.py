"""
crucible/ui/tabs/world_tab.py

World identification, named "world slot" backups, the safe world-swap
workflow, and quick actions to set the seed, start fresh, or permanently
delete the world (World Backup & Swap + World Actions feature).

This is deliberately separate from BackupTab (which stays a simple "one zip
per click" tool): WorldTab is where a user manages MULTIPLE named saved
worlds, safely swaps the live world between them, and can start over with a
clean world. Swapping/resetting/wiping are the highest-risk operations in
Crucible, so every precondition is enforced here and explained in plain
language before anything touches disk:

  - The server must be fully stopped (checked live via tmux; any status
    other than the exact "stopped" value blocks the operation).
  - There must be no unsaved server.properties edits (delegated to whatever
    guard is wired in via set_config_guard -- normally ConfigTab's own
    confirm_discard_or_save).
  - Swap and Reset always take an automatic safety backup of the CURRENT
    world first, with no way to skip it. Wipe is the one exception -- it is
    for users who explicitly want to free disk space -- so it requires a
    typed confirmation instead of a safety backup.
  - Every disk-heavy operation (backup, swap, reset, wipe, and the world-
    size scan itself) runs in a background thread so the GUI never freezes,
    even on huge modpacks with dozens of dimensions.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from ...data.instance_model import ServerInstance, dimension_label
from ...data.backup_manager import (
    BackupManager, BackupWorker, BackupEntry, SwapWorker, SwapResult,
    ResetWorldWorker, WipeWorldWorker,
)
from ...process.tmux_manager import TmuxManager
from .. import theme
from ..thread_lifecycle import connect_thread_cleanup

# Dimension lists longer than this are collapsed behind a "Show all" toggle --
# GTNH-style packs can have 80-90+ dimensions, which as one giant paragraph of
# text was one of the biggest readability complaints about this tab.
_DIMS_COLLAPSE_THRESHOLD = 6


class _WorldStatsWorker(QObject):
    """Computes the (potentially slow) recursive world size and pre-swap
    folder total off the GUI thread.

    GTNH-scale worlds can have 80-90+ dimension folders full of region
    files, so walking them with Path.rglob() on the main thread -- which is
    exactly what the old synchronous _refresh() did -- is what caused the
    "UI not responding" freezes whenever this tab was opened or refreshed.
    """

    done = pyqtSignal(int, int)   # world_size_bytes, presafe_total_bytes

    def __init__(self, instance: ServerInstance, pre_swap_dirs: list[Path], parent=None):
        super().__init__(parent)
        self._instance = instance
        self._pre_swap_dirs = pre_swap_dirs

    def run(self) -> None:
        try:
            size = self._instance.world_size_bytes()
        except Exception:  # noqa: BLE001 - keep a failed background size scan from crashing the GUI
            size = 0
        total = 0
        for d in self._pre_swap_dirs:
            total += _dir_size(d)
        self.done.emit(size, total)


class WorldTab(QWidget):
    """Named world-slot backups, safe world swapping, and world reset/wipe/seed for one instance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
        self._manager:  BackupManager | None  = None
        self._thread:   QThread | None        = None
        self._worker = None
        self._active_manager: BackupManager | None = None
        self._op_kind: str | None = None      # "backup" | "swap" | "reset" | "wipe"
        self._op_result = None
        self._config_guard: Callable[[], bool] | None = None
        self._config_reload: Callable[[], None] | None = None
        self._stats_thread: QThread | None = None
        self._stats_worker: _WorldStatsWorker | None = None
        self._stats_generation = 0
        # If a refresh is requested (load(), Refresh button, or a completed
        # backup/swap/reset/wipe) while a previous world-size scan is still
        # running, we must NOT touch self._stats_thread until that scan's own
        # `finished` signal fires -- overwriting the reference while its
        # QThread is still alive is exactly the "QThread: Destroyed while
        # thread is still running" abort. Instead we stash the newest
        # requested args here and let _stats_thread_finished start it.
        self._stats_pending: tuple[Path, list[Path]] | None = None
        self._dims_expanded = False
        self._build_ui()

    # Public API

    def set_config_guard(self, fn: Callable[[], bool] | None) -> None:
        """Wire a guard (e.g. ConfigTab.confirm_discard_or_save) that must
        return True before a swap/reset is allowed to proceed. Acting while
        there are unsaved server.properties edits could leave a world that
        no longer matches the properties the user thinks are active.
        """
        self._config_guard = fn

    def set_config_reload(self, fn: Callable[[], None] | None) -> None:
        """Wire a callback (e.g. ConfigTab.reload_from_disk) so that when
        Set Seed writes server.properties directly, the Config tab's own
        in-memory buffer is refreshed instead of going stale."""
        self._config_reload = fn

    def load(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._manager = BackupManager(instance)
        self._dims_expanded = False
        self._refresh()

    def has_active_operation(self) -> bool:
        thread_busy = bool(self._thread and self._thread.isRunning())
        stats_busy = bool(self._stats_thread and self._stats_thread.isRunning())
        return thread_busy or stats_busy

    # UI construction

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        def add_card(title: str, subtitle: str = ""):
            card = QFrame()
            card.setObjectName("WorldCard")
            card.setStyleSheet(
                f"QFrame#WorldCard {{ background-color: {theme.MANTLE}; "
                f"border: 1px solid {theme.SURFACE1}; border-radius: 8px; }}"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 12, 14, 12)
            card_layout.setSpacing(8)
            title_label = QLabel(title)
            title_label.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 14px; font-weight: 700; "
                "background: transparent; border: none;"
            )
            card_layout.addWidget(title_label)
            if subtitle:
                subtitle_label = QLabel(subtitle)
                subtitle_label.setWordWrap(True)
                subtitle_label.setStyleSheet(
                    f"color: {theme.SUBTEXT}; font-size: 11px; "
                    "background: transparent; border: none;"
                )
                card_layout.addWidget(subtitle_label)
            layout.addWidget(card)
            return card, card_layout

        # Active world overview: name, size, and dimensions stay together.
        _, overview = add_card("ACTIVE WORLD")
        info_row = QHBoxLayout()
        self._world_label = QLabel("World: -")
        self._world_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 15px; font-weight: 600; "
            "background: transparent;"
        )
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        refresh_btn.clicked.connect(self._refresh)
        info_row.addWidget(self._world_label, stretch=1)
        info_row.addWidget(refresh_btn)
        overview.addLayout(info_row)

        dims_row = QHBoxLayout()
        self._dims_label = QLabel("")
        self._dims_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; background: transparent;"
        )
        self._dims_label.setWordWrap(True)
        dims_row.addWidget(self._dims_label, stretch=1)
        self._dims_toggle_btn = QPushButton("Show all")
        self._dims_toggle_btn.setFixedWidth(80)
        self._dims_toggle_btn.clicked.connect(self._toggle_dims_expanded)
        self._dims_toggle_btn.hide()
        dims_row.addWidget(self._dims_toggle_btn, alignment=Qt.AlignmentFlag.AlignTop)
        overview.addLayout(dims_row)

        # One obvious, safe path to a fresh named world. Dangerous deletion is
        # deliberately tucked into Advanced instead of competing for attention.
        _, actions = add_card(
            "CREATE OR SWITCH WORLDS",
            "Create a named fresh world, save the current one, or switch to a saved world below.",
        )
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self._reset_btn = QPushButton("Create new world")
        self._reset_btn.setObjectName("PrimaryButton")
        self._reset_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder)
        )
        self._reset_btn.setMinimumHeight(40)
        self._reset_btn.setToolTip(
            "Choose a name; Crucible backs up the current world before preparing the new one."
        )
        self._reset_btn.clicked.connect(self._confirm_and_reset)

        self._backup_btn = QPushButton("Save current world")
        self._backup_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self._backup_btn.setMinimumHeight(40)
        self._backup_btn.clicked.connect(self._start_named_backup)

        self._seed_btn = QPushButton("Set seed")
        self._seed_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._seed_btn.setMinimumHeight(40)
        self._seed_btn.setToolTip(
            "Set the seed used by a newly generated world; existing chunks are unchanged."
        )
        self._seed_btn.clicked.connect(self._set_seed)

        action_row.addWidget(self._reset_btn, stretch=2)
        action_row.addWidget(self._backup_btn, stretch=2)
        action_row.addWidget(self._seed_btn, stretch=1)
        actions.addLayout(action_row)

        advanced_row = QHBoxLayout()
        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setText("Advanced world actions")
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.setChecked(False)
        self._advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._advanced_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._advanced_toggle.toggled.connect(self._toggle_advanced_actions)
        advanced_row.addWidget(self._advanced_toggle)
        advanced_row.addStretch()
        actions.addLayout(advanced_row)

        self._advanced_panel = QFrame()
        self._advanced_panel.setStyleSheet(
            f"QFrame {{ background-color: {theme.BASE}; border: 1px solid {theme.SURFACE1}; "
            "border-radius: 6px; }"
        )
        advanced_panel_layout = QHBoxLayout(self._advanced_panel)
        advanced_panel_layout.setContentsMargins(10, 8, 10, 8)
        advanced_panel_layout.addWidget(
            QLabel("Irreversible: deletes the active world without making a backup.")
        )
        self._wipe_btn = QPushButton("Permanently wipe world…")
        self._wipe_btn.setObjectName("DangerButton")
        self._wipe_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton)
        )
        self._wipe_btn.setToolTip(
            "Permanently delete the active world folder. No safety backup is kept."
        )
        self._wipe_btn.clicked.connect(self._confirm_and_wipe)
        advanced_panel_layout.addWidget(self._wipe_btn)
        self._advanced_panel.hide()
        actions.addWidget(self._advanced_panel)

        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress_lbl.hide()
        self._progress.hide()
        actions.addWidget(self._progress_lbl)
        actions.addWidget(self._progress)

        # Saved worlds are the switcher: every row has one direct, named action.
        _, slots = add_card(
            "SAVED WORLDS",
            "Your named world snapshots live here. Switching always protects the world you are leaving.",
        )
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["World name", "Size", "Saved", ""])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(3, 250)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        slots.addWidget(self._table)

        presafe_row = QHBoxLayout()
        self._presafe_label = QLabel("")
        self._presafe_label.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px; background: transparent;"
        )
        self._presafe_label.setWordWrap(True)
        self._cleanup_btn = QPushButton("Clean up old world copies")
        self._cleanup_btn.clicked.connect(self._cleanup_pre_swap_dirs)
        presafe_row.addWidget(self._presafe_label, stretch=1)
        presafe_row.addWidget(self._cleanup_btn)
        slots.addLayout(presafe_row)

    def _toggle_advanced_actions(self, expanded: bool) -> None:
        self._advanced_panel.setVisible(expanded)
        self._advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    # Refresh

    def _refresh(self) -> None:
        if not self._manager or not self._instance:
            return
        inst = self._instance
        world_root = inst.world_root_path()

        if world_root.is_dir():
            # Dimension listing is a cheap shallow directory scan (just the
            # DIM* folder names directly inside the world root) -- safe to
            # do synchronously. The expensive recursive byte-size walk is
            # kicked off separately below, on a background thread.
            dims = inst.world_dimension_dirs()
            self._world_label.setText(f"World: {world_root.name}   (calculating size\u2026)")
            self._set_dims_text(dims)
        else:
            self._world_label.setText(f"World: {world_root.name}   (not generated yet)")
            self._dims_label.setText(
                "This server has never been started, so no world folder exists yet."
            )
            self._dims_toggle_btn.hide()

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

            swap_btn = QPushButton("Switch to this")
            swap_btn.setAccessibleName(f"Switch to saved world {entry.display_name}")
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
            self._presafe_label.setText(
                f"{len(pre_swap_dirs)} old world folder copy/copies are kept for recovery "
                "(calculating size\u2026). These are an extra safety net kept alongside "
                "your world folder -- once you've confirmed your current world is good, "
                "you can delete them."
            )
            self._cleanup_btn.setEnabled(True)
        else:
            self._presafe_label.setText("No extra old world folders.")
            self._cleanup_btn.setEnabled(False)

        self._start_stats_worker(world_root, pre_swap_dirs)

    def _set_dims_text(self, dims: list[Path]) -> None:
        """Render the dimension list, collapsing long lists (GTNH-scale packs
        routinely have 80-90+ dimensions) behind a 'Show all' toggle instead
        of one unreadable wall-of-text paragraph."""
        if not dims:
            self._dims_label.setText("Dimensions found: Overworld only (no Nether/End generated yet)")
            self._dims_toggle_btn.hide()
            return

        full_parts = ", ".join(f"{d.name} ({dimension_label(d.name)})" for d in dims)
        if len(dims) <= _DIMS_COLLAPSE_THRESHOLD:
            self._dims_label.setText(f"Dimensions found: Overworld, {full_parts}")
            self._dims_toggle_btn.hide()
            return

        self._dims_toggle_btn.show()
        if self._dims_expanded:
            self._dims_label.setText(f"Dimensions found ({len(dims) + 1} total): Overworld, {full_parts}")
            self._dims_toggle_btn.setText("Show less")
        else:
            short_parts = ", ".join(
                f"{d.name} ({dimension_label(d.name)})" for d in dims[:_DIMS_COLLAPSE_THRESHOLD]
            )
            remaining = len(dims) - _DIMS_COLLAPSE_THRESHOLD
            self._dims_label.setText(
                f"Dimensions found ({len(dims) + 1} total): Overworld, {short_parts}, "
                f"and {remaining} more\u2026"
            )
            self._dims_toggle_btn.setText("Show all")

    def _toggle_dims_expanded(self) -> None:
        self._dims_expanded = not self._dims_expanded
        if self._instance is not None:
            self._set_dims_text(self._instance.world_dimension_dirs())

    # Background world-size scan

    def _start_stats_worker(self, world_root: Path, pre_swap_dirs: list[Path]) -> None:
        if not world_root.is_dir() and not pre_swap_dirs:
            return
        inst = self._instance
        if inst is None:
            return

        # NEVER overwrite self._stats_thread while its QThread is still
        # alive -- load(), the Refresh button, and post-op refreshes can all
        # reach this method, and dropping the last Python reference to a
        # still-running QThread is a fatal "Destroyed while thread is still
        # running" abort. Queue this request instead; _stats_thread_finished
        # will start it the moment the in-flight scan actually finishes.
        if self._stats_thread is not None and self._stats_thread.isRunning():
            self._stats_pending = (world_root, pre_swap_dirs)
            return

        self._stats_generation += 1
        generation = self._stats_generation

        thread = QThread()
        worker = _WorldStatsWorker(inst, pre_swap_dirs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(lambda size, total: self._on_stats_done(generation, size, total))
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        # Only clear the tracked refs if THIS thread is still the current one
        # -- a rapid Refresh click or tab re-load can start a newer scan
        # before an older one finishes, and that older thread's cleanup must
        # never clobber the newer thread's live reference.
        connect_thread_cleanup(thread, lambda t=thread: self._stats_thread_finished(t))
        self._stats_thread = thread
        self._stats_worker = worker
        thread.start()

    def _stats_thread_finished(self, thread: QThread) -> None:
        if self._stats_thread is thread:
            self._stats_thread = None
            self._stats_worker = None
            if self._stats_pending is not None:
                world_root, pre_swap_dirs = self._stats_pending
                self._stats_pending = None
                self._start_stats_worker(world_root, pre_swap_dirs)

    def _on_stats_done(self, generation: int, size: int, presafe_total: int) -> None:
        if generation != self._stats_generation or self._instance is None or self._manager is None:
            return  # stale -- instance changed or a newer refresh already started
        world_root = self._instance.world_root_path()
        if world_root.is_dir():
            self._world_label.setText(f"World: {world_root.name}   ({_human_size(size)})")
        pre_swap_dirs = self._manager.list_pre_swap_dirs()
        if pre_swap_dirs:
            self._presafe_label.setText(
                f"{len(pre_swap_dirs)} leftover pre-swap folder(s) from previous swaps/resets "
                f"({_human_size(presafe_total)}). These are an extra safety net kept alongside "
                "your world folder -- once you've confirmed your current world is good, "
                "you can delete them."
            )

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
        connect_thread_cleanup(self._thread, self._thread_finished)
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
        connect_thread_cleanup(self._thread, self._thread_finished)
        self._thread.start()

    def _on_swap_done(self, result: SwapResult) -> None:
        self._op_result = ("done", result)

    def _on_swap_failed(self, error: str) -> None:
        self._op_result = ("failed", error)

    # Set Seed action

    def _set_seed(self) -> None:
        inst = self._instance
        if inst is None:
            return
        if self._thread and self._thread.isRunning():
            return

        tmux = TmuxManager()
        status = tmux.get_status(inst)
        if status != "stopped":
            QMessageBox.critical(
                self, "Set Seed",
                "The server should be fully stopped before changing the seed -- "
                "a running server won't pick it up until restarted, and editing "
                "server.properties while the JVM might also be writing to it "
                f"risks a corrupted file. Current status: {status}.",
            )
            return
        if self._config_guard is not None and not self._config_guard():
            return

        props_path = inst.path_obj / "server.properties"
        if not props_path.exists():
            QMessageBox.warning(
                self, "Set Seed",
                "server.properties not found -- start the server at least once first.",
            )
            return

        current_seed = self._read_current_seed(props_path)
        seed, ok = QInputDialog.getText(
            self, "Set world seed",
            "New level-seed (leave blank for a random seed):",
            text=current_seed,
        )
        if not ok:
            return
        seed = seed.strip()

        reply = QMessageBox.warning(
            self, "Set world seed?",
            "This only affects terrain that hasn't been generated yet -- it will "
            "NOT change chunks that already exist in the current world. To "
            "actually see a new world generated with this seed, use "
            "\u201cCreate new world\u201d afterwards.\n\n"
            + (f"Set level-seed to '{seed}'?" if seed else
               "Continue with a blank (random) seed?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self._write_seed(props_path, seed)
        except OSError as exc:
            QMessageBox.critical(self, "Set Seed", f"Could not write server.properties:\n{exc}")
            return

        if self._config_reload is not None:
            self._config_reload()

        QMessageBox.information(
            self, "Seed updated",
            "level-seed saved. Newly-generated chunks (including a world made "
            "via \u201cCreate new world\u201d) will use this seed.",
        )

    @staticmethod
    def _read_current_seed(props_path: Path) -> str:
        try:
            for line in props_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("level-seed="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    @staticmethod
    def _write_seed(props_path: Path, seed: str) -> None:
        """Atomically rewrite (or insert) level-seed=..., leaving every other
        line -- including comments and ordering -- untouched."""
        lines = props_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("level-seed="):
                newline = "\n" if line.endswith("\n") else ""
                lines[i] = f"level-seed={seed}{newline}"
                found = True
                break
        if not found:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"level-seed={seed}\n")
        tmp = props_path.with_suffix(props_path.suffix + ".tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(props_path)

    # Create New World action

    def _confirm_and_reset(self) -> None:
        if not self._manager or not self._instance:
            return
        if self._thread and self._thread.isRunning():
            return

        tmux = TmuxManager()
        status = tmux.get_status(self._instance)
        if status != "stopped":
            QMessageBox.critical(
                self, "Reset cancelled",
                "The server must be fully stopped before resetting the world -- "
                "moving a world folder out from under a running server would "
                f"corrupt it. Current status: {status}.\n\n"
                "Stop the server from the header controls above, then try again.",
            )
            return
        if self._config_guard is not None and not self._config_guard():
            return

        current_name = self._instance.world_root_path().name
        default_name = f"{current_name} - New"
        suffix = 2
        while (
            (self._instance.path_obj / default_name).exists()
            and default_name != current_name
        ):
            default_name = f"{current_name} - New {suffix}"
            suffix += 1

        new_name, ok = QInputDialog.getText(
            self,
            "Name your new world",
            "Choose a folder name for the new world. Your current world will be saved first:",
            text=default_name,
        )
        if not ok:
            return
        try:
            new_name = BackupManager.validate_level_name(new_name)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid world name", str(exc))
            return

        target_root = self._instance.path_obj / new_name
        current_root = self._instance.world_root_path()
        if target_root.exists() and target_root.resolve() != current_root.resolve():
            QMessageBox.warning(
                self,
                "World name already exists",
                f"A folder named '{new_name}' already exists. Switch to that world or choose another name.",
            )
            return

        reply = QMessageBox.warning(
            self, "Create a new world?",
            f"Crucible will prepare a fresh world named '{new_name}'. It will not start the server.\n\n"
            "Before the next manual start, Crucible will:\n"
            " - Create a mandatory safety backup of the current world\n"
            " - Keep the old world folder on disk as an extra recovery copy\n"
            f" - Set this server's level-name to '{new_name}'\n\n"
            "The new world is generated only when you press Start yourself. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._begin_operation("reset")
        self._active_manager = self._manager
        self._thread = QThread()
        self._worker = ResetWorldWorker(self._active_manager, new_level_name=new_name)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_reset_done)
        self._worker.failed.connect(self._on_reset_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        connect_thread_cleanup(self._thread, self._thread_finished)
        self._thread.start()

    def _on_reset_done(self, result: SwapResult) -> None:
        self._op_result = ("done", result)

    def _on_reset_failed(self, error: str) -> None:
        self._op_result = ("failed", error)

    # Wipe World action

    def _confirm_and_wipe(self) -> None:
        if not self._manager or not self._instance:
            return
        if self._thread and self._thread.isRunning():
            return

        tmux = TmuxManager()
        status = tmux.get_status(self._instance)
        if status != "stopped":
            QMessageBox.critical(
                self, "Wipe cancelled",
                "The server must be fully stopped before wiping the world. "
                f"Current status: {status}.\n\nStop the server, then try again.",
            )
            return
        if self._config_guard is not None and not self._config_guard():
            return

        world_root = self._instance.world_root_path()
        if not world_root.is_dir():
            QMessageBox.information(self, "Wipe World", "There is no world folder to wipe.")
            return

        text, ok = QInputDialog.getText(
            self, "Permanently delete this world?",
            "This PERMANENTLY deletes the world folder and every dimension "
            "inside it. No backup is taken -- use this only to free disk "
            "space after you already have a backup you trust elsewhere.\n\n"
            f"Type WIPE to confirm deleting '{world_root.name}':",
        )
        if not ok:
            return
        if text.strip().upper() != "WIPE":
            QMessageBox.information(
                self, "Wipe cancelled",
                "Confirmation text did not match -- nothing was deleted.",
            )
            return

        self._begin_operation("wipe")
        self._active_manager = self._manager
        self._thread = QThread()
        self._worker = WipeWorldWorker(self._active_manager)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_wipe_done)
        self._worker.failed.connect(self._on_wipe_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        connect_thread_cleanup(self._thread, self._thread_finished)
        self._thread.start()

    def _on_wipe_done(self, result: SwapResult) -> None:
        self._op_result = ("done", result)

    def _on_wipe_failed(self, error: str) -> None:
        self._op_result = ("failed", error)

    # Shared thread lifecycle

    def _begin_operation(self, kind: str) -> None:
        self._op_kind = kind
        self._op_result = None
        self._backup_btn.setEnabled(False)
        self._seed_btn.setEnabled(False)
        self._reset_btn.setEnabled(False)
        self._wipe_btn.setEnabled(False)
        self._advanced_toggle.setEnabled(False)
        self._table.setEnabled(False)
        self._progress.setValue(0)
        labels = {
            "backup": "Creating backup...",
            "swap": "Swapping worlds... do not close Crucible.",
            "reset": "Preparing new world... do not close Crucible.",
            "wipe": "Wiping world...",
        }
        self._progress_lbl.setText(labels.get(kind, "Working..."))
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
        self._seed_btn.setEnabled(True)
        self._reset_btn.setEnabled(True)
        self._wipe_btn.setEnabled(True)
        self._advanced_toggle.setEnabled(True)
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
        elif kind == "reset" and result:
            if result[0] == "done":
                reset_result: SwapResult = result[1]
                if self._config_reload is not None:
                    self._config_reload()
                QMessageBox.information(
                    self, "New world ready",
                    f"{reset_result.message}\n\n"
                    "Your previous world is saved in Saved Worlds and its original "
                    "folder remains available under Old world copies. Start the server "
                    "manually when you're ready; Crucible will not start it for you.",
                )
            elif result[0] == "failed":
                QMessageBox.critical(self, "Reset Failed", result[1])
        elif kind == "wipe" and result:
            if result[0] == "done":
                wipe_result: SwapResult = result[1]
                QMessageBox.information(self, "World wiped", wipe_result.message)
            elif result[0] == "failed":
                QMessageBox.critical(self, "Wipe Failed", result[1])
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
            self, "Clean up old world copies",
            "Delete old world copies older than 7 days? Copies newer "
            "than that are kept a little longer in case you need to recover from "
            "them manually.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted = self._manager.prune_pre_swap_dirs(keep_days=7)
            QMessageBox.information(self, "Cleanup complete", f"Deleted {deleted} old world folder copy/copies.")
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
