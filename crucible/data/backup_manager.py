"""
crucible/data/backup_manager.py

Timestamped zip backups of the world folder, plus named "world slots" and a
safe world-swap workflow (World Backup & Swap feature).

Backup location: ~/.local/share/crucible/backups/{instance.id}/
Filename format: {instance.name}_{YYYYMMDD_HHMMSS}.zip
Named-slot metadata: a sidecar {filename}.json next to the zip, e.g.
    Midtech_20250416_142301_000000.zip
    Midtech_20250416_142301_000000.zip.json   <- {"slot_name": "Pre-1.20 update"}
Backups with no sidecar are simply unnamed/auto backups -- this keeps every
existing v0.6.x backup fully backward-compatible; nothing is migrated or
rewritten.

BackupWorker/SwapWorker are QObjects meant to run in a QThread so neither the
zip nor the swap operation ever blocks the GUI (GTNH worlds can be 5-20 GB).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from .instance_model import ServerInstance, is_dimension_dir_name

BASE_DIR = Path.home() / ".local" / "share" / "crucible-backups"


@dataclass
class BackupEntry:
    filename:   str
    path:       Path
    size_bytes: int
    created_at: datetime
    # Optional user-supplied label (a "world slot", e.g. "Pre-1.20 update").
    # None means this is a plain auto/unnamed backup -- exactly what every
    # v0.6.x backup looks like, so old backups keep working unmodified.
    slot_name: str | None = None

    @property
    def size_display(self) -> str:
        b = self.size_bytes
        if b >= 1_073_741_824:
            return f"{b / 1_073_741_824:.1f} GB"
        if b >= 1_048_576:
            return f"{b / 1_048_576:.0f} MB"
        return f"{b / 1024:.0f} KB"

    @property
    def display_name(self) -> str:
        """What the UI should show as this backup's primary label."""
        return self.slot_name if self.slot_name else self.filename


@dataclass
class SwapResult:
    """Outcome of a successful BackupManager.swap_world() call."""
    ok: bool
    message: str
    # Path to the automatic safety backup taken of the world that was just
    # replaced (None only if there was no existing world to back up, e.g.
    # first-ever swap-in on a server that was never started).
    pre_swap_backup_path: Path | None = None
    # Path to the renamed-aside copy of the previous world root, if one was
    # created. Kept on disk as an extra safety net; see prune_pre_swap_dirs().
    pre_swap_dir: Path | None = None


class BackupManager:
    """Manages backups, named world slots, and world swaps for one ServerInstance."""

    def __init__(self, instance: ServerInstance) -> None:
        self._instance   = instance
        self._backup_dir = BASE_DIR / instance.id
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def backup_dir(self) -> Path:
        return self._backup_dir

    # Listing

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
                slot_name  = self._read_slot_metadata(f),
            ))
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries

    def total_size_bytes(self) -> int:
        return sum(f.stat().st_size for f in self._backup_dir.glob("*.zip"))

    # Named-slot metadata (sidecar JSON, backward-compatible)

    def _metadata_path(self, zip_path: Path) -> Path:
        return zip_path.with_name(zip_path.name + ".json")

    def _write_slot_metadata(self, zip_path: Path, slot_name: str) -> None:
        meta = {"slot_name": slot_name}
        meta_path = self._metadata_path(zip_path)
        tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        tmp.replace(meta_path)

    def _read_slot_metadata(self, zip_path: Path) -> str | None:
        meta_path = self._metadata_path(zip_path)
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        name = data.get("slot_name") if isinstance(data, dict) else None
        return name if isinstance(name, str) and name.strip() else None

    def rename_slot(self, entry: BackupEntry, new_name: str | None) -> None:
        """Set (or clear, if new_name is None/blank) this backup's slot label."""
        new_name = (new_name or "").strip() or None
        if new_name is None:
            self._metadata_path(entry.path).unlink(missing_ok=True)
        else:
            self._write_slot_metadata(entry.path, new_name)
        entry.slot_name = new_name

    # Backup creation

    def create_backup(
        self,
        progress_cb: Callable[[int], None] | None = None,
        slot_name: str | None = None,
    ) -> Path:
        """
        Zip the world folder(s) into a timestamped archive.
        Runs synchronously — call from a QThread.
        Raises FileNotFoundError / OSError on failure.

        If slot_name is given, this backup becomes a named "world slot":
        it's excluded from prune_old()'s automatic deletion and shows its
        label instead of the raw filename in the UI.
        """
        server_path = Path(self._instance.path)

        # Determine which world directories to back up
        world_dirs = self._find_world_dirs(server_path)
        if not world_dirs:
            raise FileNotFoundError(
                f"No world directory found in {self._instance.path}.\n"
                "Start the server at least once to generate one."
            )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
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
                if fpath.is_file() and not fpath.is_symlink():
                    resolved = fpath.resolve()
                    try:
                        resolved.relative_to(server_path.resolve())
                    except ValueError:
                        continue
                    all_files.append((resolved, resolved.relative_to(server_path.resolve())))

        total = len(all_files)
        partial = zip_path.with_suffix(".zip.partial")
        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for i, (fpath, arcname) in enumerate(all_files):
                    zf.write(fpath, arcname)
                    if progress_cb and total > 0:
                        progress_cb(int((i + 1) / total * 100))
            with zipfile.ZipFile(partial, "r") as check:
                bad = check.testzip()
                if bad is not None:
                    raise OSError(f"Backup verification failed at {bad}")
                self._verify_level_dat_present(check, all_files)
            partial.replace(zip_path)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        if slot_name:
            try:
                self._write_slot_metadata(zip_path, slot_name)
            except OSError:
                pass  # The backup itself succeeded; a lost label is not fatal.

        if progress_cb:
            progress_cb(100)
        return zip_path

    @staticmethod
    def _verify_level_dat_present(zf: zipfile.ZipFile, all_files: list[tuple[Path, Path]]) -> None:
        """Re-open the just-written archive and confirm every level.dat that
        was meant to be included actually landed there with non-zero size.

        A backup that silently dropped or truncated level.dat is worse than
        no backup at all -- this check runs before the .partial file is ever
        renamed into a "real" backup, so a failure here means create_backup()
        raises and the caller sees no new backup at all rather than a broken
        one.
        """
        expected = [arcname for _, arcname in all_files if arcname.name == "level.dat"]
        if not expected:
            return
        sizes = {info.filename: info.file_size for info in zf.infolist()}
        for arcname in expected:
            key = str(arcname).replace("\\", "/")
            size = sizes.get(key)
            if size is None:
                raise OSError(f"Backup verification failed: {key} missing from archive")
            if size == 0:
                raise OSError(f"Backup verification failed: {key} is empty in archive")

    def create_pre_swap_backup(
        self, progress_cb: Callable[[int], None] | None = None,
    ) -> Path:
        """Mandatory, unprompted safety backup taken automatically before a
        world swap. Always named so it's exempt from prune_old()."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.create_backup(
            progress_cb=progress_cb,
            slot_name=f"Pre-swap safety backup — {timestamp}",
        )

    def delete_backup(self, entry: BackupEntry) -> None:
        entry.path.unlink(missing_ok=True)
        self._metadata_path(entry.path).unlink(missing_ok=True)

    def prune_old(self, keep_count: int = 10, include_named: bool = False) -> int:
        """Delete the oldest UNNAMED backups beyond keep_count.

        Named world slots (a deliberate checkpoint the user labeled, e.g.
        "Pre-1.20 update", or an automatic pre-swap safety backup) are never
        silently deleted by count/age-based pruning unless include_named is
        explicitly set -- a backup that took an hour to create should not
        vanish just because a bunch of auto-backups piled up after it.
        Returns the number of backups actually deleted.
        """
        entries = self.list_backups()   # newest-first
        candidates = entries if include_named else [e for e in entries if not e.slot_name]
        to_delete = candidates[keep_count:]
        for e in to_delete:
            self.delete_backup(e)
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
        root = server_path.resolve()
        for candidate in [level_name, f"{level_name}_nether", f"{level_name}_the_end"]:
            d = (server_path / candidate).resolve()
            try:
                d.relative_to(root)
            except ValueError:
                continue
            if d.exists() and d.is_dir():
                dirs.append(d)
        return dirs

    # World swap

    def swap_world(
        self,
        entry: BackupEntry,
        progress_cb: Callable[[int], None] | None = None,
    ) -> SwapResult:
        """
        Atomically replace the CURRENT world with the world stored in `entry`.

        IMPORTANT: this method has no access to tmux/live process state. The
        caller (the World tab) MUST already have confirmed the server is
        fully stopped and there are no unsaved server.properties edits
        before calling this -- swapping a world folder out from under a live
        JVM will corrupt it. This is the highest-risk operation in Crucible,
        so it is written defensively at every step:

          1. Validate the backup's world-folder name matches this server's
             CURRENT level-name. If they don't match, refuse outright --
             extracting a differently-named world would silently create a
             second, disconnected folder instead of replacing the active one.
          2. Take an automatic, unprompted, unskippable safety backup of the
             CURRENT world (if one exists). This is the single most important
             safety net here and is never optional.
          3. Rename the current world root sideways to
             "<level-name>.pre-swap-<timestamp>" -- a fast, same-filesystem,
             metadata-only rename, never a copy, and never a delete.
          4. Extract the chosen backup into a fresh world root.
          5. Verify: level.dat exists and is non-empty, and every dimension
             folder present in the backup is present in the result (a count/
             membership check, not a full byte-for-byte diff).
          6. On ANY failure in steps 4-5: delete the partially-written new
             world root and rename the pre-swap folder back into place, so
             the server is NEVER left half-swapped. The original exception
             is re-raised after rolling back.

        On success, the pre-swap folder from step 3 is deliberately left on
        disk (not deleted) as an extra safety net -- see prune_pre_swap_dirs()
        for cleaning those up later once you've confirmed the swap is good.
        """
        if not entry.path.exists():
            raise FileNotFoundError(f"Backup archive not found: {entry.path}")

        world_root = self._instance.world_root_path()
        self._verify_backup_matches_world_root(entry, world_root)

        # Step 2: mandatory safety backup of whatever world is about to be
        # replaced. Skipped only if there is no existing world to back up
        # (e.g. first-ever swap-in on a server that was never started).
        pre_swap_backup_path: Path | None = None
        if world_root.is_dir():
            pre_swap_backup_path = self.create_pre_swap_backup()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        pre_swap_dir = world_root.with_name(f"{world_root.name}.pre-swap-{timestamp}")
        renamed = False

        try:
            # Step 3: rename the live world sideways -- never delete it here.
            if world_root.is_dir():
                world_root.rename(pre_swap_dir)
                renamed = True

            # Step 4: extract into a fresh world root. Archive members are
            # stored relative to the SERVER root (not the world root), e.g.
            # "World/level.dat", "World/DIM1/region/..." -- see create_backup.
            with zipfile.ZipFile(entry.path, "r") as zf:
                names = zf.namelist()
                total = len(names)
                for i, name in enumerate(names):
                    zf.extract(name, path=self._instance.path_obj)
                    if progress_cb and total > 0:
                        progress_cb(int((i + 1) / total * 90))  # leave 90-100% for verify

            # Step 5: verify.
            self._verify_swapped_world(world_root, entry)

        except Exception:
            # Step 6: roll back -- never leave the server half-swapped.
            if world_root.exists():
                shutil.rmtree(world_root, ignore_errors=True)
            if renamed and pre_swap_dir.exists():
                pre_swap_dir.rename(world_root)
            raise

        if progress_cb:
            progress_cb(100)
        return SwapResult(
            ok=True,
            message=f"Swapped to \u2018{entry.display_name}\u2019.",
            pre_swap_backup_path=pre_swap_backup_path,
            pre_swap_dir=pre_swap_dir if renamed else None,
        )

    def _verify_backup_matches_world_root(self, entry: BackupEntry, world_root: Path) -> None:
        """Refuse to swap a backup whose stored world-folder name doesn't
        match the server's CURRENTLY configured level-name."""
        with zipfile.ZipFile(entry.path, "r") as zf:
            top_levels = {
                Path(n).parts[0] for n in zf.namelist()
                if n.strip("/") and Path(n).parts
            }
        if not top_levels:
            raise ValueError(f"Backup archive is empty: {entry.path}")
        if world_root.name not in top_levels:
            raise ValueError(
                f"This backup's world folder ({sorted(top_levels)}) doesn't match "
                f"the server's current level-name ('{world_root.name}'). Swapping "
                "would create a second, disconnected world folder instead of "
                "replacing the active one. Change level-name back to match this "
                "backup first, or choose a backup made with the current level-name."
            )

    def _verify_swapped_world(self, world_root: Path, entry: BackupEntry) -> None:
        """Post-extraction sanity check: level.dat present + non-empty, and
        every dimension folder that was in the backup is present in the
        result (membership/count check, not a full diff)."""
        level_dat = world_root / "level.dat"
        if not level_dat.exists() or level_dat.stat().st_size == 0:
            raise OSError(
                "Swap verification failed: level.dat is missing or empty after "
                "extraction. The previous world has been restored."
            )

        top = world_root.name
        with zipfile.ZipFile(entry.path, "r") as zf:
            expected_dims = {
                parts[1] for n in zf.namelist()
                if len(parts := Path(n).parts) > 1
                and parts[0] == top
                and is_dimension_dir_name(parts[1])
            }
        actual_dims = {
            d.name for d in world_root.iterdir()
            if d.is_dir() and is_dimension_dir_name(d.name)
        }
        missing = expected_dims - actual_dims
        if missing:
            raise OSError(
                "Swap verification failed: dimension folder(s) missing after "
                f"extraction: {sorted(missing)}. The previous world has been restored."
            )

    def prune_pre_swap_dirs(self, keep_days: int = 7) -> int:
        """Delete leftover \"<level>.pre-swap-<timestamp>\" sibling directories
        created automatically by swap_world() that are older than keep_days.

        These exist purely as an extra safety net right after a swap; once
        you've confirmed the swapped-to world is good, they're safe to clean
        up. Never touches anything that doesn't match the pre-swap naming
        pattern for THIS instance's current level-name.
        """
        world_root = self._instance.world_root_path()
        parent = world_root.parent
        if not parent.is_dir():
            return 0
        prefix = f"{world_root.name}.pre-swap-"
        cutoff = datetime.now().timestamp() - keep_days * 86400
        deleted = 0
        for candidate in parent.iterdir():
            if not (candidate.is_dir() and candidate.name.startswith(prefix)):
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    shutil.rmtree(candidate, ignore_errors=True)
                    deleted += 1
            except OSError:
                continue
        return deleted

    def list_pre_swap_dirs(self) -> list[Path]:
        """List leftover pre-swap sibling directories for this instance, newest first."""
        world_root = self._instance.world_root_path()
        parent = world_root.parent
        if not parent.is_dir():
            return []
        prefix = f"{world_root.name}.pre-swap-"
        found = [c for c in parent.iterdir() if c.is_dir() and c.name.startswith(prefix)]
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return found


class BackupWorker(QObject):
    """Runs BackupManager.create_backup() in a background QThread."""

    progress = pyqtSignal(int)    # 0–100
    finished = pyqtSignal(str)    # path to created zip
    failed   = pyqtSignal(str)    # error message

    def __init__(
        self,
        manager: BackupManager,
        parent: QObject | None = None,
        slot_name: str | None = None,
    ):
        super().__init__(parent)
        self._manager = manager
        self._slot_name = slot_name

    def run(self) -> None:
        try:
            path = self._manager.create_backup(
                progress_cb=lambda p: self.progress.emit(p),
                slot_name=self._slot_name,
            )
            self.finished.emit(str(path))
        except Exception as exc:
            self.failed.emit(str(exc))


class SwapWorker(QObject):
    """Runs BackupManager.swap_world() in a background QThread.

    Both the mandatory pre-swap backup and the extract/verify steps can take
    a long time on large GTNH worlds, so this must never run on the GUI thread
    -- same rationale as BackupWorker.
    """

    progress = pyqtSignal(int)       # 0–100
    finished = pyqtSignal(object)    # SwapResult
    failed   = pyqtSignal(str)       # error message

    def __init__(self, manager: BackupManager, entry: BackupEntry, parent: QObject | None = None):
        super().__init__(parent)
        self._manager = manager
        self._entry = entry

    def run(self) -> None:
        try:
            result = self._manager.swap_world(
                self._entry, progress_cb=lambda p: self.progress.emit(p)
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
