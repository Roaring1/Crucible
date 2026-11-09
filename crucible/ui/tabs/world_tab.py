"""
crucible/ui/tabs/world_tab.py

The Worlds page is the single home for the active world, saved worlds,
automatic recovery points, manual backups, and safe restore/switch actions.
The fleet snapshot engine remains shared across servers, while this page
presents recovery in the context of the worlds a person actually manages.

World swaps, restores, resets, and wipes are high-risk operations. They use
the shared fleet lock, require the server to be confirmed stopped before
changing world folders, verify the selected recovery source, and preserve a
rollback path before touching the active world.

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
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QEvent, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QDialog,
    QLineEdit,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...data.backup_manager import (
    BackupEntry,
    BackupManager,
    SwapResult,
)
from ...data.fleet_backup import (
    DEFAULT_FLEET_BACKUP_DIR,
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
from .backup_tab import _FleetBackupWorker

# Dimension lists longer than this are collapsed behind a "Show all" toggle --
# GTNH-style packs can have 80-90+ dimensions, which as one giant paragraph of
# text was one of the biggest readability complaints about this tab.
_DIMS_COLLAPSE_THRESHOLD = 6


class _WorldStatsWorker(QObject):
    """Compute the active world's recursive size away from the GUI thread.

    GTNH-scale worlds can have 80-90+ dimension folders full of region
    files, so walking them with Path.rglob() on the main thread would freeze
    the workspace whenever Worlds opened or refreshed.
    """

    done = pyqtSignal(int)  # world_size_bytes

    def __init__(self, instance: ServerInstance, parent=None):
        super().__init__(parent)
        self._instance = instance

    def run(self) -> None:
        try:
            size = self._instance.world_size_bytes()
        except Exception:  # noqa: BLE001 - keep a failed background size scan from crashing the GUI
            size = 0
        self.done.emit(size)


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
        new_level_name: str | None = None,
        world_recovery: dict[str, Any] | None = None,
    ):
        super().__init__()
        self._instances = list(instances)
        self._instance = instance
        self._action = action
        self._entry = entry
        self._new_level_name = new_level_name
        self._world_recovery = dict(world_recovery) if world_recovery else None

    def run(self) -> None:
        try:
            fleet = FleetBackupManager(self._instances, TmuxManager())
            with fleet_operation_lock(fleet.root):
                if self._world_recovery is not None:
                    # Export the selected immutable snapshot before the next
                    # fleet capture can rotate it out of the two-generation
                    # window. The temporary archive is only a bridge into the
                    # existing validated, rollback-safe world swap.
                    with tempfile.TemporaryDirectory(
                        prefix="crucible-world-restore-"
                    ) as temporary:
                        world_name = str(self._world_recovery["world_name"])
                        archive = fleet.export_world_archive(
                            str(self._world_recovery["generation_id"]),
                            str(self._instance.id),
                            world_name,
                            Path(temporary) / "selected-world.zip",
                            deep_verify=True,
                            legacy_archive_filename=self._world_recovery.get(
                                "legacy_archive_filename"
                            ),
                        )
                        created_at = datetime.fromisoformat(
                            str(archive["created_at"])
                        ).astimezone()
                        self._entry = BackupEntry(
                            filename=str(archive["filename"]),
                            path=Path(archive["path"]),
                            size_bytes=int(archive["size_bytes"]),
                            created_at=created_at,
                            slot_name=world_name,
                            sha256=str(archive["sha256"]),
                            fleet_generation_id=str(archive["generation_id"]),
                        )
                        self._run_locked(fleet)
                else:
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
                    new_level_name=self._new_level_name,
                    safety_backup_verified=True,
                    fleet_generation_id=before.generation_id,
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

            if self._action == "backup":
                self.finished.emit({"action": "backup", "backup": before})
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
        except Exception as exc:  # noqa: BLE001 - worker boundary must report every failure through its Qt signal.
            self.failed.emit(str(exc))


class _WipeWorldConfirmDialog(QDialog):
    """Bounded, case-sensitive confirmation gate for the irreversible world wipe."""

    def __init__(self, world_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("WipeWorldConfirmDialog")
        self.setWindowTitle("Confirm world wipe")
        self.setModal(True)
        self.setSizeGripEnabled(False)
        if parent is not None:
            available_width = max(420, parent.width() - 48)
            dialog_width = min(560, available_width)
        else:
            dialog_width = 560
        self.setFixedWidth(dialog_width)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("Permanently delete this world?")
        title.setObjectName("WipeDialogTitle")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT};"
        )
        title.setWordWrap(True)
        layout.addWidget(title)

        world_label = QLabel(f"World folder: {world_name}")
        world_label.setObjectName("WipeWorldName")
        world_label.setWordWrap(True)
        world_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        world_label.setStyleSheet(f"color: {theme.SUBTEXT};")
        layout.addWidget(world_label)

        warning = QFrame()
        warning.setObjectName("WipeWarningPanel")
        warning.setStyleSheet(
            f"QFrame#WipeWarningPanel {{ background-color: {theme.MANTLE}; "
            f"border: 1px solid {theme.SURFACE1}; border-left: 3px solid {theme.RED}; "
            "border-radius: 4px; }"
            "QFrame#WipeWarningPanel QLabel { border: none; background: transparent; }"
        )
        warning_layout = QVBoxLayout(warning)
        warning_layout.setContentsMargins(12, 10, 12, 10)
        warning_layout.setSpacing(5)

        warning_title = QLabel("A complete fleet backup is created first")
        warning_title.setStyleSheet(
            f"color: {theme.RED}; font-weight: 700; border: none;"
        )
        warning_title.setWordWrap(True)
        warning_layout.addWidget(warning_title)

        warning_body = QLabel(
            "Crucible creates and verifies a complete fleet backup before deleting "
            "the world folder, including every dimension and its saved data. "
            "If backup creation or verification fails, the wipe does not start."
        )
        warning_body.setObjectName("WipeWarningBody")
        warning_body.setWordWrap(True)
        warning_body.setMinimumWidth(0)
        warning_layout.addWidget(warning_body)
        layout.addWidget(warning)

        instruction = QLabel("Type exactly WIPE to enable permanent deletion:")
        instruction.setObjectName("WipeConfirmationInstruction")
        instruction.setWordWrap(True)
        instruction.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        layout.addWidget(instruction)

        self._confirmation_edit = QLineEdit()
        self._confirmation_edit.setObjectName("WipeConfirmationInput")
        self._confirmation_edit.setAccessibleName("Type WIPE to confirm world deletion")
        self._confirmation_edit.setPlaceholderText("WIPE")
        self._confirmation_edit.setClearButtonEnabled(True)
        self._confirmation_edit.setStyleSheet(
            f"QLineEdit#WipeConfirmationInput:focus {{ border: 2px solid {theme.ACCENT}; "
            "padding: 4px 7px; }"
        )
        layout.addWidget(self._confirmation_edit)

        self._validation_label = QLabel("Enter the exact uppercase text WIPE.")
        self._validation_label.setObjectName("WipeConfirmationValidation")
        self._validation_label.setStyleSheet(f"color: {theme.RED}; font-size: 11px;")
        self._validation_label.setWordWrap(True)
        self._validation_label.hide()
        layout.addWidget(self._validation_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setObjectName("WipeCancelButton")
        self._cancel_button.setDefault(True)
        self._cancel_button.setAutoDefault(True)
        self._cancel_button.setStyleSheet(
            f"QPushButton#WipeCancelButton:focus {{ border: 2px solid {theme.ACCENT}; "
            "padding: 3px 11px; }"
        )
        self._cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_button)

        self._confirm_button = QPushButton("Permanently delete world")
        self._confirm_button.setObjectName("DangerButton")
        self._confirm_button.setEnabled(False)
        self._confirm_button.setDefault(False)
        self._confirm_button.setAutoDefault(False)
        self._confirm_button.setStyleSheet(
            f"QPushButton#DangerButton:focus {{ border: 2px solid {theme.ACCENT}; "
            "padding: 3px 11px; }"
        )
        self._confirm_button.clicked.connect(self._try_accept)
        buttons.addWidget(self._confirm_button)
        layout.addLayout(buttons)

        # Compute height after all wrapped labels and layouts are present. This
        # ensures the warning body is not clipped at either dialog width.
        self.adjustSize()

        # Make the safe action the initial focus and define a stable keyboard route.
        self.setTabOrder(self._cancel_button, self._confirmation_edit)
        self.setTabOrder(self._confirmation_edit, self._confirm_button)
        self.setTabOrder(self._confirm_button, self._cancel_button)
        self._cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._confirmation_edit.textChanged.connect(self._on_confirmation_changed)
        self._confirmation_edit.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self._confirmation_edit
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            # Never let Return leak to QDialog's default Cancel button while the
            # user is typing. Exact WIPE accepts; any other text stays in place.
            self._try_accept()
            return True
        return super().eventFilter(watched, event)

    @property
    def confirmation_text(self) -> str:
        return self._confirmation_edit.text()

    def _on_confirmation_changed(self, value: str) -> None:
        exact = value == "WIPE"
        self._confirm_button.setEnabled(exact)
        self._validation_label.setVisible(bool(value) and not exact)

    def _try_accept(self) -> None:
        if self._confirmation_edit.text() != "WIPE":
            self._validation_label.show()
            self._confirmation_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        super().accept()

    def accept(self) -> None:
        # Guard every accept path, not only clicks on the destructive button.
        self._try_accept()


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
        self._stats_pending: Path | None = None
        self._dims_expanded = False
        self._recovery_thread: QThread | None = None
        self._recovery_worker: _FleetBackupWorker | None = None
        self._recovery_job_mode: str | None = None
        self._recovery_generations = []
        self._world_recovery_points: dict[str, list[Any]] = {}
        self._recovery_scheduler: dict[str, str] = {}
        self._recovery_state_data: dict[str, Any] = {}
        self._timer_enabled = False
        # The initial refresh can finish rendering before the background
        # recovery-list worker returns. Keep its ZIP metadata projection so
        # that the worker's follow-up render does not reopen every archive on
        # the GUI thread. Explicit refreshes and archive mutations rebuild it.
        self._archive_inventory_instance_id: str | None = None
        self._archive_inventory_entries: list[tuple[BackupEntry, str]] = []
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
        self._archive_inventory_instance_id = None
        self._archive_inventory_entries = []
        self._refresh()
        self._run_recovery_job("list")

    def has_active_operation(self) -> bool:
        thread_busy = bool(self._thread and self._thread.isRunning())
        stats_busy = bool(self._stats_thread and self._stats_thread.isRunning())
        recovery_busy = bool(
            self._recovery_thread and self._recovery_thread.isRunning()
        )
        return thread_busy or stats_busy or recovery_busy

    def _server_restore_generations(self) -> list[Any]:
        if self._instance is None:
            return []
        instance_id = str(self._instance.id)
        return [
            item for item in self._recovery_generations
            if instance_id in getattr(item, "server_ids", ())
        ]

    def _run_recovery_job(
        self,
        mode: str,
        *,
        restore_args: tuple[str, str, Path, str] | None = None,
    ) -> None:
        if not self._instance_manager or not self._instance:
            return
        if (
            self._thread and self._thread.isRunning()
            or self._recovery_thread and self._recovery_thread.isRunning()
        ):
            return
        instances = list(self._instance_manager.instances)
        if not instances:
            return

        self._recovery_job_mode = mode
        self._set_recovery_busy(True, mode)
        self._recovery_thread = QThread()
        self._recovery_thread.setObjectName("CrucibleWorldRecoveryThread")
        self._recovery_worker = _FleetBackupWorker(
            instances, mode, restore_args=restore_args
        )
        self._recovery_worker.moveToThread(self._recovery_thread)
        self._recovery_thread.started.connect(self._recovery_worker.run)
        self._recovery_worker.finished.connect(self._on_recovery_job_finished)
        self._recovery_worker.failed.connect(self._on_recovery_job_failed)
        self._recovery_worker.finished.connect(self._recovery_thread.quit)
        self._recovery_worker.failed.connect(self._recovery_thread.quit)
        self._recovery_worker.finished.connect(self._recovery_worker.deleteLater)
        self._recovery_worker.failed.connect(self._recovery_worker.deleteLater)
        connect_thread_cleanup(
            self._recovery_thread, self._recovery_thread_finished
        )
        self._recovery_thread.start()

    def _set_recovery_busy(self, busy: bool, mode: str = "") -> None:
        for button in (
            self._backup_all_btn,
            self._verify_backups_btn,
            self._schedule_btn,
            self._open_folder_btn,
        ):
            button.setEnabled(not busy)
        self._restore_server_btn.setEnabled(
            not busy and bool(self._server_restore_generations())
        )
        if busy:
            labels = {
                "list": "Checking backups…",
                "verify": "Verifying backups…",
                "install_timer": "Enabling automatic backups…",
                "remove_timer": "Pausing automatic backups…",
                "restore": "Restoring server copy…",
            }
            self._recovery_state.setText(labels.get(mode, "Working…"))
        else:
            self._render_recovery_status(self._recovery_state_data)

    def _on_recovery_job_finished(self, result: Any) -> None:
        mode = self._recovery_job_mode
        if mode == "list":
            self._recovery_generations = list(result.get("generations", []))
            self._recovery_scheduler = dict(result.get("scheduler", {}))
            self._recovery_state_data = dict(result.get("state", {}))
            by_server = result.get("world_recovery_points", {})
            self._world_recovery_points = dict(
                by_server.get(str(self._instance.id), {})
            )
            self._render_recovery_status(self._recovery_state_data)
            # load() and post-operation refresh have already inventoried local
            # ZIPs. Re-render with the newly returned fleet restore points
            # without reopening every ZIP central directory on the GUI thread.
            self._refresh(refresh_archives=False)
        elif mode == "verify":
            failures = [item for item in result if not item.get("ok")]
            if failures:
                details = "\n".join(
                    f"{Path(item['path']).name}: "
                    + "; ".join(item.get("errors", [])[:3])
                    for item in failures
                )
                QMessageBox.warning(
                    self, "Backup verification", f"Some backups need attention:\n\n{details}"
                )
            else:
                QMessageBox.information(
                    self,
                    "Backups verified",
                    f"All {len(result)} retained restore points passed SHA-256 verification.",
                )
        elif mode == "install_timer":
            QMessageBox.information(
                self,
                "Automatic backups enabled",
                "Crucible will back up every registered server about every 10 minutes.",
            )
        elif mode == "remove_timer":
            QMessageBox.information(
                self,
                "Automatic backups paused",
                "Existing restore points are unchanged.",
            )
        elif mode == "restore":
            QMessageBox.information(
                self,
                "Server restored",
                f"{result['server_name']} was restored to a separate folder:\n\n"
                f"{result['target']}\n\n"
                "The live registered server was not changed. Start the restored copy manually.",
            )

    def _on_recovery_job_failed(self, error: str) -> None:
        mode = self._recovery_job_mode
        self._recovery_state_data = {
            **self._recovery_state_data,
            "last_status": "failed",
        }
        self._recovery_state.setText("Backup operation needs attention")
        if mode == "restore":
            title = "Server restore failed"
        elif mode == "verify":
            title = "Backup verification failed"
        elif mode in {"install_timer", "remove_timer"}:
            title = "Automatic backup schedule"
        else:
            title = "Could not load recovery points"
        QMessageBox.warning(self, title, error)

    def _recovery_thread_finished(self) -> None:
        mode = self._recovery_job_mode
        self._recovery_thread = None
        self._recovery_worker = None
        self._recovery_job_mode = None
        self._set_recovery_busy(False)
        if mode in {"verify", "install_timer", "remove_timer", "restore"}:
            QTimer.singleShot(100, lambda: self._run_recovery_job("list"))

    def _render_recovery_status(self, state: dict[str, Any] | None = None) -> None:
        state = state or {}
        active = self._recovery_scheduler.get("active") == "true"
        enabled = self._recovery_scheduler.get("enabled") == "true"
        self._timer_enabled = active or enabled
        if active:
            self._recovery_state.setText("Automatic · every 10 min · 2 restore points")
            self._schedule_btn.setText("Pause")
            self._schedule_btn.setAccessibleName("Pause automatic backups")
            self._schedule_btn.setIcon(
                QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause)
            )
        elif enabled:
            self._recovery_state.setText("Schedule enabled · timer not running")
            self._schedule_btn.setText("Disable")
            self._schedule_btn.setAccessibleName("Disable automatic backups")
            self._schedule_btn.setIcon(
                QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
            )
        else:
            self._recovery_state.setText("Automatic backups off")
            self._schedule_btn.setText("Enable")
            self._schedule_btn.setAccessibleName("Enable automatic backups")
            self._schedule_btn.setIcon(
                QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            )

        last_completed = (
            state.get("last_completed_at_utc")
            or (self._recovery_generations[0].created_at if self._recovery_generations else None)
        )
        self._recovery_last.setText(
            f"Last: {_friendly_datetime(last_completed)}" if last_completed else "No backup yet"
        )
        last_status = state.get("last_status")
        if last_status == "failed":
            self._recovery_state.setText(
                self._recovery_state.text() + " · last run failed"
            )
            self._recovery_state.setStyleSheet(
                f"color: {theme.YELLOW}; font-size: 12px; font-weight: 600; background: transparent;"
            )
        else:
            self._recovery_state.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 12px; font-weight: 600; background: transparent;"
            )

    def _confirm_backup_all(self) -> None:
        if not self._instance_manager or not self._instance_manager.instances:
            QMessageBox.information(self, "Back up servers", "No servers are registered.")
            return
        reply = QMessageBox.question(
            self,
            "Back up all servers?",
            "Create one verified recovery point for every registered server and world? "
            "Running servers are safely flushed. Crucible will not start or stop servers.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._start_fleet_action("backup")

    def _toggle_backup_schedule(self) -> None:
        if self._timer_enabled:
            reply = QMessageBox.question(
                self,
                "Pause automatic backups?",
                "Pause the ten-minute schedule? Existing restore points will be kept.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._run_recovery_job("remove_timer")
        else:
            self._run_recovery_job("install_timer")

    def _open_backup_folder(self) -> None:
        DEFAULT_FLEET_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(DEFAULT_FLEET_BACKUP_DIR))
        )

    def _restore_full_server(self) -> None:
        generations = self._server_restore_generations()
        if self._instance is None or not generations:
            QMessageBox.information(
                self,
                "Restore server",
                "No retained full-server restore point includes this server yet.",
            )
            return
        labels = [
            f"{_friendly_datetime(item.created_at)} · {item.server_count} servers"
            for item in generations
        ]
        choice, ok = QInputDialog.getItem(
            self, "Restore full server", "Choose a restore point:", labels, 0, False
        )
        if not ok or choice not in labels:
            return
        generation = generations[labels.index(choice)]
        parent_text = QFileDialog.getExistingDirectory(
            self,
            "Choose where to restore the server copy",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not parent_text:
            return
        safe_name = "".join(
            char if char.isalnum() or char in "._-" else "-"
            for char in self._instance.name
        ).strip(".-") or "Crucible-server"
        target = Path(parent_text) / (
            f"{safe_name}-restored-{generation.generation_id[-8:]}"
        )
        if target.exists() or target.is_symlink():
            QMessageBox.warning(
                self, "Restore destination exists",
                f"Crucible will not overwrite an existing folder:\n\n{target}",
            )
            return
        reply = QMessageBox.question(
            self,
            "Restore a full server copy?",
            f"Restore {self._instance.name} from {_friendly_datetime(generation.created_at)} "
            f"to this new folder?\n\n{target}\n\n"
            "The registered server and its live folder will not be changed. "
            "The restored copy will remain stopped until you start it yourself.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._run_recovery_job(
            "restore",
            restore_args=(
                generation.generation_id,
                str(self._instance.id),
                target,
                self._instance.name,
            ),
        )

    # UI construction

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        scroll.setWidget(content)
        root_layout.addWidget(scroll)

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
        self._world_label.setAccessibleName("Selected server active world")
        self._world_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 15px; font-weight: 600; "
            "background: transparent;"
        )
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setAccessibleName("Refresh selected server world inventory")
        refresh_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        refresh_btn.setMinimumWidth(refresh_btn.sizeHint().width() + 3)
        refresh_btn.setMinimumHeight(32)
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
        self._dims_toggle_btn.setAccessibleName("Show all world dimensions")
        self._dims_toggle_btn.setFixedWidth(80)
        self._dims_toggle_btn.clicked.connect(self._toggle_dims_expanded)
        self._dims_toggle_btn.hide()
        dims_row.addWidget(self._dims_toggle_btn, alignment=Qt.AlignmentFlag.AlignTop)
        overview.addLayout(dims_row)

        # One obvious, safe path to a fresh named world. Dangerous deletion is
        # deliberately tucked into Advanced instead of competing for attention.
        _, actions = add_card(
            "WORLD ACTIONS",
            "Create a fresh world or switch to one below.",
        )
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self._reset_btn = QPushButton("Create new world")
        self._reset_btn.setObjectName("PrimaryButton")
        self._reset_btn.setAccessibleName("Reset the active world after a verified fleet checkpoint")
        self._reset_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder)
        )
        self._reset_btn.setMinimumHeight(40)
        self._reset_btn.setToolTip(
            "Choose a name; Crucible backs up the current world before preparing the new one."
        )
        self._reset_btn.clicked.connect(self._confirm_and_reset)

        self._seed_btn = QPushButton("Set seed")
        self._seed_btn.setAccessibleName("Change the active world seed for newly generated chunks")
        self._seed_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._seed_btn.setMinimumHeight(40)
        self._seed_btn.setToolTip(
            "Set the seed used by a newly generated world; existing chunks are unchanged."
        )
        self._seed_btn.clicked.connect(self._set_seed)

        action_row.addWidget(self._reset_btn, stretch=2)
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
        self._wipe_btn.setAccessibleName("Permanently wipe the active world after a verified fleet checkpoint")
        self._wipe_btn.setObjectName("DangerButton")
        self._wipe_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton)
        )
        self._wipe_btn.setToolTip(
            "Crucible first creates and verifies a complete fleet checkpoint, then deletes the active world folder."
        )
        self._wipe_btn.clicked.connect(self._confirm_and_wipe)
        advanced_panel_layout.addWidget(self._wipe_btn)

        self._presafe_label = QLabel("")
        self._presafe_label.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px; background: transparent;"
        )
        self._presafe_label.setWordWrap(True)
        self._cleanup_btn = QPushButton("Clean up old world copies")
        self._cleanup_btn.setAccessibleName(
            "Create and verify a fleet snapshot before cleaning old world copies"
        )
        self._cleanup_btn.clicked.connect(self._cleanup_pre_swap_dirs)
        advanced_panel_layout.addWidget(self._presafe_label, stretch=1)
        advanced_panel_layout.addWidget(self._cleanup_btn)

        self._advanced_panel.hide()
        actions.addWidget(self._advanced_panel)

        # Recovery belongs beside worlds. Keep the normal view to one status
        # line and a handful of direct actions; generation IDs and file-level
        # diagnostics stay in explicit details/verification flows.
        _, recovery = add_card("RECOVERY")
        recovery_header = QHBoxLayout()
        self._recovery_state = QLabel("Checking automatic backups…")
        self._recovery_state.setAccessibleName("Automatic backup status")
        self._recovery_state.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        recovery_header.addWidget(self._recovery_state, stretch=1)
        self._recovery_last = QLabel(" ")
        self._recovery_last.setAccessibleName("Last completed backup")
        self._recovery_last.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px; background: transparent;"
        )
        recovery_header.addWidget(self._recovery_last)
        recovery.addLayout(recovery_header)

        recovery_actions = QHBoxLayout()
        recovery_actions.setSpacing(8)
        self._backup_all_btn = QPushButton("Back up all servers now")
        self._backup_all_btn.setObjectName("PrimaryButton")
        self._backup_all_btn.setAccessibleName("Back up all registered servers now")
        self._backup_all_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self._backup_all_btn.setMinimumHeight(36)
        self._backup_all_btn.setToolTip(
            "Create a complete recovery point for every registered server and world."
        )
        self._backup_all_btn.clicked.connect(self._confirm_backup_all)

        self._verify_backups_btn = QPushButton("Verify")
        self._verify_backups_btn.setAccessibleName("Verify retained automatic backups")
        self._verify_backups_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
        )
        self._verify_backups_btn.setMinimumHeight(36)
        self._verify_backups_btn.setToolTip(
            "Check every retained backup file against its SHA-256 manifest."
        )
        self._verify_backups_btn.clicked.connect(
            lambda: self._run_recovery_job("verify")
        )

        self._restore_server_btn = QPushButton("Restore full server…")
        self._restore_server_btn.setAccessibleName("Restore a complete server backup to a new folder")
        self._restore_server_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._restore_server_btn.setMinimumHeight(36)
        self._restore_server_btn.clicked.connect(self._restore_full_server)

        self._schedule_btn = QPushButton("Pause")
        self._schedule_btn.setAccessibleName("Pause automatic backups")
        self._schedule_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        )
        self._schedule_btn.setMinimumHeight(36)
        self._schedule_btn.setToolTip("Pause or resume the ten-minute automatic backup schedule.")
        self._schedule_btn.clicked.connect(self._toggle_backup_schedule)

        self._open_folder_btn = QToolButton()
        self._open_folder_btn.setAccessibleName("Open Crucible backup storage folder")
        self._open_folder_btn.setToolTip("Open backup storage folder")
        self._open_folder_btn.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self._open_folder_btn.setMinimumSize(36, 36)
        self._open_folder_btn.clicked.connect(self._open_backup_folder)

        recovery_actions.addWidget(self._backup_all_btn)
        recovery_actions.addWidget(self._verify_backups_btn)
        recovery_actions.addWidget(self._restore_server_btn)
        recovery_actions.addWidget(self._schedule_btn)
        recovery_actions.addWidget(self._open_folder_btn)
        recovery.addLayout(recovery_actions)

        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        self._progress = QProgressBar()
        self._progress.setAccessibleName("World maintenance progress")
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress_lbl.hide()
        self._progress.hide()
        actions.addWidget(self._progress_lbl)
        actions.addWidget(self._progress)

        # Saved worlds are the switcher: every row has one direct, named action.
        _, slots = add_card(
            "WORLDS & RECOVERY",
            "Active and saved worlds with restore points.",
        )
        self._table = QTableWidget(0, 6)
        self._table.setAccessibleName("Worlds and recovery points")
        self._table.setAccessibleDescription(
            "Active and saved worlds with their available recovery history and safe switch actions."
        )
        self._table.setHorizontalHeaderLabels(
            ["World", "State", "Size", "Saved", "Recovery", "Actions"]
        )
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            hh.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(5, 260)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        slots.addWidget(self._table)



    def _toggle_advanced_actions(self, expanded: bool) -> None:
        self._advanced_panel.setVisible(expanded)
        self._advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    # Refresh

    def _archive_entries_for(
        self, *, refresh: bool = False
    ) -> list[tuple[BackupEntry, str]]:
        """Return display-ready world archive entries, cached for one instance.

        Parsing each ZIP central directory is required to establish the
        represented world root. Do it once per explicit refresh/mutation, not
        again when the asynchronous fleet recovery projection returns.
        """
        if self._manager is None or self._instance is None:
            return []
        instance_id = str(self._instance.id)
        if (
            refresh
            or self._archive_inventory_instance_id != instance_id
        ):
            entries = self._manager.list_backups()
            external_names = {entry.filename for entry in entries}
            try:
                fleet_archives = FleetBackupManager(
                    [self._instance], TmuxManager()
                ).list_legacy_world_archives(instance_id)
            except (FleetBackupError, OSError, ValueError):
                fleet_archives = []
            entries.extend(
                entry for entry in fleet_archives
                if entry.filename not in external_names
            )
            entries.sort(key=lambda entry: entry.created_at, reverse=True)
            inventory: list[tuple[BackupEntry, str]] = []
            for entry in entries:
                try:
                    world_name = self._manager._backup_level_name(entry)
                except (OSError, ValueError, zipfile.BadZipFile):
                    world_name = entry.display_name
                inventory.append((entry, world_name))
            self._archive_inventory_entries = inventory
            self._archive_inventory_instance_id = instance_id
        return list(self._archive_inventory_entries)

    def _refresh(self, *, refresh_archives: bool = True) -> None:
        if not self._manager or not self._instance:
            return
        inst = self._instance
        world_root = inst.world_root_path()
        has_active_world = world_root.is_dir()

        if has_active_world:
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

        # Reuse the just-built ZIP projection when this render only adds
        # recovery points returned by the background fleet-list worker.
        archive_entries = self._archive_entries_for(
            refresh=refresh_archives
        )

        rows: list[dict[str, Any]] = []
        active_points = self._world_recovery_points.get(world_root.name, [])
        represented_worlds: set[str] = set()
        if has_active_world:
            rows.append({
                "kind": "active",
                "name": world_root.name,
                "points": active_points,
            })
            represented_worlds.add(world_root.name)
        for entry, archive_world_name in archive_entries:
            # An archive of the active world's own folder belongs in that
            # row's History, not as a duplicate Saved row.
            if has_active_world and archive_world_name == world_root.name:
                represented_worlds.add(archive_world_name)
                continue
            represented_worlds.add(archive_world_name)
            rows.append({
                "kind": "archive",
                "name": entry.display_name,
                "world_name": archive_world_name,
                "entry": entry,
                "points": self._world_recovery_points.get(archive_world_name, []),
            })

        # Folder worlds found in automatic fleet generations are also saved
        # worlds. They do not need a separate imported ZIP to appear here.
        snapshot_worlds = sorted(
            (
                (name, points)
                for name, points in self._world_recovery_points.items()
                if name not in represented_worlds and points
            ),
            key=lambda item: item[1][0].created_at,
            reverse=True,
        )
        for world_name, points in snapshot_worlds:
            rows.append({
                "kind": "snapshot",
                "name": world_name,
                "world_name": world_name,
                "points": points,
            })

        self._table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            points = item["points"]
            latest = points[0] if points else None
            if item["kind"] == "active":
                name_item = QTableWidgetItem(item["name"])
                name_item.setIcon(
                    QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon)
                )
                name_item.setToolTip("The world currently selected by server.properties.")
                self._table.setItem(row, 0, name_item)
                self._table.setItem(row, 1, QTableWidgetItem("Active"))
                size_text = _human_size(latest.size_bytes) if latest else "—"
                self._table.setItem(row, 2, QTableWidgetItem(size_text))
                self._table.setItem(
                    row, 3,
                    QTableWidgetItem(_friendly_datetime(latest.created_at) if latest else "Not backed up"),
                )
            elif item["kind"] == "archive":
                entry = item["entry"]
                name_item = QTableWidgetItem(item["name"])
                name_item.setIcon(
                    QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
                )
                fleet_generation_id = getattr(entry, "fleet_generation_id", None)
                if fleet_generation_id:
                    name_item.setToolTip("Verified saved-world archive.")
                elif not entry.slot_name:
                    name_item.setToolTip("Legacy saved-world archive.")
                else:
                    name_item.setToolTip("Named saved-world archive.")
                self._table.setItem(row, 0, name_item)
                self._table.setItem(row, 1, QTableWidgetItem("Saved"))
                self._table.setItem(row, 2, QTableWidgetItem(entry.size_display))
                self._table.setItem(row, 3, QTableWidgetItem(_friendly_datetime(entry.created_at)))
            else:
                name_item = QTableWidgetItem(item["name"])
                name_item.setIcon(
                    QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
                )
                name_item.setToolTip("World captured in automatic server recovery snapshots.")
                self._table.setItem(row, 0, name_item)
                self._table.setItem(row, 1, QTableWidgetItem("Saved"))
                self._table.setItem(
                    row, 2, QTableWidgetItem(_human_size(latest.size_bytes))
                )
                self._table.setItem(
                    row, 3, QTableWidgetItem(_friendly_datetime(latest.created_at))
                )

            recovery_text = (
                f"{len(points)} points" if points
                else ("Protected archive" if item["kind"] == "archive" else "No backup yet")
            )
            self._table.setItem(row, 4, QTableWidgetItem(recovery_text))

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 0, 2, 0)
            actions_layout.setSpacing(4)

            if points:
                history_btn = QPushButton("History")
                history_btn.setAccessibleName(f"View restore points for world {item['name']}")
                history_btn.setMinimumHeight(30)
                history_btn.setMinimumWidth(history_btn.sizeHint().width() + 4)
                history_btn.clicked.connect(
                    lambda _checked=False, world=item["world_name"] if item["kind"] == "archive" else item["name"]:
                    self._show_world_history(world)
                )
                actions_layout.addWidget(history_btn)

            if item["kind"] == "archive":
                entry = item["entry"]
                swap_btn = QPushButton("Switch")
                swap_btn.setAccessibleName(f"Switch to saved world {entry.display_name}")
                swap_btn.clicked.connect(
                    lambda _checked=False, e=entry: self._confirm_and_swap(e)
                )
                actions_layout.addWidget(swap_btn)

                # Imported archives can still be renamed while they remain
                # in their legacy location. Immutable fleet copies are read-only.
                if not getattr(entry, "fleet_generation_id", None):
                    rename_btn = QPushButton("Rename")
                    rename_btn.setAccessibleName(
                        f"Rename legacy world archive {entry.display_name}"
                    )
                    rename_btn.clicked.connect(
                        lambda _checked=False, e=entry: self._rename_entry(e)
                    )
                    actions_layout.addWidget(rename_btn)
            elif item["kind"] == "snapshot" and latest is not None:
                restore_btn = QPushButton("Restore")
                restore_btn.setAccessibleName(f"Restore saved world {item['name']}")
                restore_btn.clicked.connect(
                    lambda _checked=False, world=item["world_name"], point=latest:
                    self._confirm_restore_generation(world, point)
                )
                actions_layout.addWidget(restore_btn)
            self._table.setCellWidget(row, 5, actions)
            self._table.setRowHeight(row, 34)

        pre_swap_dirs = self._manager.list_pre_swap_dirs()
        if pre_swap_dirs:
            self._presafe_label.setText(
                f"{len(pre_swap_dirs)} previous world copy/copies kept for recovery."
            )
            self._cleanup_btn.setEnabled(True)
        else:
            self._presafe_label.setText("No extra old world folders.")
            self._cleanup_btn.setEnabled(False)

        self._start_stats_worker(world_root)

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
            self._dims_toggle_btn.setAccessibleName("Show fewer world dimensions")
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
            self._dims_toggle_btn.setAccessibleName("Show all world dimensions")

    def _toggle_dims_expanded(self) -> None:
        self._dims_expanded = not self._dims_expanded
        if self._instance is not None:
            self._set_dims_text(self._instance.world_dimension_dirs())

    # Background world-size scan

    def _start_stats_worker(self, world_root: Path) -> None:
        if not world_root.is_dir():
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
            self._stats_pending = world_root
            return

        self._stats_generation += 1
        generation = self._stats_generation

        thread = QThread()
        worker = _WorldStatsWorker(inst)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(lambda size: self._on_stats_done(generation, size))
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
                world_root = self._stats_pending
                self._stats_pending = None
                self._start_stats_worker(world_root)

    def _on_stats_done(self, generation: int, size: int) -> None:
        if generation != self._stats_generation or self._instance is None:
            return  # stale -- instance changed or a newer refresh already started
        world_root = self._instance.world_root_path()
        if world_root.is_dir():
            self._world_label.setText(f"World: {world_root.name}   ({_human_size(size)})")


    def _start_fleet_action(
        self,
        action: str,
        entry: BackupEntry | None = None,
        *,
        new_level_name: str | None = None,
        world_recovery: dict[str, Any] | None = None,
    ) -> None:
        if not self._instance_manager or not self._instance:
            QMessageBox.warning(
                self, "Fleet backup",
                "The server registry is unavailable; no world action was started.",
            )
            return
        if (
            self._thread and self._thread.isRunning()
            or self._recovery_thread and self._recovery_thread.isRunning()
        ):
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
            new_level_name,
            world_recovery,
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
            "Before this happens, Crucible will:\n"
            " - Automatically back up the CURRENT world (always, cannot be skipped)\n"
            " - Move the current world folder aside rather than deleting it\n"
            " - Set level-name to the saved world's original folder name\n"
            " - Extract the chosen backup and verify it before finishing\n"
            " - Automatically roll back if anything goes wrong\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._start_fleet_action("swap", entry)

    def _show_world_history(self, world_name: str) -> None:
        points = self._world_recovery_points.get(world_name, [])
        if not points:
            QMessageBox.information(
                self, "World recovery", f"No retained restore points were found for {world_name}."
            )
            return
        labels = [
            f"{_friendly_datetime(point.created_at)} · {_human_size(point.size_bytes)}"
            + (" · Active then" if point.active else "")
            for point in points
        ]
        choice, ok = QInputDialog.getItem(
            self, f"{world_name} backups", "Choose a restore point:", labels, 0, False
        )
        if not ok or choice not in labels:
            return
        self._confirm_restore_generation(
            world_name, points[labels.index(choice)]
        )

    def _confirm_restore_generation(self, world_name: str, point: Any) -> None:
        if not self._instance or not self._manager:
            return
        if self._thread and self._thread.isRunning():
            return
        status = TmuxManager().get_status(self._instance)
        if status != "stopped":
            QMessageBox.warning(
                self, "Restore cancelled",
                "Stop the server before restoring a world. Crucible will not move a world "
                f"folder while the server is {status!r}.",
            )
            return
        if self._config_guard is not None and not self._config_guard():
            return

        reply = QMessageBox.warning(
            self,
            "Restore this world?",
            f"Restore {world_name} from {_friendly_datetime(point.created_at)}?\n\n"
            "Crucible will first create a fresh full-server recovery point, move the "
            "current world aside, verify the restored world, and roll back automatically "
            "if anything fails. The server will remain stopped.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._start_fleet_action(
            "swap",
            world_recovery={
                "generation_id": point.generation_id,
                "world_name": world_name,
                "legacy_archive_filename": getattr(
                    point, "legacy_archive_filename", None
                ),
            },
        )

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
                self, "Create new world cancelled",
                "The server must be fully stopped before creating a new world -- "
                "moving a world folder out from under a running server would "
                f"corrupt it. Current status: {status}.\n\n"
                "Stop the server from the header controls above, then try again.",
            )
            return
        if self._config_guard is not None and not self._config_guard():
            return

        current_root = self._instance.world_root_path()
        current_name = current_root.name
        default_name = f"{current_name} - New"
        suffix = 2
        while (
            (self._instance.path_obj / default_name).exists()
            and default_name != current_name
        ):
            default_name = f"{current_name} - New {suffix}"
            suffix += 1

        prompt = (
            "Choose a folder name. Crucible will save the active world first."
            if current_root.is_dir()
            else "Choose a folder name. No active world folder exists yet; existing world folders and saved backups will be left untouched."
        )
        new_name, ok = QInputDialog.getText(
            self,
            "Name your new world",
            prompt,
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
        if target_root.exists() and target_root.resolve() != current_root.resolve():
            QMessageBox.warning(
                self,
                "World name already exists",
                f"A folder named '{new_name}' already exists. Switch to that world or choose another name.",
            )
            return

        if current_root.is_dir():
            recovery_details = (
                "Before the next manual start, Crucible will:\n"
                " - Create a complete verified fleet checkpoint\n"
                " - Keep the old world folder on disk as an extra recovery copy\n"
                f" - Set this server's level-name to '{new_name}'"
            )
        else:
            recovery_details = (
                "There is no generated active world folder to back up yet.\n"
                "Existing world folders and saved backup archives will be left untouched.\n"
                f"Crucible will set this server's level-name to '{new_name}'."
            )
        reply = QMessageBox.warning(
            self, "Create a new world?",
            f"Crucible will prepare a fresh world named '{new_name}'. It will not start the server.\n\n"
            f"{recovery_details}\n\n"
            "The new world is generated only when you press Start yourself. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._start_fleet_action("reset", new_level_name=new_name)

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

        confirmation = _WipeWorldConfirmDialog(world_root.name, self)
        if confirmation.exec() != QDialog.DialogCode.Accepted:
            return
        if confirmation.confirmation_text != "WIPE":
            # Defence in depth at the destructive action boundary.
            return

        # The existing worker creates and verifies the complete fleet snapshot
        # before it removes the active world. Never bypass that path here.
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
        self._advanced_toggle.setEnabled(False)
        self._table.setEnabled(False)
        for button in (
            self._backup_all_btn,
            self._verify_backups_btn,
            self._restore_server_btn,
            self._schedule_btn,
            self._open_folder_btn,
        ):
            button.setEnabled(False)
        self._progress.setRange(0, 0)
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
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._seed_btn.setEnabled(True)
        self._reset_btn.setEnabled(True)
        self._wipe_btn.setEnabled(True)
        self._advanced_toggle.setEnabled(True)
        self._table.setEnabled(True)

        if kind == "backup" and result:
            if result[0] == "failed":
                QMessageBox.critical(self, "Backup Failed", result[1])
            elif result[0] == "done":
                payload = result[1]
                backup = payload["backup"]
                QMessageBox.information(
                    self,
                    "Recovery point saved",
                    f"Verified fleet generation {backup.generation_id} was created. "
                    "It covers all registered managed servers and their discovered worlds.",
                )
        elif kind == "swap" and result:
            if result[0] == "done":
                swap_result: SwapResult = result[1]
                if self._config_reload is not None:
                    self._config_reload()
                QMessageBox.information(
                    self, "Swap complete",
                    f"{swap_result.message}\n\n"
                    "The swap used complete fleet generations rather than an additional "
                    "per-server ZIP backup.",
                )
            elif result[0] == "failed":
                QMessageBox.critical(
                    self, "Swap Failed",
                    f"{result[1]}\n\nThe previous world has been restored -- nothing was lost.",
                )
        elif kind == "reset" and result:
            if result[0] == "done":
                payload = result[1]
                reset_result: SwapResult = payload["result"]
                if self._config_reload is not None:
                    self._config_reload()
                recovery_note = (
                    f"The previous world folder remains at {reset_result.pre_swap_dir}."
                    if reset_result.pre_swap_dir is not None
                    else "There was no generated active world folder to move."
                )
                QMessageBox.information(
                    self, "New world ready",
                    f"{reset_result.message}\n\n"
                    f"{recovery_note} The complete prior server state is preserved in "
                    f"fleet generation {payload['before'].generation_id}. Start the server "
                    "manually when you're ready; Crucible will not start it for you.",
                )
            elif result[0] == "failed":
                QMessageBox.critical(self, "Create new world failed", result[1])
        elif kind == "wipe" and result:
            if result[0] == "done":
                wipe_result: SwapResult = result[1]["result"]
                QMessageBox.information(self, "World wiped", wipe_result.message)
            elif result[0] == "failed":
                QMessageBox.critical(self, "World wipe failed", result[1])
        elif kind == "cleanup" and result:
            if result[0] == "done":
                payload = result[1]
                backup = payload["backup"]
                QMessageBox.information(
                    self, "World cleanup complete",
                    f"Fleet generation {backup.generation_id} was verified before cleanup. "
                    f"Removed {payload['deleted']} redundant pre-swap world folder(s).",
                )
            elif result[0] == "failed":
                QMessageBox.critical(self, "World cleanup failed", result[1])
        self._refresh()
        self._run_recovery_job("list")

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
        reply = QMessageBox.question(
            self, "Clean up old world copies",
            "Delete old world copies older than 7 days? Copies newer "
            "than that are kept a little longer in case you need to recover from "
            "them manually.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
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
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    if b > 0:
        return "<1 KB"
    return "0 B"


def _friendly_datetime(value: str | datetime | None) -> str:
    """Format timestamps for a human glance, using the computer's local time."""
    if not value:
        return "-"
    try:
        moment = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value))
        )
        moment = moment.astimezone()
    except (TypeError, ValueError, OverflowError):
        return str(value)
    now = datetime.now().astimezone()
    clock = moment.strftime("%I:%M %p").lstrip("0")
    if moment.date() == now.date():
        return f"Today · {clock}"
    if moment.date().toordinal() == now.date().toordinal() - 1:
        return f"Yesterday · {clock}"
    month_day = moment.strftime("%b %d").replace(" 0", " ")
    date = (
        month_day
        if moment.year == now.year
        else f"{month_day}, {moment.year}"
    )
    return f"{date} · {clock}"
