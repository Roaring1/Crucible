"""
crucible/data/backup_manager.py

Timestamped zip backups of the world folder.

Backup location: ~/.local/share/crucible/backups/{instance.id}/
Filename format: {instance.name}_{YYYYMMDD_HHMMSS}.zip

BackupWorker is a QObject meant to run in a QThread so the zip
operation never blocks the GUI (GTNH worlds can be 5–20 GB).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from .instance_model import ServerInstance

BASE_DIR = Path.home() / ".local" / "share" / "crucible-backups"


@dataclass
class BackupEntry:
    filename:   str
    path:       Path
    size_bytes: int
    created_at: datetime

    @property
    def size_display(self) -> str:
        b = self.size_bytes
        if b >= 1_073_741_824:
            return f"{b / 1_073_741_824:.1f} GB"
        if b >= 1_048_576:
            return f"{b / 1_048_576:.0f} MB"
        return f"{b / 1024:.0f} KB"


class BackupManager:
    """Manages backups for one ServerInstance."""

    def __init__(self, instance: ServerInstance) -> None:
        self._instance   = instance
        self._backup_dir = BASE_DIR / instance.id
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def backup_dir(self) -> Path:
        return self._backup_dir

    def list_backups(self) -> list[BackupEntry]:
        """Return backups sorted newest-first."""
        entries = []
        for f in self._backup_dir.glob("*.zip"):
            st = f.stat()
            entries.append(BackupEntry(
                filename   = f.name,
                path       = f,
                size_bytes = st.st_size,
                created_at = datetime.fromtimestamp(st.st_mtime),
            ))
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries

    def total_size_bytes(self) -> int:
        return sum(f.stat().st_size for f in self._backup_dir.glob("*.zip"))

    def create_backup(
        self,
        progress_cb: Callable[[int], None] | None = None,
    ) -> Path:
        """
        Zip the world folder(s) into a timestamped archive.
        Runs synchronously — call from a QThread.
        Raises FileNotFoundError / OSError on failure.
        """
        server_path = Path(self._instance.path)

        # Determine which world directories to back up
        world_dirs = self._find_world_dirs(server_path)
        if not world_dirs:
            raise FileNotFoundError(
                f"No world directory found in {self._instance.path}.\n"
                "Start the server at least once to generate one."
            )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Sanitize instance name for filename
        safe_name = "".join(
            c for c in self._instance.name if c.isalnum() or c in "._- "
        ).strip().replace(" ", "_")
        zip_name = f"{safe_name}_{timestamp}.zip"
        zip_path = self._backup_dir / zip_name

        # Collect all files
        all_files: list[tuple[Path, Path]] = []   # (abs_path, arcname)
        for wdir in world_dirs:
            for fpath in wdir.rglob("*"):
                if fpath.is_file():
                    all_files.append((fpath, fpath.relative_to(server_path)))

        total = len(all_files)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for i, (fpath, arcname) in enumerate(all_files):
                try:
                    zf.write(fpath, arcname)
                except OSError:
                    pass   # skip locked/missing files mid-backup
                if progress_cb and total > 0:
                    progress_cb(int((i + 1) / total * 100))

        if progress_cb:
            progress_cb(100)
        return zip_path

    def delete_backup(self, entry: BackupEntry) -> None:
        entry.path.unlink(missing_ok=True)

    def prune_old(self, keep_count: int = 10) -> int:
        """Delete oldest backups beyond keep_count. Returns number deleted."""
        entries   = self.list_backups()   # newest-first
        to_delete = entries[keep_count:]
        for e in to_delete:
            e.path.unlink(missing_ok=True)
        return len(to_delete)

    def _find_world_dirs(self, server_path: Path) -> list[Path]:
        """Find world directories: read level-name from server.properties."""
        props = server_path / "server.properties"
        level_name = "world"   # default
        if props.exists():
            for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("level-name="):
                    level_name = line.split("=", 1)[1].strip()
                    break

        dirs = []
        for candidate in [level_name, f"{level_name}_nether", f"{level_name}_the_end"]:
            d = server_path / candidate
            if d.exists() and d.is_dir():
                dirs.append(d)
        return dirs


class BackupWorker(QObject):
    """Runs BackupManager.create_backup() in a background QThread."""

    progress = pyqtSignal(int)    # 0–100
    finished = pyqtSignal(str)    # path to created zip
    failed   = pyqtSignal(str)    # error message

    def __init__(self, manager: BackupManager, parent: QObject | None = None):
        super().__init__(parent)
        self._manager = manager

    def run(self) -> None:
        try:
            path = self._manager.create_backup(
                progress_cb=lambda p: self.progress.emit(p)
            )
            self.finished.emit(str(path))
        except Exception as exc:
            self.failed.emit(str(exc))
