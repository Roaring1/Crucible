"""Fleet-wide backup generations, scheduling, verification and restore-point status.

The backup policy is global: one snapshot per registered server in each
published generation, with exactly the newest two complete generations kept.
No per-server retention overrides or hidden named backup slots live here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...data import backup_scheduler
from ...data.fleet_backup import (
    DEFAULT_FLEET_BACKUP_DIR,
    FleetBackupError,
    FleetBackupManager,
    FleetBackupResult,
    fleet_operation_lock,
)
from ...data.instance_manager import InstanceManager
from ...data.instance_model import ServerInstance
from ...process.tmux_manager import TmuxManager
from .. import theme
from ..thread_lifecycle import connect_thread_cleanup


class _FleetBackupWorker(QObject):
    """Runs full-fleet filesystem, tmux and systemd actions away from the GUI."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        instances,
        mode: str,
        restore_args: tuple[str, str, Path, str] | None = None,
    ):
        super().__init__()
        self._instances = list(instances)
        self._mode = mode
        self._restore_args = restore_args

    def run(self) -> None:
        try:
            if self._mode in {
                "list", "capture", "verify", "install_timer", "restore"
            }:
                manager = FleetBackupManager(self._instances, TmuxManager())
            else:
                manager = None

            if self._mode == "list":
                assert manager is not None
                result = {
                    "generations": manager.list_generations(),
                    "issues": manager.generation_issues(),
                    "state": self._read_state(manager.root / "state.json"),
                    "scheduler": backup_scheduler.scheduler_status(),
                    "legacy_sources": manager.legacy_source_inventory(),
                    "legacy_backup_blockers": manager.legacy_backup_blockers(),
                }
            elif self._mode == "capture":
                assert manager is not None
                result = manager.run_backup()
            elif self._mode == "verify":
                assert manager is not None
                checks = []
                for path in manager.generation_candidates():
                    checks.append({
                        "path": path,
                        **manager.verify_generation(path, deep=True),
                    })
                result = checks
            elif self._mode == "install_timer":
                if not self._instances:
                    raise FleetBackupError(
                        "No registered servers are available; automatic fleet backups were not enabled."
                    )
                assert manager is not None
                blockers = manager.legacy_backup_blockers()
                if blockers:
                    names = ", ".join(str(item["path"]) for item in blockers[:5])
                    raise FleetBackupError(
                        f"Automatic backups were not enabled: {len(blockers)} legacy "
                        "backup source(s) are still outside the two retained fleet "
                        f"generations. Migrate/review them first: {names}"
                    )
                result = backup_scheduler.install_timer()
            elif self._mode == "restore":
                assert manager is not None
                if self._restore_args is None:
                    raise FleetBackupError("A server restore target was not selected.")
                generation_id, instance_id, target, server_name = self._restore_args
                # Serialize with timer/manual backup, world operations and pack
                # updates so retention cannot prune this generation mid-restore.
                with fleet_operation_lock(manager.root):
                    restored = manager.restore_server(
                        generation_id,
                        instance_id,
                        target,
                        deep_verify=True,
                    )
                result = {
                    "generation_id": generation_id,
                    "instance_id": instance_id,
                    "server_name": server_name,
                    "target": str(restored),
                }
            elif self._mode == "remove_timer":
                result = backup_scheduler.remove_timer()
            else:
                raise ValueError(f"Unknown fleet backup job: {self._mode}")
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - keep worker failures inside the Qt signal boundary.
            self.failed.emit(str(exc))

    @staticmethod
    def _read_state(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            return {}
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return result if isinstance(result, dict) else {}


class BackupTab(QWidget):
    """Global backup view, shared by every selected Crucible server."""

    def __init__(self, instance_manager: InstanceManager | None = None, parent=None):
        super().__init__(parent)
        self._instance_manager = instance_manager
        self._instance: ServerInstance | None = None
        self._thread: QThread | None = None
        self._worker: _FleetBackupWorker | None = None
        self._job_mode: str | None = None
        self._job_result: Any = None
        self._job_error = ""
        self._timer_enabled = False
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

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
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)
        scroll.setWidget(content)
        root_layout.addWidget(scroll)

        heading = QHBoxLayout()
        title = QLabel("Fleet backups")
        title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 17px; font-weight: 700;"
        )
        heading.addWidget(title)
        heading.addStretch()
        self._open_btn = QPushButton("Open backup folder")
        self._open_btn.clicked.connect(self._open_folder)
        heading.addWidget(self._open_btn)
        layout.addLayout(heading)

        policy = QGroupBox("One policy for every registered server")
        policy.setToolTip(
            "This retention policy applies to the whole Crucible server fleet. "
            "There are no per-instance backup-count overrides."
        )
        policy_grid = QGridLayout(policy)
        policy_grid.setContentsMargins(12, 24, 12, 10)
        policy_grid.setHorizontalSpacing(14)
        policy_grid.setVerticalSpacing(5)

        policy_grid.addWidget(QLabel("Schedule"), 0, 0)
        schedule_value = QLabel("Every 10 minutes")
        schedule_value.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 700;"
        )
        policy_grid.addWidget(schedule_value, 0, 1)

        policy_grid.addWidget(QLabel("Coverage"), 0, 2)
        coverage_value = QLabel("All registered servers")
        coverage_value.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 700;"
        )
        policy_grid.addWidget(coverage_value, 0, 3)

        policy_grid.addWidget(QLabel("Retention"), 1, 0)
        retention_value = QLabel("Newest 2 complete fleet generations")
        retention_value.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 700;"
        )
        policy_grid.addWidget(retention_value, 1, 1, 1, 3)

        self._scheduler_label = QLabel("Checking automatic backup timer…")
        self._scheduler_label.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px;"
        )
        policy_grid.addWidget(self._scheduler_label, 2, 0, 1, 4)
        self._timer_btn = QPushButton("Enable automatic backups")
        self._timer_btn.setToolTip(
            "Install/enable a systemd user timer. Backups continue when the Crucible window is closed."
        )
        self._timer_btn.clicked.connect(self._toggle_timer)
        policy_grid.addWidget(self._timer_btn, 3, 0, 1, 2)

        self._timer_remove_btn = QPushButton("Disable schedule")
        self._timer_remove_btn.setToolTip(
            "Disable the ten-minute automatic timer. Existing generations are preserved."
        )
        self._timer_remove_btn.clicked.connect(self._remove_timer)
        self._timer_remove_btn.setEnabled(False)
        policy_grid.addWidget(self._timer_remove_btn, 3, 2, 1, 2)
        layout.addWidget(policy)

        coverage = QLabel(
            "Included: all regular files in each registered server folder, every "
            "discovered world save, configs, mods, access lists, launchers, current "
            "per-instance saved-world archives, and identified retired/manual legacy "
            "backup sources. Historical sources without a safe server match stay "
            "labelled as unassigned; they are never silently attached to a live server. "
            "Excluded: transient logs, crash reports, caches, partial import staging, "
            "session locks, symbolic links, and non-regular filesystem objects. "
            "Every excluded path and reason is recorded in the generation manifest."
        )
        coverage.setWordWrap(True)
        coverage.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        coverage.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px; padding: 2px 4px;"
        )
        layout.addWidget(coverage)

        action_row = QHBoxLayout()
        self._backup_btn = QPushButton("Back up all servers now")
        self._backup_btn.setObjectName("PrimaryButton")
        self._backup_btn.setMinimumHeight(38)
        self._backup_btn.setToolTip(
            "Create one new complete fleet generation. Running servers are flushed and "
            "temporarily placed in save-off mode; no server is started or stopped."
        )
        self._backup_btn.clicked.connect(self._confirm_backup_now)
        action_row.addWidget(self._backup_btn)

        self._verify_btn = QPushButton("Verify retained generations")
        self._verify_btn.setMinimumHeight(38)
        self._verify_btn.setToolTip(
            "Deep-check every file in both retained generations against its SHA-256 manifest."
        )
        self._verify_btn.clicked.connect(self._verify_generations)
        action_row.addWidget(self._verify_btn)
        layout.addLayout(action_row)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px;"
        )
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress_label.hide()
        self._progress.hide()
        layout.addWidget(self._progress_label)
        layout.addWidget(self._progress)

        summary = QGroupBox("Latest backup activity")
        summary_grid = QGridLayout(summary)
        summary_grid.setContentsMargins(12, 23, 12, 9)
        summary_grid.setHorizontalSpacing(14)
        summary_grid.addWidget(QLabel("Last result"), 0, 0)
        self._last_result = QLabel("No backup has run yet")
        self._last_result.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: 600;"
        )
        summary_grid.addWidget(self._last_result, 0, 1)
        summary_grid.addWidget(QLabel("Completed"), 1, 0)
        self._last_completed = QLabel("—")
        summary_grid.addWidget(self._last_completed, 1, 1)
        summary_grid.addWidget(QLabel("Registered servers"), 2, 0)
        self._server_count = QLabel("—")
        summary_grid.addWidget(self._server_count, 2, 1)
        summary_grid.addWidget(QLabel("Legacy world archives"), 3, 0)
        self._migration_status = QLabel("Migration status not available yet")
        self._migration_status.setWordWrap(True)
        self._migration_status.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px;"
        )
        summary_grid.addWidget(self._migration_status, 3, 1)
        summary_grid.addWidget(QLabel("Other legacy sources"), 4, 0)
        self._other_legacy_sources = QLabel("Inventory not available yet")
        self._other_legacy_sources.setWordWrap(True)
        self._other_legacy_sources.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px;"
        )
        summary_grid.addWidget(self._other_legacy_sources, 4, 1)
        layout.addWidget(summary)

        label = QLabel("Complete fleet generations")
        label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 13px; font-weight: 700;"
        )
        generation_heading = QHBoxLayout()
        generation_heading.addWidget(label)
        generation_heading.addStretch()
        self._restore_btn = QPushButton(
            "Restore selected server to a new folder…"
        )
        self._restore_btn.setMinimumHeight(36)
        self._restore_btn.setAccessibleName(
            "Restore selected server snapshot to a new folder"
        )
        self._restore_btn.setToolTip(
            "Deep-verify the selected fleet generation, then restore only the "
            "currently selected server into a new separate folder. The live "
            "registered server is never overwritten."
        )
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._restore_selected_server)
        generation_heading.addWidget(self._restore_btn)
        layout.addLayout(generation_heading)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Created", "Servers", "Files", "Logical size", "Generation ID"]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._show_selected_details)
        layout.addWidget(self._table, stretch=1)

        self._details = QLabel(
            "A generation is published only after every registered server has been "
            "captured and its manifest, file inventory, paths and sizes pass checks. "
            "Newly copied data is SHA-256 recorded; unchanged immutable files are "
            "reused. Use Verify retained generations to re-hash every stored file "
            "before relying on a restore."
        )
        self._details.setWordWrap(True)
        self._details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._details.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px;"
        )
        layout.addWidget(self._details)

        self._issues = QLabel("")
        self._issues.setWordWrap(True)
        self._issues.setStyleSheet(
            f"color: {theme.RED}; font-size: 11px; font-weight: 600;"
        )
        self._issues.hide()
        layout.addWidget(self._issues)

    # ------------------------------------------------------------------
    # Public API / lifecycle
    # ------------------------------------------------------------------

    def load(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._server_count.setText(str(len(self._instances())))
        has_servers = bool(self._instances())
        self._timer_btn.setEnabled(has_servers)
        if not has_servers:
            self._timer_btn.setToolTip(
                "Register at least one server before enabling automatic fleet backups."
            )
        self._refresh()

    def _instances(self) -> list[ServerInstance]:
        if self._instance_manager is None:
            return [self._instance] if self._instance is not None else []
        return list(self._instance_manager.instances)

    def has_active_operation(self) -> bool:
        return bool(self._thread and self._thread.isRunning())

    # ------------------------------------------------------------------
    # Background jobs
    # ------------------------------------------------------------------

    def _run_job(
        self,
        mode: str,
        *,
        restore_args: tuple[str, str, Path, str] | None = None,
    ) -> None:
        if self.has_active_operation():
            return
        instances = self._instances()
        if not instances and mode in {"capture", "verify"}:
            QMessageBox.information(
                self, "Fleet backups", "There are no registered servers to back up."
            )
            return

        self._job_mode = mode
        self._job_result = None
        self._job_error = ""
        self._set_busy(True, mode)

        self._thread = QThread()
        self._thread.setObjectName("CrucibleFleetBackupThread")
        self._worker = _FleetBackupWorker(
            instances, mode, restore_args=restore_args
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_job_finished)
        self._worker.failed.connect(self._on_job_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        connect_thread_cleanup(self._thread, self._thread_finished)
        self._thread.start()

    def _set_busy(self, busy: bool, mode: str = "") -> None:
        self._backup_btn.setEnabled(not busy)
        self._verify_btn.setEnabled(not busy)
        self._timer_btn.setEnabled(not busy)
        self._timer_remove_btn.setEnabled(not busy and self._timer_enabled)
        if busy:
            messages = {
                "list": "Checking retained generations and timer status…",
                "capture": "Capturing every registered server; running servers are safely flushed…",
                "verify": "Deep-verifying retained generations and every SHA-256 checksum…",
                "restore": "Deep-verifying the selected generation and restoring to a new folder…",
                "install_timer": "Installing and enabling the systemd user timer…",
                "remove_timer": "Disabling the automatic fleet backup timer…",
            }
            self._progress_label.setText(messages.get(mode, "Working…"))
            self._progress_label.show()
            self._progress.show()
        else:
            self._progress_label.hide()
            self._progress.hide()
        self._restore_btn.setEnabled(
            not busy
            and self._instance is not None
            and self._table.currentRow() >= 0
        )
        self._backup_btn.setText(
            "Backing up all servers…" if busy and mode == "capture"
            else "Back up all servers now"
        )

    def _on_job_finished(self, result: Any) -> None:
        mode = self._job_mode
        self._job_result = result
        if mode == "list":
            self._render_inventory(result)
        elif mode == "capture":
            if isinstance(result, FleetBackupResult):
                self._last_result.setText(
                    f"Complete · {result.server_count} servers · {result.file_count:,} files"
                )
                self._last_completed.setText(result.generation_id)
                self._details.setText(
                    f"Generation {result.generation_id} passed manifest/inventory "
                    "checks and was published. Newly copied data was SHA-256 recorded; "
                    "use Verify retained generations for a deep re-hash of every file. "
                    f"Logical size: {_human_size(result.logical_bytes)}. "
                    f"New data copied: {_human_size(result.bytes_copied)}. "
                    f"Immutable data reused from the previous generation: "
                    f"{_human_size(result.bytes_reused)}. "
                    f"Older generations pruned: {len(result.pruned_generations)}."
                )
        elif mode == "restore":
            target = result["target"]
            QMessageBox.information(
                self,
                "Server restore completed",
                f"{result['server_name']} was restored from generation "
                f"{result['generation_id']} to a new folder:\n\n{target}\n\n"
                "Every file in the source generation passed deep SHA-256 "
                "verification. The registered live server was not changed.",
            )
            self._details.setText(
                f"Restored {result['server_name']} from generation "
                f"{result['generation_id']} to {target}. The source generation "
                "passed deep SHA-256 verification; no live server path was changed."
            )
        elif mode == "verify":
            failed = [row for row in result if not row["ok"]]
            if failed:
                QMessageBox.warning(
                    self, "Backup verification",
                    "One or more retained generations failed deep verification:\n\n"
                    + "\n".join(
                        f"{row['path'].name}: " + "; ".join(row["errors"][:4])
                        for row in failed
                    ),
                )
            else:
                QMessageBox.information(
                    self, "Backup verification",
                    f"Deep verification passed for all {len(result)} retained generation(s). "
                    "Every file's SHA-256 matched its manifest.",
                )
        elif mode == "install_timer":
            QMessageBox.information(
                self, "Automatic backups enabled",
                "The systemd user timer is enabled. Crucible will capture every registered "
                "server approximately every 10 minutes and keep only the newest two complete "
                "fleet generations. Closing this window does not stop the timer.",
            )
        elif mode == "remove_timer":
            QMessageBox.information(
                self, "Automatic backups disabled",
                "The systemd user timer is disabled. Existing backup generations are unchanged.",
            )

        if mode in {"capture", "install_timer", "remove_timer", "verify", "restore"}:
            QTimer.singleShot(100, self._refresh)

    def _on_job_failed(self, error: str) -> None:
        self._job_error = error
        mode = self._job_mode
        if mode == "capture":
            self._last_result.setText("Failed · previous complete generations retained")
            QMessageBox.critical(self, "Fleet backup failed", error)
        elif mode in {"install_timer", "remove_timer"}:
            QMessageBox.warning(self, "Automatic backup timer", error)
        elif mode == "verify":
            QMessageBox.critical(self, "Backup verification failed", error)
        elif mode == "restore":
            QMessageBox.critical(
                self,
                "Server restore failed",
                f"{error}\n\nNo registered server folder was overwritten.",
            )
        else:
            self._scheduler_label.setText("Could not read automatic backup status.")
            self._issues.setText(error)
            self._issues.show()
        if mode in {"capture", "install_timer", "remove_timer"}:
            QTimer.singleShot(100, self._refresh)

    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._job_mode = None
        self._job_result = None
        self._set_busy(False)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self._run_job("list")

    def _render_inventory(self, result: dict[str, Any]) -> None:
        generations = result.get("generations", [])
        state = result.get("state", {})
        scheduler = result.get("scheduler", {})

        self._table.setRowCount(len(generations))
        for row, item in enumerate(generations):
            created = QTableWidgetItem(item.created_at.replace("T", " ").replace("Z", " UTC"))
            servers = QTableWidgetItem(str(item.server_count))
            files = QTableWidgetItem(f"{item.file_count:,}")
            size = QTableWidgetItem(_human_size(item.logical_bytes))
            generation = QTableWidgetItem(item.generation_id)
            generation.setData(Qt.ItemDataRole.UserRole, item)
            self._table.setItem(row, 0, created)
            self._table.setItem(row, 1, servers)
            self._table.setItem(row, 2, files)
            self._table.setItem(row, 3, size)
            self._table.setItem(row, 4, generation)

        if generations:
            self._table.selectRow(0)
        else:
            self._details.setText(
                "No complete fleet generation exists yet. Run the first backup now; "
                "the timer can then maintain the rolling two-generation history."
            )

        last_status = str(state.get("last_status", "never"))
        completed = str(state.get("last_completed_at_utc") or "—")
        self._last_completed.setText(completed)
        registered = len(self._instances())
        self._server_count.setText(str(registered))

        if last_status == "success":
            self._last_result.setText(
                f"Complete · {state.get('registered_servers', 0)} servers"
            )
        elif last_status == "failed":
            self._last_result.setText("Failed · previous generations preserved")
        elif generations:
            self._last_result.setText("Complete fleet generations available")
        else:
            self._last_result.setText("No backup has run yet")

        legacy_sources = result.get("legacy_sources", [])
        legacy_blockers = result.get(
            "legacy_backup_blockers",
            [item for item in legacy_sources if item.get("kind") != "failed_import_data"],
        )
        failed_imports = [
            item for item in legacy_sources
            if item.get("kind") == "failed_import_data"
        ]
        pending_legacy_bytes = sum(
            max(0, int(item.get("bytes", 0) or 0))
            for item in legacy_blockers
        )
        if legacy_blockers:
            kinds = {}
            for item in legacy_blockers:
                kind = str(item.get("kind", "legacy source")).replace("_", " ")
                kinds[kind] = kinds.get(kind, 0) + 1
            kind_summary = ", ".join(
                f"{count} {kind}" for kind, count in sorted(kinds.items())
            )
            self._other_legacy_sources.setText(
                f"{len(legacy_blockers)} restorable legacy source(s) still outside "
                f"the fleet generations ({_human_size(pending_legacy_bytes)}): "
                f"{kind_summary}. Crucible will preserve these opaquely and retire "
                "their old locations only after both generations verify the same files."
            )
            self._other_legacy_sources.setStyleSheet(
                f"color: {theme.YELLOW}; font-size: 11px; font-weight: 600;"
            )
        elif failed_imports:
            failed_bytes = sum(int(item.get("bytes", 0) or 0) for item in failed_imports)
            self._other_legacy_sources.setText(
                "All complete legacy backup sources have been migrated. "
                f"{len(failed_imports)} partial failed-import folder(s) "
                f"({_human_size(failed_bytes)}) remain outside backups by design; "
                "they are not complete restore points and have not been deleted."
            )
            self._other_legacy_sources.setStyleSheet(
                f"color: {theme.SUBTEXT}; font-size: 11px;"
            )
        else:
            self._other_legacy_sources.setText(
                "All identified complete legacy backup sources are now represented "
                "inside the two retained fleet generations."
            )
            self._other_legacy_sources.setStyleSheet(
                f"color: {theme.GREEN}; font-size: 11px; font-weight: 600;"
            )

        archive_migration = state.get("legacy_archive_migration", {})
        archive_pending = int(archive_migration.get("source_archives_pending", 0) or 0)
        archive_pending_bytes = int(archive_migration.get("source_bytes_pending", 0) or 0)
        archive_removed = int(archive_migration.get("source_archives_removed", 0) or 0)
        archive_removed_bytes = int(archive_migration.get("source_bytes_removed", 0) or 0)
        archive_errors = archive_migration.get("cleanup_errors", [])

        other_migration = state.get("legacy_source_migration", {})
        other_pending = int(other_migration.get("sources_pending", len(legacy_blockers)) or 0)
        other_pending_bytes = int(
            other_migration.get("bytes_pending", pending_legacy_bytes) or 0
        )
        other_removed = int(other_migration.get("sources_removed", 0) or 0)
        other_removed_bytes = int(other_migration.get("bytes_removed", 0) or 0)
        other_errors = other_migration.get("cleanup_errors", [])
        migration_errors = [*archive_errors, *other_errors]

        if migration_errors:
            self._migration_status.setText(
                "Legacy backup migration needs review: "
                + "; ".join(str(item) for item in migration_errors[:2])
            )
            self._migration_status.setStyleSheet(
                f"color: {theme.YELLOW}; font-size: 11px; font-weight: 600;"
            )
        elif archive_pending or other_pending:
            pending_total = archive_pending + other_pending
            pending_total_bytes = archive_pending_bytes + other_pending_bytes
            self._migration_status.setText(
                f"{pending_total} complete legacy source(s) still mirrored outside "
                f"the fleet generations ({_human_size(pending_total_bytes)}). "
                "The original copies remain until both retained generations verify "
                "the same file contents."
            )
            self._migration_status.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 11px;"
            )
        elif archive_removed or other_removed:
            self._migration_status.setText(
                f"Legacy migration complete: {archive_removed + other_removed} old "
                f"source location(s) retired ({_human_size(archive_removed_bytes + other_removed_bytes)}). "
                "The newest two generations preserve those files."
            )
            self._migration_status.setStyleSheet(
                f"color: {theme.GREEN}; font-size: 11px; font-weight: 600;"
            )
        elif generations:
            self._migration_status.setText(
                "No complete legacy backup sources are waiting to be migrated."
            )
            self._migration_status.setStyleSheet(
                f"color: {theme.SUBTEXT}; font-size: 11px;"
            )
        else:
            self._migration_status.setText(
                "Legacy backups will be inventoried in the first fleet generation."
            )

        active = scheduler.get("active") == "true"
        enabled = scheduler.get("enabled") == "true"
        self._timer_enabled = active or enabled
        if active:
            next_run = scheduler.get("next") or "next timer interval"
            self._scheduler_label.setText(
                f"Automatic timer active · next run: {next_run}"
            )
            self._scheduler_label.setStyleSheet(
                f"color: {theme.GREEN}; font-size: 11px; font-weight: 600;"
            )
            self._timer_btn.setText("Refresh timer status")
            self._timer_remove_btn.setEnabled(True)
        else:
            detail = scheduler.get("detail") or "timer not active"
            self._scheduler_label.setStyleSheet(
                f"color: {theme.YELLOW}; font-size: 11px; font-weight: 600;"
            )
            if legacy_blockers:
                self._scheduler_label.setText(
                    f"Automatic timer inactive · {len(legacy_blockers)} legacy "
                    "backup source(s) must be migrated into both retained generations first."
                )
                self._timer_btn.setText("Resolve legacy backups before enabling")
                self._timer_btn.setEnabled(False)
                self._timer_btn.setToolTip(
                    "An unattended timer stays disabled until every retired-ID archive, "
                    "manual server snapshot and flat legacy world archive is represented "
                    "in the two retained fleet generations."
                )
            else:
                self._scheduler_label.setText(
                    f"Automatic timer inactive · {detail}"
                )
                self._timer_btn.setText("Enable automatic backups")
                self._timer_btn.setEnabled(True)
                self._timer_btn.setToolTip(
                    "Install/enable a systemd user timer. Backups continue when the Crucible window is closed."
                )
            self._timer_remove_btn.setEnabled(enabled)

        issues = result.get("issues", [])
        warnings = []
        if issues:
            warnings.append(
                "Invalid/incomplete generation data was found. Crucible will not rotate "
                "or delete it automatically:\n"
                + "\n".join(
                    f"• {item['path'].name}: " + "; ".join(item["errors"][:2])
                    for item in issues
                )
            )
        if legacy_blockers:
            warnings.append(
                "Complete legacy backup sources are not yet included in both retained "
                "fleet generations. No source was changed or deleted. Review the "
                "remaining source paths and migration status:\n"
                + "\n".join(
                    f"• {item['path']} ({_human_size(int(item.get('bytes', 0) or 0))})"
                    f" — {item['reason']}"
                    for item in legacy_blockers[:8]
                )
            )
        if warnings:
            self._issues.setText("\n\n".join(warnings))
            self._issues.show()
        else:
            self._issues.clear()
            self._issues.hide()

        if self._job_mode is None and generations:
            self._show_selected_details()

    def _show_selected_details(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            self._restore_btn.setEnabled(False)
            return
        item = self._table.item(row, 4)
        summary = item.data(Qt.ItemDataRole.UserRole) if item else None
        if summary is None:
            self._restore_btn.setEnabled(False)
            return
        self._restore_btn.setEnabled(self._instance is not None)
        server_list = ", ".join(summary.server_names) or (
            f"{summary.server_count} registered server(s)"
        )
        worlds = ", ".join(summary.world_saves) or "No generated world saves were present."
        legacy_source_list = ", ".join(summary.legacy_sources[:6])
        legacy_source_summary = (
            f"Opaque historical backup sources ({summary.legacy_source_count}, "
            f"{_human_size(summary.legacy_source_bytes)}): {legacy_source_list}"
            + (
                f"; {summary.legacy_source_count - 6} more."
                if summary.legacy_source_count > 6 else "."
            )
            if summary.legacy_source_count
            else "Opaque historical backup sources: none."
        )
        if summary.excluded_count:
            examples = "; ".join(summary.excluded_entries)
            excluded = (
                f"{summary.excluded_count} excluded path(s) recorded in the manifest"
                + (f". Examples: {examples}" if examples else "")
                + (
                    f"; {summary.excluded_count - len(summary.excluded_entries)} more."
                    if summary.excluded_count > len(summary.excluded_entries) else "."
                )
            )
        else:
            excluded = "No paths were excluded from this generation."
        self._details.setText(
            f"Generation: {summary.generation_id}\n"
            f"Path: {summary.path}\n"
            f"Servers ({summary.server_count}): {server_list}\n"
            f"World saves: {worlds}\n"
            f"{legacy_source_summary}\n"
            f"Excluded paths: {excluded}\n"
            f"Files: {summary.file_count:,} · "
            f"Logical size: {_human_size(summary.logical_bytes)}\n"
            "Stored files are immutable and independently restorable to a new "
            "server folder; the live server is never overwritten by Restore."
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _confirm_backup_now(self) -> None:
        if not self._instances():
            QMessageBox.information(self, "Fleet backup", "No servers are registered.")
            return
        reply = QMessageBox.warning(
            self,
            "Back up every server?",
            "Crucible will capture all registered servers in one fleet generation. "
            "For each running server it will request a world save, temporarily pause "
            "automatic saving, copy the files, then re-enable automatic saving. "
            "It will not start or stop any server. If a server is unmanaged or its "
            "save cannot be confirmed, the generation will fail safely and the "
            "previous two generations will remain.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._run_job("capture")

    def _verify_generations(self) -> None:
        self._run_job("verify")

    def _restore_selected_server(self) -> None:
        if self._instance is None or self.has_active_operation():
            return
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 4)
        summary = item.data(Qt.ItemDataRole.UserRole) if item else None
        if summary is None:
            return

        parent_text = QFileDialog.getExistingDirectory(
            self,
            "Choose a parent folder for the restored server",
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
            f"{safe_name}-{str(self._instance.id)[:8]}-restored-"
            f"{summary.generation_id[-8:]}"
        )
        if target.exists() or target.is_symlink():
            QMessageBox.warning(
                self,
                "Restore destination already exists",
                f"Crucible will never overwrite an existing folder:\n\n{target}\n\n"
                "Choose a different parent folder.",
            )
            return

        reply = QMessageBox.warning(
            self,
            "Restore a separate server copy?",
            f"Crucible will deep-verify every file in generation "
            f"{summary.generation_id}, then restore only "
            f"{self._instance.name} to this new folder:\n\n{target}\n\n"
            "This can require substantial disk space. The live registered "
            "server is never replaced or modified.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._run_job(
                "restore",
                restore_args=(
                    summary.generation_id,
                    str(self._instance.id),
                    target,
                    self._instance.name,
                ),
            )

    def _toggle_timer(self) -> None:
        if self._timer_btn.text() == "Refresh timer status":
            self._refresh()
        else:
            reply = QMessageBox.question(
                self,
                "Enable automatic fleet backups?",
                "Install a systemd user timer that runs every 10 minutes, even after "
                "the Crucible window is closed? It covers every registered server and "
                "retains only the newest two complete fleet generations.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._run_job("install_timer")

    def _remove_timer(self) -> None:
        reply = QMessageBox.question(
            self,
            "Disable automatic backups?",
            "Disable the ten-minute user timer? Existing backup generations will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._run_job("remove_timer")

    def _open_folder(self) -> None:
        root = DEFAULT_FLEET_BACKUP_DIR
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))


def _human_size(size: int) -> str:
    if size >= 1_073_741_824:
        return f"{size / 1_073_741_824:.1f} GB"
    if size >= 1_048_576:
        return f"{size / 1_048_576:.0f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"
