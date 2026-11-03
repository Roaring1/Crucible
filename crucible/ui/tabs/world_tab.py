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

import re
import shutil
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...data.backup_manager import BackupEntry, BackupManager, SwapResult
from ...data.fleet_backup import (
    BackupAlreadyRunning,
    FleetBackupError,
    FleetBackupManager,
    fleet_operation_lock,
)
from ...data.instance_manager import InstanceManager
from ...data.instance_model import ServerInstance, dimension_label
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
        except (OSError, ValueError):
            size = 0
        total = 0
        for d in self._pre_swap_dirs:
            total += _dir_size(d)
        self.done.emit(size, total)


class _FleetWorldActionWorker(QObject):
    """Create complete fleet checkpoints around destructive world operations."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        instances: list[ServerInstance],
        instance: ServerInstance,
        action: str,
        entry: BackupEntry | None = None,
    ):
        super().__init__()
        self._instances = list(instances)
        self._instance = instance
        self._action = action
        self._entry = entry

    def run(self) -> None:
        try:
            fleet = FleetBackupManager(self._instances, TmuxManager())
            with fleet_operation_lock(fleet.root):
                self._run_locked(fleet)
        except Exception as exc:  # noqa: BLE001 - a worker must report every failure through its Qt signal.
            self.failed.emit(str(exc))

    def _run_locked(self, fleet: FleetBackupManager) -> None:
        try:
            before = fleet.run_backup(
                operation_lock_held=True,
                # Keep an external archive selected in the World table on disk
                # until it has been extracted and the post-swap generation has
                # captured the resulting server state.
                retire_legacy_sources=self._action != "swap",
            )
            manager = BackupManager(self._instance)

            # The selected server could have been started after the confirmation
            # dialog but before the fleet snapshot finished. Do not move/delete
            # any live world folders unless the process state is still positively
            # confirmed stopped at the point of mutation.
            if self._action in {"swap", "reset", "wipe", "cleanup"}:
                status = TmuxManager().get_status(self._instance)
                if status != "stopped":
                    raise RuntimeError(
                        "World changes were cancelled after the complete fleet "
                        f"generation {before.generation_id} was saved because "
                        f"the selected server is now {status!r}, not confirmed stopped. "
                        "No live world folder was changed."
                    )

            if self._action == "swap":
                if self._entry is None:
                    raise RuntimeError("A legacy world archive must be selected before swapping.")
                swapped = manager.swap_world(
                    self._entry,
                    safety_backup_verified=True,
                    retain_pre_swap_dir=True,
                    fleet_generation_id=before.generation_id,
                )
                # The selected legacy archive becomes the new live world. Take
                # a second fleet generation before retiring its ZIP or the
                # temporary prior-world directory, so both the new world and
                # the previous world are covered by the rolling restore points.
                after = None
                cleanup_warning = ""
                try:
                    after = fleet.run_backup(operation_lock_held=True)
                except FleetBackupError as exc:
                    cleanup_warning = (
                        "The world swap itself succeeded, but the follow-up fleet "
                        "generation failed. The first complete generation remains "
                        f"available. The temporary prior-world folder and selected "
                        f"legacy archive were retained for recovery. Detail: {exc}"
                    )

                if after is not None:
                    if swapped.pre_swap_dir is not None:
                        try:
                            shutil.rmtree(swapped.pre_swap_dir)
                            swapped.pre_swap_dir = None
                        except OSError as exc:
                            cleanup_warning = (
                                "The second fleet generation is verified, but the "
                                "temporary prior-world folder could not be removed: "
                                f"{swapped.pre_swap_dir}: {exc}. The source archive was kept."
                            )
                    # The second generation's normal migration cleanup
                    # retires an external legacy ZIP only when both retained
                    # generations contain the same verified archive. Never
                    # delete it directly here or mutate a generation archive.
                    swapped.fleet_generation_id = after.generation_id
                    swapped.message += (
                        f" The before/after world states are covered by complete "
                        f"fleet generations; newest generation: {after.generation_id}."
                    )
                else:
                    swapped.fleet_generation_id = before.generation_id

                if cleanup_warning:
                    swapped.message += " " + cleanup_warning
                else:
                    swapped.message += (
                        " The temporary swap folder was retired. Any separate legacy "
                        "archive source follows the two-generation checksum retention rule."
                    )
                self.finished.emit({
                    "action": "swap",
                    "result": swapped,
                    "before": before,
                    "after": after,
                    "cleanup_warning": cleanup_warning,
                })
                return

            if self._action == "reset":
                reset = manager.reset_world(
                    safety_backup_verified=True,
                    retain_pre_swap_dir=False,
                    fleet_generation_id=before.generation_id,
                )
                reset.message += (
                    f" The prior complete server state is in fleet generation "
                    f"{before.generation_id}."
                )
                self.finished.emit({
                    "action": "reset", "result": reset, "before": before
                })
                return

            if self._action == "wipe":
                wiped = manager.wipe_world(
                    safety_backup_verified=True,
                    fleet_generation_id=before.generation_id,
                )
                self.finished.emit({
                    "action": "wipe", "result": wiped, "before": before
                })
                return

            if self._action == "cleanup":
                server_root = Path(self._instance.path)
                deleted = 0
                for candidate in server_root.iterdir():
                    if (
                        candidate.is_dir()
                        and not candidate.is_symlink()
                        and re.search(
                            r"\.pre-swap-\d{8}_\d{6}_\d{6}$",
                            candidate.name,
                        )
                        and (candidate / "level.dat").is_file()
                        and not (candidate / "level.dat").is_symlink()
                    ):
                        shutil.rmtree(candidate)
                        deleted += 1
                self.finished.emit({
                    "action": "cleanup",
                    "backup": before,
                    "deleted": deleted,
                })
                return

            raise ValueError(f"Unsupported fleet world action: {self._action}")
        except Exception as exc:
            self.failed.emit(str(exc))


class WorldTab(QWidget):
    """World inventory, fleet-backed world restore/swap, reset and wipe."""

    def __init__(self, instance_manager: InstanceManager | None = None, parent=None):
        super().__init__(parent)
        self._instance_manager = instance_manager
        self._instance: ServerInstance | None = None
        self._manager:  BackupManager | None  = None
        self._thread:   QThread | None        = None
        self._worker = None
        self._active_manager: BackupManager | None = None
        self._op_kind: str | None = None      # "swap" | "reset" | "wipe" | "cleanup"
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
        layout.setSpacing(10)

        head = QLabel("World: current world")
        head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        layout.addWidget(head)

        info_row = QHBoxLayout()
        self._world_label = QLabel("World: -")
        # Was theme.SUBTEXT (low-contrast grey) -- promoted to TEXT since this
        # is primary information users specifically said was hard to read.
        self._world_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        info_row.addWidget(self._world_label)
        info_row.addStretch()
        info_row.addWidget(refresh_btn)
        layout.addLayout(info_row)

        dims_row = QHBoxLayout()
        self._dims_label = QLabel("")
        self._dims_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        self._dims_label.setWordWrap(True)
        dims_row.addWidget(self._dims_label, stretch=1)
        self._dims_toggle_btn = QPushButton("Show all")
        self._dims_toggle_btn.setFixedWidth(80)
        self._dims_toggle_btn.clicked.connect(self._toggle_dims_expanded)
        self._dims_toggle_btn.hide()
        dims_row.addWidget(self._dims_toggle_btn, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(dims_row)

        layout.addWidget(_hline())

        # ---- Quick world actions: Set Seed / Reset / Wipe ---------------
        actions_head = QLabel("World actions")
        actions_head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        layout.addWidget(actions_head)

        actions_info = QLabel(
            "Set the seed used for newly generated terrain, reset the active world, "
            "or wipe it. Reset/wipe and legacy swaps require a complete fleet checkpoint first."
        )
        actions_info.setWordWrap(True)
        actions_info.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(actions_info)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self._seed_btn = QPushButton("\U0001F331  Set Seed\u2026")
        self._seed_btn.setToolTip("Change level-seed in server.properties (affects newly-generated chunks only)")
        self._seed_btn.clicked.connect(self._set_seed)
        self._reset_btn = QPushButton("Reset world\u2026")
        self._reset_btn.setToolTip(
            "Create and verify a complete fleet checkpoint, then reset the active world. "
            "The previous world remains recoverable from the two retained generations."
        )
        self._reset_btn.clicked.connect(self._confirm_and_reset)
        self._wipe_btn = QPushButton("Wipe world\u2026")
        self._wipe_btn.setObjectName("DangerButton")
        self._wipe_btn.setToolTip(
            "After a complete fleet checkpoint is verified, permanently remove the "
            "live world folder. Recovery requires restoring that retained generation."
        )
        self._wipe_btn.clicked.connect(self._confirm_and_wipe)
        actions_row.addWidget(self._seed_btn)
        actions_row.addWidget(self._reset_btn)
        actions_row.addWidget(self._wipe_btn)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        layout.addWidget(_hline())

        # Fleet checkpoint creation and automatic scheduling live on the
        # Backups tab. World remains focused on world-specific selection,
        # restoration, seed, reset and cleanup operations.
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

        slots_head = QLabel("Saved world archives")
        slots_head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        layout.addWidget(slots_head)

        slots_info = QLabel(
            "World-only archives from older Crucible versions are migrated into the "
            "complete fleet generations. They stay selectable here after migration; "
            "the two-generation fleet retention policy controls their lifetime."
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
        self._cleanup_btn = QPushButton("Snapshot, then clean up old world copies")
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

        # Older Crucible world ZIPs are migrated into the retained fleet
        # generations. Prefer a still-present legacy source while it is being
        # mirrored into both generations; once that source is retired, the
        # identical archive remains selectable directly from the latest
        # immutable fleet generation.
        entries = self._manager.list_backups()
        external_names = {entry.filename for entry in entries}
        try:
            fleet_archives = FleetBackupManager(
                [inst], TmuxManager()
            ).list_legacy_world_archives(inst.id)
        except (FleetBackupError, OSError, ValueError):
            fleet_archives = []
        entries.extend(
            entry for entry in fleet_archives
            if entry.filename not in external_names
        )
        entries.sort(key=lambda entry: entry.created_at, reverse=True)

        self._table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name_item = QTableWidgetItem(entry.display_name)
            fleet_generation_id = getattr(entry, "fleet_generation_id", None)
            if fleet_generation_id:
                name_item.setToolTip(
                    f"Verified immutable fleet archive from generation {fleet_generation_id}."
                )
            elif not entry.slot_name:
                name_item.setToolTip("Legacy world archive awaiting fleet migration.")
            else:
                name_item.setToolTip(
                    "Legacy world archive. It will be retired from this folder after "
                    "two fleet generations verify its contents."
                )
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

            actions_layout.addWidget(swap_btn)

            # Fleet-generation archives are immutable members of a complete
            # restore point. There is deliberately no per-row delete action:
            # that would imply a per-world retention exception. The rolling
            # two-generation policy governs their lifetime.
            if not getattr(entry, "fleet_generation_id", None):
                rename_btn = QPushButton("Rename")
                rename_btn.setAccessibleName(f"Rename legacy world archive {entry.display_name}")
                rename_btn.setToolTip(
                    "Change the saved-world label while this legacy source is being migrated."
                )
                rename_btn.clicked.connect(
                    lambda _checked=False, e=entry: self._rename_entry(e)
                )
                actions_layout.addWidget(rename_btn)
            self._table.setCellWidget(row, 3, actions)
            self._table.setRowHeight(row, 32)

        pre_swap_dirs = self._manager.list_pre_swap_dirs()
        if pre_swap_dirs:
            self._presafe_label.setText(
                f"{len(pre_swap_dirs)} leftover pre-swap folder(s) from previous swaps/resets "
                "(calculating size\u2026). These are an extra safety net kept alongside "
                "your world folder -- once you've confirmed your current world is good, "
                "you can delete them."
            )
            self._cleanup_btn.setEnabled(True)
        else:
            self._presafe_label.setText("No leftover pre-swap folders.")
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

    def _start_fleet_action(self, action: str, entry: BackupEntry | None = None) -> None:
        if not self._instance_manager or not self._instance:
            QMessageBox.warning(
                self, "Fleet backup",
                "The server registry is unavailable; no world action was started.",
            )
            return
        if self._thread and self._thread.isRunning():
            return

        self._begin_operation(action)
        self._active_manager = self._manager
        self._thread = QThread()
        self._thread.setObjectName("CrucibleFleetWorldAction")
        self._worker = _FleetWorldActionWorker(
            list(self._instance_manager.instances),
            self._instance,
            action,
            entry,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_fleet_action_done)
        self._worker.failed.connect(self._on_fleet_action_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        connect_thread_cleanup(self._thread, self._thread_finished)
        self._thread.start()

    def _on_fleet_action_done(self, result: dict) -> None:
        self._op_result = ("done", result)

    def _on_fleet_action_failed(self, error: str) -> None:
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
            "Crucible will:\n"
            " - Create a complete fleet generation for every registered server\n"
            " - Extract and verify the selected legacy world archive\n"
            " - Create a second complete fleet generation containing the new world "
            "and the previous world\n"
            " - Retire the now-redundant legacy archive and temporary old-world folder\n"
            " - Keep the newest two complete fleet generations\n"
            "If the second generation fails, the old-world folder and selected archive "
            "are kept for recovery.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._start_fleet_action("swap", entry)

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
            "\u201cReset World\u201d afterwards.\n\n"
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
            "via \u201cReset World\u201d) will use this seed.",
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

    # Reset World action

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

        reply = QMessageBox.warning(
            self, "Reset world?",
            "This will start a brand new world on the next server start.\n\n"
            "Crucible will first create and verify a complete fleet generation "
            "covering every registered server and all recoverable world saves. "
            "Only after that succeeds will it remove the active world folder. "
            "The prior world remains recoverable from the fleet generation, and "
            "the next server start will generate a fresh world.\n\n"
            "Tip: use \u201cSet Seed\u2026\u201d above first if you want the new world "
            "generated from a specific seed.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._start_fleet_action("reset")

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

        reply = QMessageBox.warning(
            self,
            "Back up and permanently wipe this world?",
            f"Crucible will first create a complete fleet generation. Only after "
            f"that generation is verified will it permanently delete '{world_root.name}'. "
            "This cannot be undone from the live server folder; recovery requires "
            "restoring the fleet generation to a separate location.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        text, ok = QInputDialog.getText(
            self, "Confirm permanent world deletion",
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

        self._start_fleet_action("wipe")

    def _on_wipe_done(self, result: SwapResult) -> None:
        self._op_result = ("done", result)

    def _on_wipe_failed(self, error: str) -> None:
        self._op_result = ("failed", error)

    # Shared thread lifecycle

    def _begin_operation(self, kind: str) -> None:
        self._op_kind = kind
        self._op_result = None
        self._seed_btn.setEnabled(False)
        self._reset_btn.setEnabled(False)
        self._wipe_btn.setEnabled(False)
        self._table.setEnabled(False)
        self._progress.setRange(0, 0)
        labels = {
            "swap": "Creating fleet checkpoints and swapping worlds...",
            "reset": "Creating a fleet checkpoint and resetting the world...",
            "wipe": "Creating a fleet checkpoint and wiping the world...",
            "cleanup": "Creating a fleet checkpoint before removing old world copies...",
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
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._seed_btn.setEnabled(True)
        self._reset_btn.setEnabled(True)
        self._wipe_btn.setEnabled(True)
        self._table.setEnabled(True)

        if result and result[0] == "failed":
            title = {
                "swap": "World swap failed",
                "reset": "World reset failed",
                "wipe": "World wipe failed",
                "cleanup": "World cleanup failed",
            }.get(kind or "", "World operation failed")
            detail = str(result[1])
            if kind == "swap":
                detail += (
                    "\n\nAny complete fleet generation already published remains available. "
                    "Check the operation details before retrying."
                )
            QMessageBox.critical(self, title, detail)
        elif result and result[0] == "done":
            payload = result[1]
            action = payload.get("action") if isinstance(payload, dict) else kind

            if action == "swap":
                swap_result: SwapResult = payload["result"]
                QMessageBox.information(
                    self, "Swap complete",
                    f"{swap_result.message}\n\n"
                    "The swap used complete fleet generations rather than an additional "
                    "per-server ZIP backup.",
                )
            elif action == "reset":
                reset_result: SwapResult = payload["result"]
                QMessageBox.information(
                    self, "World reset",
                    f"{reset_result.message}\n\nStart the server to generate the new world.",
                )
            elif action == "wipe":
                wipe_result: SwapResult = payload["result"]
                QMessageBox.information(self, "World wiped", wipe_result.message)
            elif action == "cleanup":
                backup = payload["backup"]
                QMessageBox.information(
                    self, "World cleanup complete",
                    f"Fleet generation {backup.generation_id} was verified before cleanup. "
                    f"Removed {payload['deleted']} redundant pre-swap world folder(s).",
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
        try:
            with fleet_operation_lock():
                self._manager.rename_slot(entry, name)
        except (BackupAlreadyRunning, FleetBackupError, OSError, ValueError) as exc:
            QMessageBox.warning(
                self, "World archive is busy",
                f"Crucible could not safely change this archive label while a "
                f"fleet backup or other operation is running.\n\n{exc}",
            )
            return
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
            try:
                with fleet_operation_lock():
                    self._manager.delete_backup(entry)
            except (BackupAlreadyRunning, FleetBackupError, OSError, ValueError) as exc:
                QMessageBox.warning(
                    self, "World archive is busy",
                    f"Crucible could not safely delete this archive while a fleet "
                    f"backup or other operation is running.\\n\\n{exc}",
                )
                return
            self._refresh()

    def _cleanup_pre_swap_dirs(self) -> None:
        if not self._manager or not self._instance_manager or not self._instance:
            return
        status = TmuxManager().get_status(self._instance)
        if status != "stopped":
            QMessageBox.warning(
                self,
                "World cleanup unavailable",
                "Old world folders can only be retired while the selected server "
                f"is confirmed stopped. Current status: {status}.",
            )
            return
        reply = QMessageBox.warning(
            self, "Snapshot before cleanup",
            "Crucible will create a complete fleet generation first. It will then "
            "remove old top-level pre-swap world folders that are included in that "
            "verified generation. If the generation fails, nothing is deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._start_fleet_action("cleanup")


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
