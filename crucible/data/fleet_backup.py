"""Transactional, fleet-wide server snapshots with two-generation retention.

Snapshots are directory trees rather than repeatedly compressed ZIPs. On ext4,
unchanged files are hard-linked only from the previous immutable snapshot,
never from a live server folder. Changed files are copied, hashed and checked
for concurrent modification. The result is a content-stable point-in-time tree
with efficient incremental storage and straightforward offline restoration.

Running Minecraft servers are quiesced with save-off/save-all/save-on. A
snapshot is not published unless the console confirms the save and re-enable
steps. No server is ever started by this module.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .instance_model import ServerInstance

DEFAULT_FLEET_BACKUP_DIR = (
    Path.home() / ".local" / "share" / "crucible-backups" / "fleet"
)
GENERATION_SCHEMA = 1
DEFAULT_KEEP_GENERATIONS = 2

# These are reproducible, transient or competing backup locations. The reason
# for every skipped subtree/file is written to the generation manifest.
_EXCLUDED_DIRS = {
    ".git",
    ".cache",
    "cache",
    "caches",
    "logs",
    "crash-reports",
    "backups",
    "tmp",
    "temp",
}
_EXCLUDED_FILE_NAMES = {
    "session.lock",
    ".ds_store",
}
_EXCLUDED_FILE_SUFFIXES = {
    ".log",
    ".tmp",
    ".partial",
    ".pid",
    ".lck",
}
_EXCLUDED_FILE_MARKERS = (
    ".log.",
    ".tmp.",
    ".partial.",
)


class FleetBackupError(RuntimeError):
    """The fleet snapshot could not be committed safely."""


class BackupAlreadyRunning(FleetBackupError):
    """Another scheduler/manual backup or fleet mutation owns the lock."""


class ConsoleCommandUnacknowledged(FleetBackupError):
    """A console command was sent, but its expected confirmation was not observed."""


@contextmanager
def fleet_operation_lock(
    root: Path = DEFAULT_FLEET_BACKUP_DIR,
) -> Iterator[None]:
    """Serialize backups, pack updates and destructive world operations.

    The lock is process-wide, non-blocking and owner-private. A timer firing
    during a world/update transaction exits with a visible busy error instead
    of snapshotting an intermediate state or starting a competing copy.
    """
    root = Path(root).expanduser()
    if root.is_symlink():
        raise FleetBackupError(
            f"Fleet operation lock root must not be a symbolic link: {root}"
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    lock_path = root / "fleet-operation.lock"
    if lock_path.is_symlink():
        raise FleetBackupError(
            "Refusing a symbolic link at the fleet operation lock path."
        )
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise FleetBackupError(
            f"Could not safely open fleet operation lock: {exc}"
        ) from exc

    handle = os.fdopen(fd, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupAlreadyRunning(
                "Another fleet backup, world operation or modpack update is already running."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started={_utc_now()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        handle.close()


@dataclass(frozen=True)
class FleetBackupResult:
    generation_id: str
    path: Path
    server_count: int
    file_count: int
    logical_bytes: int
    bytes_copied: int
    bytes_reused: int
    pruned_generations: tuple[str, ...]
    migrated_legacy_archives: int = 0
    legacy_archive_bytes_removed: int = 0


@dataclass(frozen=True)
class GenerationSummary:
    generation_id: str
    path: Path
    created_at: str
    server_count: int
    file_count: int
    logical_bytes: int
    bytes_copied: int
    server_names: tuple[str, ...] = ()
    world_saves: tuple[str, ...] = ()
    excluded_entries: tuple[str, ...] = ()
    excluded_count: int = 0
    legacy_sources: tuple[str, ...] = ()
    legacy_source_count: int = 0
    legacy_source_bytes: int = 0
    server_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorldRecoveryPoint:
    """One world restore point backed by an immutable fleet generation."""

    generation_id: str
    created_at: str
    world_name: str
    active: bool
    size_bytes: int
    legacy_archive_filename: str | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_relative(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or "\x00" in raw
    ):
        raise FleetBackupError(f"Unsafe relative backup path: {raw!r}")
    return path


def _stat_signature(info: os.stat_result) -> dict[str, int]:
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
    }


def _same_signature(a: dict[str, Any] | None, b: dict[str, int]) -> bool:
    if not isinstance(a, dict):
        return False
    return all(int(a.get(key, -1)) == value for key, value in b.items())


def _snapshot_file_mode(original_mode: int) -> int:
    """Retain executable/read bits while making stored file contents read-only."""
    return (stat.S_IMODE(original_mode) & 0o555) | 0o400


def _is_excluded_directory(name: str) -> str | None:
    lowered = name.casefold()
    if lowered in _EXCLUDED_DIRS:
        return "transient, log, cache, or separate-backup directory"
    # Previous Crucible versions may leave recoverable top-level world folders
    # named <level>.pre-swap-<timestamp>. They contain unique world state and
    # must be included in a complete fleet generation, not silently skipped.
    return None


def _is_excluded_file(name: str) -> str | None:
    lowered = name.casefold()
    if lowered in _EXCLUDED_FILE_NAMES:
        return "runtime lock or desktop metadata"
    if Path(lowered).suffix in _EXCLUDED_FILE_SUFFIXES:
        return "transient log, lock, temporary or partial file"
    if any(marker in lowered for marker in _EXCLUDED_FILE_MARKERS):
        return "rotated log or temporary file"
    return None


def _server_key(instance_id: str) -> str:
    return hashlib.sha256(str(instance_id).encode("utf-8")).hexdigest()


def _world_inventory(server_root: Path, active_world: str) -> list[dict[str, Any]]:
    worlds: list[dict[str, Any]] = []
    try:
        children = sorted(server_root.iterdir(), key=lambda path: path.name.casefold())
    except OSError as exc:
        raise FleetBackupError(
            f"Could not inspect world saves in {server_root}: {exc}"
        ) from exc

    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        if _is_excluded_directory(child.name):
            continue
        # A pre-swap directory is a temporary rollback copy of a prior active
        # world, not a separately named world slot. It remains fully captured
        # as part of the server tree, but is surfaced through the existing
        # previous-world cleanup/recovery controls rather than as a world row.
        if re.search(r"\.pre-swap-\d{8}_\d{6}_\d{6}$", child.name):
            continue
        level_dat = child / "level.dat"
        try:
            if (
                level_dat.is_file()
                and not level_dat.is_symlink()
                and level_dat.stat().st_size > 0
            ):
                worlds.append(
                    {
                        "name": child.name,
                        "active": child.name == active_world,
                        "level_dat_bytes": level_dat.stat().st_size,
                    }
                )
        except OSError as exc:
            raise FleetBackupError(
                f"Could not inspect world {child.name}: {exc}"
            ) from exc
    return worlds


def _active_world_name(server_root: Path) -> str:
    properties = server_root / "server.properties"
    if not properties.is_file() or properties.is_symlink():
        return "world"
    try:
        for line in properties.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            stripped = line.strip()
            if stripped.startswith("level-name="):
                name = stripped.split("=", 1)[1].strip()
                if name and Path(name).name == name and name not in (".", ".."):
                    return name
    except OSError as exc:
        raise FleetBackupError(f"Could not read {properties}: {exc}") from exc
    return "world"


def _walk_server(root: Path) -> tuple[list[Path], list[Path], list[dict[str, str]]]:
    """Return safe relative file/dir paths and explicit exclusions."""
    files: list[Path] = []
    directories: list[Path] = []
    excluded: list[dict[str, str]] = []

    def fail_on_walk_error(error: OSError) -> None:
        raise FleetBackupError(f"Could not enumerate server files: {error}") from error

    for current_text, dir_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=fail_on_walk_error
    ):
        current = Path(current_text)
        rel_current = current.relative_to(root)

        keep_dirs: list[str] = []
        for name in sorted(dir_names, key=str.casefold):
            full = current / name
            rel = (rel_current / name).as_posix()
            if full.is_symlink():
                excluded.append({"path": rel, "reason": "symbolic link not followed"})
                continue
            reason = _is_excluded_directory(name)
            if reason:
                excluded.append({"path": rel, "reason": reason})
                continue
            if not full.is_dir():
                excluded.append({"path": rel, "reason": "not a regular directory"})
                continue
            directories.append(rel_current / name)
            keep_dirs.append(name)
        dir_names[:] = keep_dirs

        for name in sorted(file_names, key=str.casefold):
            full = current / name
            rel = (rel_current / name).as_posix()
            if full.is_symlink():
                excluded.append({"path": rel, "reason": "symbolic link not followed"})
                continue
            reason = _is_excluded_file(name)
            if reason:
                excluded.append({"path": rel, "reason": reason})
                continue
            try:
                info = full.lstat()
            except OSError as exc:
                raise FleetBackupError(f"Could not stat {full}: {exc}") from exc
            if not stat.S_ISREG(info.st_mode):
                excluded.append(
                    {"path": rel, "reason": "non-regular filesystem object"}
                )
                continue
            files.append(rel_current / name)

    files.sort(key=lambda path: path.as_posix().casefold())
    directories.sort(key=lambda path: path.as_posix().casefold())
    excluded.sort(key=lambda item: item["path"].casefold())
    return files, directories, excluded


def _copy_and_hash_live(
    source: Path, destination: Path
) -> tuple[str, int, dict[str, int], int]:
    """Copy from a no-follow file descriptor, hashing bytes and rejecting mutation."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise FleetBackupError(f"Could not safely open {source}: {exc}") from exc

    temp = destination.with_name(destination.name + f".{uuid.uuid4().hex}.partial")
    digest = hashlib.sha256()
    copied = 0
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise FleetBackupError(f"Not a regular file: {source}")
        before_sig = _stat_signature(before)
        destination.parent.mkdir(parents=True, exist_ok=True)

        with os.fdopen(source_fd, "rb", closefd=True) as src:
            source_fd = -1
            with temp.open("xb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
                    digest.update(block)
                    copied += len(block)
                dst.flush()
                os.fsync(dst.fileno())
            after = os.fstat(src.fileno())

        after_sig = _stat_signature(after)
        if before_sig != after_sig or copied != before_sig["size"]:
            raise FleetBackupError(
                f"File changed during snapshot; refusing an inconsistent copy: {source}"
            )

        os.chmod(temp, _snapshot_file_mode(before.st_mode))
        os.replace(temp, destination)
        return digest.hexdigest(), copied, before_sig, copied
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def _copy_and_hash_snapshot(source: Path, destination: Path, expected_hash: str) -> int:
    """Fallback when the filesystem cannot hard-link a prior immutable snapshot."""
    digest = hashlib.sha256()
    copied = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + f".{uuid.uuid4().hex}.partial")
    try:
        with source.open("rb") as src, temp.open("xb") as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
                digest.update(block)
                copied += len(block)
            dst.flush()
            os.fsync(dst.fileno())
        if digest.hexdigest() != expected_hash:
            raise FleetBackupError(
                f"Previous immutable snapshot failed checksum verification: {source}"
            )
        os.chmod(temp, stat.S_IMODE(source.stat().st_mode) & 0o555 | 0o400)
        os.replace(temp, destination)
        return copied
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FleetBackupManager:
    """Create complete, immutable fleet generations with global two-generation retention."""

    def __init__(
        self,
        instances: Iterable[ServerInstance],
        tmux,
        root: Path = DEFAULT_FLEET_BACKUP_DIR,
        *,
        keep_generations: int = DEFAULT_KEEP_GENERATIONS,
        command_timeout_s: float = 30.0,
        poll_interval_s: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        legacy_backup_base: Path | None = None,
    ) -> None:
        if keep_generations != DEFAULT_KEEP_GENERATIONS:
            raise ValueError("Crucible fleet backups retain exactly two generations.")
        self.instances = list(instances)
        self.tmux = tmux
        self.root = Path(root).expanduser()
        self.generations_dir = self.root / "generations"
        self.staging_dir = self.root / "staging"
        self.lock_path = self.root / "fleet-backup.lock"
        self.state_path = self.root / "state.json"
        self.command_timeout_s = command_timeout_s
        self.poll_interval_s = poll_interval_s
        self._sleep = sleep
        self.legacy_backup_base = (
            Path(legacy_backup_base).expanduser()
            if legacy_backup_base is not None
            else None
        )
        self._bytes_copied = 0
        self._bytes_reused = 0

    # ------------------------------------------------------------------
    # Fleet cycle and retention
    # ------------------------------------------------------------------

    def run_backup(
        self,
        *,
        operation_lock_held: bool = False,
        retire_legacy_sources: bool = True,
    ) -> FleetBackupResult:
        """Capture every server; optionally defer old-archive source cleanup.

        World swaps use retire_legacy_sources=False for their pre-swap
        generation so a selected external archive is not unlinked before it
        has been extracted. Their post-swap generation resumes normal cleanup.
        """
        if not operation_lock_held:
            with fleet_operation_lock(self.root):
                return self.run_backup(
                    operation_lock_held=True,
                    retire_legacy_sources=retire_legacy_sources,
                )
        if not self.instances:
            raise FleetBackupError(
                "No registered Crucible servers are available to back up."
            )

        self.root = self.root.expanduser()
        if self.root.is_symlink():
            raise FleetBackupError(
                f"Fleet backup root must be a real directory, not a symbolic link: {self.root}"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.generations_dir.is_symlink() or self.staging_dir.is_symlink():
            raise FleetBackupError(
                "Fleet backup generations/staging paths must not be symbolic links."
            )
        self.generations_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._restrict_directory(self.root)
        self._restrict_directory(self.generations_dir)
        self._restrict_directory(self.staging_dir)

        lock_handle = self._acquire_lock()
        stage: Path | None = None
        published_path: Path | None = None
        published_durable = False
        generation_id = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        )
        started_at = _utc_now()
        self._bytes_copied = 0
        self._bytes_reused = 0

        try:
            self._cleanup_stale_staging()
            issues = self.generation_issues()
            if issues:
                details = "; ".join(
                    f"{item['path'].name}: {', '.join(item['errors'][:3])}"
                    for item in issues
                )
                raise FleetBackupError(
                    "Existing fleet generation data is invalid; refusing to rotate or "
                    "delete any retained restore points until it is reviewed: "
                    + details
                )
            previous = self._latest_generation()
            previous_instances = {
                str(item["id"]): {
                    **item,
                    "_generation_path": str(previous[0]),
                }
                for item in (previous[1]["instances"] if previous else [])
            }
            stage = self.staging_dir / generation_id
            stage.mkdir(mode=0o700)

            captured_instances: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            seen_paths: set[str] = set()
            for instance in self.instances:
                instance_id = str(instance.id)
                normalized_path = str(Path(instance.path).expanduser().resolve())
                if instance_id in seen_ids or normalized_path in seen_paths:
                    raise FleetBackupError(
                        "The registry contains duplicate server identities or paths; "
                        "refusing to publish a generation with ambiguous entries."
                    )
                seen_ids.add(instance_id)
                seen_paths.add(normalized_path)
                prior = previous_instances.get(instance_id)
                captured_instances.append(
                    self._snapshot_instance(stage, instance, prior)
                )

            legacy_sources, legacy_sources_excluded = self._snapshot_legacy_sources(
                stage=stage,
                previous_manifest=previous[1] if previous else None,
            )
            file_count = sum(
                len(item["files"]) + len(item.get("legacy_world_archives", []))
                for item in captured_instances
            ) + sum(len(item["files"]) for item in legacy_sources)
            logical_bytes = sum(
                int(file_entry["size"])
                for item in captured_instances
                for file_entry in (
                    list(item["files"]) + list(item.get("legacy_world_archives", []))
                )
            ) + sum(
                int(file_entry["size"])
                for source in legacy_sources
                for file_entry in source["files"]
            )
            manifest: dict[str, Any] = {
                "schema": GENERATION_SCHEMA,
                "status": "complete",
                "generation_id": generation_id,
                "created_at_utc": started_at,
                "retention_generations": DEFAULT_KEEP_GENERATIONS,
                "servers": len(captured_instances),
                "file_count": file_count,
                "logical_bytes": logical_bytes,
                "bytes_copied": self._bytes_copied,
                "bytes_reused": self._bytes_reused,
                "instances": captured_instances,
                "legacy_sources": legacy_sources,
                "legacy_sources_excluded": legacy_sources_excluded,
            }
            payload = _canonical_json(manifest)
            manifest["manifest_sha256"] = _sha256_bytes(payload)
            self._write_json_atomic(stage / "manifest.json", manifest)
            self._seal_generation(stage)
            self._fsync_directory(stage)

            check = self.verify_generation(stage, deep=False)
            if not check["ok"]:
                raise FleetBackupError(
                    "The new fleet generation failed verification: "
                    + "; ".join(check["errors"][:8])
                )

            final_path = self.generations_dir / generation_id
            # Rename requires a writable staging directory and a writable
            # stage-root entry; the immutable child tree is already sealed.
            # Seal the published root immediately after the atomic rename.
            stage.chmod(0o700)
            os.replace(stage, final_path)
            stage = None
            published_path = final_path
            final_path.chmod(0o555)
            self._fsync_directory_strict(final_path)
            self._fsync_directory_strict(self.generations_dir)
            self._fsync_directory_strict(self.staging_dir)
            published_durable = True

            pruned = self._prune_after_publish()
            retained = self.list_generations()
            if retire_legacy_sources:
                (
                    migrated_archives,
                    migrated_bytes,
                    pending_archives,
                    pending_bytes,
                    cleanup_errors,
                ) = self._cleanup_migrated_legacy_archives(retained)
                (
                    migrated_legacy_sources,
                    migrated_legacy_source_bytes,
                    pending_legacy_sources,
                    pending_legacy_source_bytes,
                    legacy_source_cleanup_errors,
                ) = self._cleanup_migrated_legacy_sources(retained)
            else:
                # A selected external world archive may be about to be used by
                # a world swap. The generation contains a verified copy, but
                # leave the user's source ZIP until the post-swap generation
                # also contains it.
                migrated_archives = 0
                migrated_bytes = 0
                pending_archives, pending_bytes = self._count_legacy_sources()
                cleanup_errors = []
                migrated_legacy_sources = 0
                migrated_legacy_source_bytes = 0
                legacy_blockers = self.legacy_backup_blockers()
                pending_legacy_sources = len(legacy_blockers)
                pending_legacy_source_bytes = sum(
                    int(item.get("bytes", 0)) for item in legacy_blockers
                )
                legacy_source_cleanup_errors = []
            result = FleetBackupResult(
                generation_id=generation_id,
                path=final_path,
                server_count=len(captured_instances),
                file_count=file_count,
                logical_bytes=logical_bytes,
                bytes_copied=self._bytes_copied,
                bytes_reused=self._bytes_reused,
                pruned_generations=tuple(pruned),
                migrated_legacy_archives=migrated_archives,
                legacy_archive_bytes_removed=migrated_bytes,
            )
            self._write_state(
                {
                    "last_status": "success",
                    "last_generation_id": generation_id,
                    "last_started_at_utc": started_at,
                    "last_completed_at_utc": _utc_now(),
                    "last_error": None,
                    "registered_servers": len(captured_instances),
                    "retained_generations": [
                        summary.generation_id for summary in retained
                    ],
                    "legacy_archive_migration": {
                        "source_archives_removed": migrated_archives,
                        "source_bytes_removed": migrated_bytes,
                        "source_archives_pending": pending_archives,
                        "source_bytes_pending": pending_bytes,
                        "cleanup_errors": cleanup_errors,
                        "rule": (
                            "An old world ZIP is removed from its separate legacy folder "
                            "only after both retained fleet generations contain the same "
                            "verified archive."
                        ),
                    },
                    "legacy_source_migration": {
                        "sources_removed": migrated_legacy_sources,
                        "bytes_removed": migrated_legacy_source_bytes,
                        "sources_pending": pending_legacy_sources,
                        "bytes_pending": pending_legacy_source_bytes,
                        "cleanup_errors": legacy_source_cleanup_errors,
                        "excluded_partial_import_sources": legacy_sources_excluded,
                        "rule": (
                            "Retired-ID archives and manual/flat backup sources are "
                            "preserved opaquely and removed from their original location "
                            "only after both retained generations verify the same file inventory. "
                            "Partial failed-import data is not a complete backup and is left untouched."
                        ),
                    },
                    "policy": {
                        "interval_minutes": 10,
                        "keep_generations": DEFAULT_KEEP_GENERATIONS,
                        "scope": "all registered Crucible-managed servers",
                    },
                }
            )
            return result
        except Exception as exc:
            if published_path is not None and not published_durable and (
                published_path.exists() or published_path.is_symlink()
            ):
                try:
                    # The atomic rename happened, but one of the directory
                    # metadata flushes failed. Remove this uncommitted third
                    # tree while both prior retained generations are intact.
                    self._make_generation_removable(published_path)
                    shutil.rmtree(published_path)
                    try:
                        self._fsync_directory_strict(self.generations_dir)
                    except OSError as flush_exc:
                        print(
                            "[crucible] could not confirm directory flush after "
                            f"removing uncommitted generation {published_path}: {flush_exc}"
                        )
                except OSError as cleanup_exc:
                    print(
                        "[crucible] could not remove uncommitted fleet generation "
                        f"{published_path}: {cleanup_exc}"
                    )
            if stage is not None:
                try:
                    # A failure can occur after sealing has made the staging
                    # tree read-only. Re-open its directories before removing
                    # the incomplete generation; never publish or rotate it.
                    self._make_generation_removable(stage)
                    shutil.rmtree(stage)
                except OSError as cleanup_exc:
                    print(
                        "[crucible] could not remove failed fleet staging tree "
                        f"{stage}: {cleanup_exc}"
                    )
            self._write_state(
                {
                    "last_status": "failed",
                    "last_generation_id": None,
                    "last_started_at_utc": started_at,
                    "last_completed_at_utc": _utc_now(),
                    "last_error": str(exc),
                    "registered_servers": len(self.instances),
                    "retained_generations": [
                        summary.generation_id for summary in self.list_generations()
                    ],
                    "policy": {
                        "interval_minutes": 10,
                        "keep_generations": DEFAULT_KEEP_GENERATIONS,
                        "scope": "all registered Crucible-managed servers",
                    },
                }
            )
            if isinstance(exc, FleetBackupError):
                raise
            raise FleetBackupError(str(exc)) from exc
        finally:
            lock_handle.close()

    def _acquire_lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.lock_path.is_symlink():
            raise FleetBackupError(
                "Refusing a symbolic link at the fleet backup lock path."
            )
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise FleetBackupError(
                f"Could not safely open the fleet backup lock: {exc}"
            ) from exc
        handle = os.fdopen(fd, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise BackupAlreadyRunning(
                "A fleet backup cycle is already running."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started={_utc_now()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        return handle

    def _latest_manifest_fast(self) -> tuple[Path, dict[str, Any]] | None:
        """Load the newest manifest without statting every server file.

        Used for the World tab's archive inventory. Deep verification remains
        an explicit action and happens before a restore/swap.
        """
        if self.state_path.is_symlink():
            return None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        generation_id = (
            state.get("last_generation_id") if isinstance(state, dict) else None
        )
        candidates: list[Path] = []
        if isinstance(generation_id, str) and generation_id:
            candidates.append(self.generations_dir / generation_id)
        candidates.extend(self.generation_candidates())
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen or candidate.is_symlink() or not candidate.is_dir():
                continue
            seen.add(candidate)
            manifest = self._load_manifest(candidate)
            if manifest is not None:
                return candidate, manifest
        return None

    def list_legacy_world_archives(self, instance_id: str):
        """List saved-world ZIPs migrated into the newest complete generation."""
        latest = self._latest_manifest_fast()
        if latest is None:
            return []
        generation_path, manifest = latest
        server = next(
            (
                item
                for item in manifest.get("instances", [])
                if isinstance(item, dict) and str(item.get("id")) == str(instance_id)
            ),
            None,
        )
        if server is None:
            return []

        from .backup_manager import BackupEntry

        archive_root = (
            generation_path
            / "servers"
            / _server_key(str(instance_id))
            / "legacy-world-archives"
        )
        result = []
        for item in server.get("legacy_world_archives", []):
            try:
                filename = str(item["filename"])
                path = archive_root / filename
                if (
                    Path(filename).name != filename
                    or path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size != int(item["size"])
                    or path.stat().st_mode & 0o222
                ):
                    continue
                # Python 3.11+ parses the UTC "Z" suffix directly.
                created = datetime.fromisoformat(
                    str(item["created_at_utc"])
                ).astimezone()
                entry = BackupEntry(
                    filename=filename,
                    path=path,
                    size_bytes=int(item["size"]),
                    created_at=created,
                    slot_name=item.get("slot_name"),
                    sha256=str(item["sha256"]),
                    fleet_generation_id=str(manifest["generation_id"]),
                )
                result.append(entry)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        result.sort(key=lambda entry: entry.created_at, reverse=True)
        return result

    def generation_candidates(self) -> list[Path]:
        """List every direct child of the generation store, including invalid remnants."""
        try:
            return sorted(
                self.generations_dir.iterdir(), key=lambda path: path.name, reverse=True
            )
        except FileNotFoundError:
            return []

    def generation_issues(self) -> list[dict[str, Any]]:
        """Return incomplete, corrupt or unsafe generation-store children."""
        issues: list[dict[str, Any]] = []
        for path in self.generation_candidates():
            result = self.verify_generation(path, deep=False)
            if not result["ok"]:
                issues.append({"path": path, "errors": result["errors"]})
        return issues

    def list_generations(self) -> list[GenerationSummary]:
        found: list[GenerationSummary] = []
        for path in self.generation_candidates():
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                result = self.verify_generation(path, deep=False)
                if not result["ok"]:
                    continue
                manifest = result["manifest"]
                server_entries = [
                    item
                    for item in manifest.get("instances", [])
                    if isinstance(item, dict)
                ]
                server_names = tuple(
                    str(item.get("name") or item.get("id") or "Unknown server")
                    for item in server_entries
                )
                world_saves = tuple(
                    f"{item.get('name') or item.get('id')}: "
                    f"{world.get('name') or '(unnamed world)'}"
                    + (" [active]" if world.get("active") else "")
                    for item in server_entries
                    for world in item.get("world_saves", [])
                    if isinstance(world, dict)
                )
                excluded_entries: list[str] = []
                excluded_count = 0
                for server in server_entries:
                    server_name = str(
                        server.get("name") or server.get("id") or "server"
                    )
                    exclusions = list(server.get("excluded", []))
                    exclusions.extend(server.get("legacy_archive_excluded", []))
                    for exclusion in exclusions:
                        if not isinstance(exclusion, dict):
                            continue
                        excluded_count += 1
                        if len(excluded_entries) < 8:
                            excluded_entries.append(
                                f"{server_name}: {exclusion.get('path', '?')} "
                                f"({exclusion.get('reason', 'excluded')})"
                            )
                legacy_sources = [
                    item
                    for item in manifest.get("legacy_sources", [])
                    if isinstance(item, dict)
                ]
                legacy_source_names = tuple(
                    f"{item.get('source', '?')} "
                    f"({item.get('kind', 'legacy backup')}, "
                    f"{int(item.get('bytes', 0)):,} bytes)"
                    for item in legacy_sources
                )
                legacy_source_bytes = sum(
                    int(item.get("bytes", 0)) for item in legacy_sources
                )
                excluded_legacy = list(manifest.get("legacy_sources_excluded", []))
                for item in excluded_legacy:
                    if isinstance(item, dict):
                        excluded_count += 1
                        if len(excluded_entries) < 8:
                            excluded_entries.append(
                                f"Legacy source {item.get('path', '?')} "
                                f"({item.get('reason', 'excluded')})"
                            )
                found.append(
                    GenerationSummary(
                        generation_id=str(manifest["generation_id"]),
                        path=path,
                        created_at=str(manifest["created_at_utc"]),
                        server_count=int(manifest["servers"]),
                        file_count=int(manifest["file_count"]),
                        logical_bytes=int(manifest["logical_bytes"]),
                        bytes_copied=int(manifest["bytes_copied"]),
                        server_names=server_names,
                        world_saves=world_saves,
                        excluded_entries=tuple(excluded_entries),
                        excluded_count=excluded_count,
                        legacy_sources=legacy_source_names,
                        legacy_source_count=len(legacy_sources),
                        legacy_source_bytes=legacy_source_bytes,
                        server_ids=tuple(str(item.get("id") or "") for item in server_entries),
                    )
                )
            except (OSError, ValueError, KeyError, TypeError, FleetBackupError):
                continue
        found.sort(key=lambda summary: summary.created_at, reverse=True)
        return found

    @staticmethod
    def _legacy_archive_world_name(path: Path) -> str:
        """Read one safe world root from an immutable, checksum-verified ZIP."""
        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = archive.infolist()
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise FleetBackupError(
                f"Could not inspect saved-world archive {path.name}: {exc}"
            ) from exc
        roots: set[str] = set()
        has_level_dat = False
        for info in infos:
            name = info.filename
            relative = PurePosixPath(name)
            if (
                not name.strip("/")
                or "\\x00" in name
                or "\\\\" in name
                or relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
            ):
                raise FleetBackupError(
                    f"Saved-world archive {path.name} contains an unsafe path."
                )
            roots.add(relative.parts[0])
            if (
                len(relative.parts) == 2
                and relative.parts[1] == "level.dat"
                and not info.is_dir()
                and info.file_size > 0
            ):
                has_level_dat = True
        base_roots = [
            root for root in roots
            if not root.endswith(("_nether", "_the_end"))
        ]
        if len(base_roots) != 1 or not has_level_dat:
            raise FleetBackupError(
                f"Saved-world archive {path.name} does not contain one complete world."
            )
        world_name = base_roots[0]
        has_world_level_dat = any(
            PurePosixPath(info.filename).parts == (world_name, "level.dat")
            and not info.is_dir()
            and info.file_size > 0
            for info in infos
        )
        if not has_world_level_dat:
            raise FleetBackupError(
                f"Saved-world archive {path.name} has no level.dat at its world root."
            )
        relative = _safe_relative(world_name)
        if len(relative.parts) != 1 or world_name in {".", ".."}:
            raise FleetBackupError(
                f"Saved-world archive {path.name} has an unsafe world root."
            )
        return world_name

    def list_world_recovery_points(
        self, instance_id: str
    ) -> dict[str, list[WorldRecoveryPoint]]:
        """List world-folder and migrated ZIP restore points by world name."""
        points: dict[str, list[WorldRecoveryPoint]] = {}
        seen_archives: set[tuple[str, str]] = set()
        for summary in self.list_generations():
            manifest = self._load_manifest(summary.path)
            if manifest is None:
                continue
            server = next(
                (
                    item for item in manifest.get("instances", [])
                    if isinstance(item, dict)
                    and str(item.get("id")) == str(instance_id)
                ),
                None,
            )
            if server is None:
                continue
            file_records = [
                item for item in server.get("files", [])
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ]
            for world in server.get("world_saves", []):
                if not isinstance(world, dict):
                    continue
                world_name = str(world.get("name") or "")
                try:
                    relative_root = _safe_relative(world_name)
                except FleetBackupError:
                    continue
                if len(relative_root.parts) != 1:
                    continue
                world_roots = {
                    world_name,
                    f"{world_name}_nether",
                    f"{world_name}_the_end",
                }
                world_files = [
                    item for item in file_records
                    if str(item["path"]).split("/", 1)[0] in world_roots
                ]
                level_dat = next(
                    (
                        item for item in world_files
                        if str(item["path"]) == f"{world_name}/level.dat"
                    ),
                    None,
                )
                if level_dat is None or int(level_dat.get("size", 0)) <= 0:
                    continue
                points.setdefault(world_name, []).append(
                    WorldRecoveryPoint(
                        generation_id=summary.generation_id,
                        created_at=summary.created_at,
                        world_name=world_name,
                        active=bool(world.get("active")),
                        size_bytes=sum(int(item.get("size", 0)) for item in world_files),
                    )
                )

            archive_root = (
                summary.path
                / "servers"
                / _server_key(str(instance_id))
                / "legacy-world-archives"
            )
            for record in server.get("legacy_world_archives", []):
                if not isinstance(record, dict):
                    continue
                filename = record.get("filename")
                digest = str(record.get("sha256") or "")
                if (
                    not isinstance(filename, str)
                    or Path(filename).name != filename
                    or not filename.lower().endswith(".zip")
                    or not digest
                ):
                    continue
                identity = (filename, digest)
                if identity in seen_archives:
                    continue
                archive_path = archive_root / filename
                try:
                    if (
                        archive_path.is_symlink()
                        or not archive_path.is_file()
                        or archive_path.stat().st_size != int(record.get("size", -1))
                        or archive_path.stat().st_mode & 0o222
                    ):
                        continue
                    world_name = self._legacy_archive_world_name(archive_path)
                    created_at = str(record.get("created_at_utc") or summary.created_at)
                    points.setdefault(world_name, []).append(
                        WorldRecoveryPoint(
                            generation_id=summary.generation_id,
                            created_at=created_at,
                            world_name=world_name,
                            active=False,
                            size_bytes=int(record.get("size", 0)),
                            legacy_archive_filename=filename,
                        )
                    )
                    seen_archives.add(identity)
                except (OSError, ValueError, TypeError, FleetBackupError):
                    continue

        for world_points in points.values():
            world_points.sort(key=lambda item: item.created_at, reverse=True)
        return points

    def export_world_archive(
        self,
        generation_id: str,
        instance_id: str,
        world_name: str,
        archive_path: Path,
        *,
        deep_verify: bool = True,
        legacy_archive_filename: str | None = None,
    ) -> dict[str, Any]:
        """Export one verified world restore point as a temporary ZIP.

        Folder snapshots and previously saved-world ZIPs are both held inside
        immutable fleet generations. This method materializes only the chosen
        world and bridges it to BackupManager's rollback-safe world swap.
        """
        try:
            world_root = _safe_relative(world_name)
        except FleetBackupError as exc:
            raise FleetBackupError(
                f"Unsafe world name in restore request: {world_name!r}"
            ) from exc
        if (
            len(world_root.parts) != 1
            or world_name in {".", ".."}
            or "\\" in world_name
            or "\x00" in world_name
        ):
            raise FleetBackupError(
                f"Unsafe world name in restore request: {world_name!r}"
            )

        if not generation_id or Path(generation_id).name != generation_id:
            raise FleetBackupError("Invalid fleet generation ID in world restore request.")
        source_generation = self.generations_dir / generation_id
        verification = self.verify_generation(source_generation, deep=deep_verify)
        if not verification["ok"]:
            raise FleetBackupError(
                "Refusing to restore an invalid generation: "
                + "; ".join(verification["errors"][:8])
            )
        server = next(
            (
                item for item in verification["manifest"].get("instances", [])
                if isinstance(item, dict)
                and str(item.get("id")) == str(instance_id)
            ),
            None,
        )
        if server is None:
            raise FleetBackupError(
                f"Generation {generation_id} does not contain server {instance_id}."
            )

        target = Path(archive_path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = Path(os.path.abspath(target))
        if target.exists() or target.is_symlink():
            raise FleetBackupError(f"Temporary world archive already exists: {target}")
        for instance in self.instances:
            live_server = Path(instance.path).expanduser().resolve()
            if target == live_server or live_server in target.parents:
                raise FleetBackupError(
                    "Refusing to write a temporary recovery archive inside a live server folder."
                )
        current = Path(target.parent.anchor or "/")
        for component in target.parent.parts[1:]:
            current = current / component
            if current.is_symlink():
                raise FleetBackupError(
                    f"Refusing a symbolic link in the temporary archive path: {current}"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        if partial.exists() or partial.is_symlink():
            raise FleetBackupError(f"Temporary archive staging path already exists: {partial}")

        try:
            if legacy_archive_filename is not None:
                if (
                    Path(legacy_archive_filename).name != legacy_archive_filename
                    or not legacy_archive_filename.lower().endswith(".zip")
                ):
                    raise FleetBackupError("Unsafe saved-world archive filename.")
                record = next(
                    (
                        item for item in server.get("legacy_world_archives", [])
                        if isinstance(item, dict)
                        and item.get("filename") == legacy_archive_filename
                    ),
                    None,
                )
                if record is None:
                    raise FleetBackupError(
                        f"Generation {generation_id} does not contain saved-world archive "
                        f"{legacy_archive_filename!r}."
                    )
                source = (
                    source_generation
                    / "servers"
                    / _server_key(str(instance_id))
                    / "legacy-world-archives"
                    / legacy_archive_filename
                )
                expected_hash = str(record.get("sha256") or "")
                if (
                    source.is_symlink()
                    or not source.is_file()
                    or source.stat().st_size != int(record.get("size", -1))
                    or not expected_hash
                    or _hash_file(source) != expected_hash
                ):
                    raise FleetBackupError(
                        f"Saved-world archive {legacy_archive_filename} failed its generation checksum."
                    )
                if self._legacy_archive_world_name(source) != world_name:
                    raise FleetBackupError(
                        "The selected saved-world archive does not match the requested world."
                    )
                shutil.copyfile(source, partial)
                digest = _hash_file(partial)
                created_at = str(record.get("created_at_utc") or verification["manifest"]["created_at_utc"])
            else:
                world = next(
                    (
                        item for item in server.get("world_saves", [])
                        if isinstance(item, dict) and item.get("name") == world_name
                    ),
                    None,
                )
                if world is None:
                    raise FleetBackupError(
                        f"Generation {generation_id} does not contain world {world_name!r}."
                    )
                matching_files = [
                    item for item in server.get("files", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and str(item["path"]).split("/", 1)[0] in {
                        world_name, f"{world_name}_nether", f"{world_name}_the_end"
                    }
                ]
                level_dat_record = next(
                    (
                        item for item in matching_files
                        if item["path"] == f"{world_name}/level.dat"
                    ),
                    None,
                )
                if level_dat_record is None or int(level_dat_record.get("size", 0)) <= 0:
                    raise FleetBackupError(
                        f"World {world_name!r} has no valid level.dat in generation {generation_id}."
                    )

                server_files_root = (
                    source_generation / "servers" / _server_key(str(instance_id)) / "files"
                )
                allowed_roots = {
                    world_name, f"{world_name}_nether", f"{world_name}_the_end"
                }
                with zipfile.ZipFile(
                    partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True
                ) as archive:
                    for directory in server.get("directories", []):
                        if not isinstance(directory, dict):
                            continue
                        relative = _safe_relative(str(directory.get("path", "")))
                        if relative.parts[0] not in allowed_roots:
                            continue
                        source = server_files_root.joinpath(*relative.parts)
                        if source.is_symlink() or not source.is_dir():
                            raise FleetBackupError(
                                f"World directory is missing or unsafe: {relative}"
                            )
                        archive.write(source, relative.as_posix() + "/")
                    for record in matching_files:
                        relative = _safe_relative(str(record["path"]))
                        source = server_files_root.joinpath(*relative.parts)
                        if source.is_symlink() or not source.is_file():
                            raise FleetBackupError(
                                f"World file is missing or unsafe: {relative}"
                            )
                        archive.write(source, relative.as_posix())
                digest = _hash_file(partial)
                created_at = str(verification["manifest"]["created_at_utc"])

            if not partial.is_file() or partial.is_symlink():
                raise FleetBackupError("Temporary world archive was not created safely.")
            os.replace(partial, target)
            return {
                "path": target,
                "filename": target.name,
                "size_bytes": target.stat().st_size,
                "sha256": digest,
                "world_name": world_name,
                "generation_id": generation_id,
                "created_at": created_at,
            }
        except Exception:
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

    def _latest_generation(self) -> tuple[Path, dict[str, Any]] | None:
        for summary in self.list_generations():
            manifest = self._load_manifest(summary.path)
            if manifest is not None:
                return summary.path, manifest
        return None

    def _cleanup_migrated_legacy_archives(
        self,
        retained: list[GenerationSummary],
    ) -> tuple[int, int, int, int, list[str]]:
        """Remove the old external ZIP only after both retained generations preserve it."""
        if len(retained) < DEFAULT_KEEP_GENERATIONS:
            return (
                0,
                0,
                self._count_legacy_sources()[0],
                self._count_legacy_sources()[1],
                [],
            )

        generation_records: list[tuple[GenerationSummary, dict[str, Any]]] = []
        for summary in retained[:DEFAULT_KEEP_GENERATIONS]:
            manifest = self._load_manifest(summary.path)
            if manifest is None:
                return (
                    0,
                    0,
                    self._count_legacy_sources()[0],
                    self._count_legacy_sources()[1],
                    [f"Could not reload generation manifest: {summary.generation_id}"],
                )
            generation_records.append((summary, manifest))

        removed_count = 0
        removed_bytes = 0
        errors: list[str] = []
        for instance in self.instances:
            backup_dir = self._legacy_backup_dir(instance)
            if backup_dir.is_symlink() or not backup_dir.is_dir():
                continue
            for source in sorted(
                backup_dir.glob("*.zip"), key=lambda item: item.name.casefold()
            ):
                if source.is_symlink() or not source.is_file():
                    continue
                records: list[dict[str, Any]] = []
                stored_copies: list[tuple[Path, dict[str, Any]]] = []
                for summary, manifest in generation_records:
                    server = next(
                        (
                            item
                            for item in manifest.get("instances", [])
                            if isinstance(item, dict)
                            and str(item.get("id")) == str(instance.id)
                        ),
                        None,
                    )
                    match = next(
                        (
                            item
                            for item in (server or {}).get("legacy_world_archives", [])
                            if isinstance(item, dict)
                            and item.get("filename") == source.name
                        ),
                        None,
                    )
                    if match is None:
                        stored_copies = []
                        break
                    stored_copies.append((summary.path, match))

                if len(stored_copies) != DEFAULT_KEEP_GENERATIONS:
                    continue
                records = [record for _generation, record in stored_copies]
                hashes = {str(item.get("sha256", "")) for item in records}
                if len(hashes) != 1 or not next(iter(hashes)):
                    continue

                try:
                    digest = next(iter(hashes))
                    source_info = source.stat(follow_symlinks=False)
                    source_signature = _stat_signature(source_info)
                    if not all(
                        _same_signature(item.get("source_stat"), source_signature)
                        for item in records
                    ):
                        continue

                    # A shallow manifest/size check is not enough to retire
                    # the only external original. Verify the actual immutable
                    # archive bytes in BOTH retained generations first.
                    copies_valid = True
                    for generation_path, record in stored_copies:
                        relative = _safe_relative(str(record.get("path", "")))
                        expected_relative = PurePosixPath(
                            "legacy-world-archives", source.name
                        )
                        if relative != expected_relative:
                            copies_valid = False
                            errors.append(
                                f"{instance.name}/{source.name}: retained archive path "
                                f"is invalid in generation {generation_path.name}"
                            )
                            break
                        stored_path = (
                            generation_path
                            / "servers"
                            / _server_key(str(instance.id))
                            / "legacy-world-archives"
                            / source.name
                        )
                        if (
                            stored_path.is_symlink()
                            or not stored_path.is_file()
                            or stored_path.stat(follow_symlinks=False).st_size
                            != int(record.get("size", -1))
                            or stored_path.stat(follow_symlinks=False).st_mode & 0o222
                            or _hash_file(stored_path) != digest
                        ):
                            copies_valid = False
                            errors.append(
                                f"{instance.name}/{source.name}: retained archive bytes "
                                f"failed SHA-256 verification in generation "
                                f"{generation_path.name}; legacy source was preserved"
                            )
                            break
                    if not copies_valid:
                        continue
                    if _hash_file(source) != digest:
                        continue
                    if not _same_signature(
                        _stat_signature(source.stat(follow_symlinks=False)),
                        source_signature,
                    ):
                        continue

                    source.unlink()
                    metadata = source.with_name(source.name + ".json")
                    if metadata.is_file() and not metadata.is_symlink():
                        metadata.unlink()
                    removed_count += 1
                    removed_bytes += int(source_info.st_size)
                except OSError as exc:
                    errors.append(
                        f"{instance.name}/{source.name}: could not retire migrated source: {exc}"
                    )

        pending_count, pending_bytes = self._count_legacy_sources()
        return removed_count, removed_bytes, pending_count, pending_bytes, errors

    def _cleanup_migrated_legacy_sources(
        self,
        retained: list[GenerationSummary],
    ) -> tuple[int, int, int, int, list[str]]:
        """Retire unmapped/manual legacy sources only after two matching snapshots."""
        blockers = self.legacy_backup_blockers()
        pending_count = len(blockers)
        pending_bytes = sum(int(item.get("bytes", 0)) for item in blockers)
        if len(retained) < DEFAULT_KEEP_GENERATIONS or not blockers:
            return 0, 0, pending_count, pending_bytes, []

        generation_records: list[tuple[GenerationSummary, dict[str, Any]]] = []
        for summary in retained[:DEFAULT_KEEP_GENERATIONS]:
            manifest = self._load_manifest(summary.path)
            if manifest is None:
                return (
                    0,
                    0,
                    pending_count,
                    pending_bytes,
                    [f"Could not reload generation manifest: {summary.generation_id}"],
                )
            generation_records.append((summary, manifest))

        removed_count = 0
        removed_bytes = 0
        errors: list[str] = []

        for blocker in blockers:
            source_name = str(blocker["path"])
            source, _ = self._legacy_source_location(source_name)
            stored_sources: list[tuple[Path, dict[str, Any]]] = []
            for summary, manifest in generation_records:
                match = next(
                    (
                        item
                        for item in manifest.get("legacy_sources", [])
                        if isinstance(item, dict) and item.get("source") == source_name
                    ),
                    None,
                )
                if match is None:
                    stored_sources = []
                    break
                stored_sources.append((summary.path, match))
            source_records = [record for _generation_path, record in stored_sources]

            if len(source_records) != DEFAULT_KEEP_GENERATIONS:
                continue
            if any(item.get("excluded") for item in source_records):
                continue
            if len({item.get("tree_sha256") for item in source_records}) != 1:
                continue
            if len({item.get("source_type") for item in source_records}) != 1:
                continue

            record_maps = [
                {
                    str(file_item["source_path"]): file_item
                    for file_item in item.get("files", [])
                    if isinstance(file_item, dict)
                    and isinstance(file_item.get("source_path"), str)
                }
                for item in source_records
            ]
            if record_maps[0].keys() != record_maps[1].keys():
                continue
            if source.is_symlink() or not source.exists():
                continue

            try:
                if source.is_file():
                    current_files = {source_name: source}
                    is_directory = False
                elif source.is_dir():
                    current_files: dict[str, Path] = {}
                    unsafe = False
                    for current_text, dir_names, file_names in os.walk(
                        source, topdown=True, followlinks=False
                    ):
                        current = Path(current_text)
                        safe_dirs: list[str] = []
                        for name in dir_names:
                            candidate = current / name
                            if candidate.is_symlink():
                                unsafe = True
                            else:
                                safe_dirs.append(name)
                        dir_names[:] = safe_dirs
                        for name in file_names:
                            candidate = current / name
                            if candidate.is_symlink():
                                unsafe = True
                                continue
                            info = candidate.stat(follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode):
                                unsafe = True
                                continue
                            relative = str(
                                PurePosixPath(source_name)
                                / PurePosixPath(
                                    candidate.relative_to(source).as_posix()
                                )
                            )
                            current_files[relative] = candidate
                    is_directory = True
                    if unsafe:
                        continue
                else:
                    continue

                if current_files.keys() != record_maps[0].keys():
                    continue

                total_bytes = 0
                source_is_current = True
                source_signatures: dict[str, dict[str, int]] = {}
                for relative, source_file in current_files.items():
                    file_records = [records[relative] for records in record_maps]
                    info = source_file.stat(follow_symlinks=False)
                    signature = _stat_signature(info)
                    expected_hashes = {
                        str(item.get("sha256", "")) for item in file_records
                    }
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or len(expected_hashes) != 1
                        or not next(iter(expected_hashes))
                        or not all(
                            _same_signature(item.get("source_stat"), signature)
                            and int(item.get("size", -1)) == info.st_size
                            for item in file_records
                        )
                    ):
                        source_is_current = False
                        break
                    if _hash_file(source_file) != next(iter(expected_hashes)):
                        source_is_current = False
                        break
                    source_signatures[relative] = signature
                    total_bytes += info.st_size

                if not source_is_current:
                    continue

                # The matching manifest fingerprints and a healthy external
                # source do not prove that both stored copies still contain
                # those bytes. Hash every actual file in both retained
                # generations before retiring a historical/manual source.
                source_root_relative = _safe_relative(source_name)
                expected_source_type = "directory" if is_directory else "file"
                if any(
                    item.get("source_type") != expected_source_type
                    for item in source_records
                ):
                    continue
                stored_copies_valid = True
                for generation_path, source_record in stored_sources:
                    stored_map = {
                        str(item["source_path"]): item
                        for item in source_record.get("files", [])
                        if isinstance(item, dict)
                        and isinstance(item.get("source_path"), str)
                    }
                    if stored_map.keys() != record_maps[0].keys():
                        stored_copies_valid = False
                        break
                    for source_file_name, file_entry in stored_map.items():
                        source_file_relative = _safe_relative(source_file_name)
                        if is_directory:
                            inside_parts = source_file_relative.parts[
                                len(source_root_relative.parts) :
                            ]
                            if (
                                source_file_relative.parts[
                                    : len(source_root_relative.parts)
                                ]
                                != source_root_relative.parts
                                or not inside_parts
                            ):
                                stored_copies_valid = False
                                break
                            expected_snapshot_path = (
                                PurePosixPath("legacy-sources")
                                / source_root_relative
                                / PurePosixPath(*inside_parts)
                            )
                        else:
                            expected_snapshot_path = (
                                PurePosixPath("legacy-sources")
                                / source_root_relative
                            )
                            if source_file_relative != source_root_relative:
                                stored_copies_valid = False
                                break

                        try:
                            recorded_path = _safe_relative(
                                str(file_entry.get("path", ""))
                            )
                            if recorded_path != expected_snapshot_path:
                                stored_copies_valid = False
                                break
                            stored_path = generation_path.joinpath(
                                *recorded_path.parts
                            )
                            if (
                                stored_path.is_symlink()
                                or not stored_path.is_file()
                            ):
                                stored_copies_valid = False
                                break
                            stored_info = stored_path.stat(follow_symlinks=False)
                            if (
                                not stat.S_ISREG(stored_info.st_mode)
                                or stored_info.st_size
                                != int(file_entry.get("size", -1))
                                or stored_info.st_mode & 0o222
                                or not file_entry.get("sha256")
                                or _hash_file(stored_path)
                                != str(file_entry["sha256"])
                            ):
                                stored_copies_valid = False
                                break
                            parent = generation_path
                            for component in recorded_path.parts[:-1]:
                                parent = parent / component
                                if parent.is_symlink() or not parent.is_dir():
                                    stored_copies_valid = False
                                    break
                            if not stored_copies_valid:
                                break
                        except (OSError, KeyError, ValueError, TypeError, FleetBackupError):
                            stored_copies_valid = False
                            break
                    if not stored_copies_valid:
                        errors.append(
                            f"{source_name}: retained legacy-source bytes failed "
                            f"SHA-256 verification in generation {generation_path.name}; "
                            "the original source was preserved"
                        )
                        break
                if not stored_copies_valid:
                    continue

                # Close the final enumeration-to-delete window. A new file
                # may have appeared after the source hashes were checked; do
                # not let rmtree silently discard data absent from the two
                # retained manifests.
                source_changed = source.is_symlink()
                if is_directory:
                    final_files: dict[str, Path] = {}
                    if not source.is_dir():
                        source_changed = True
                    else:
                        for current_text, dir_names, file_names in os.walk(
                            source, topdown=True, followlinks=False
                        ):
                            current = Path(current_text)
                            safe_dirs: list[str] = []
                            for name in dir_names:
                                candidate = current / name
                                if candidate.is_symlink():
                                    source_changed = True
                                else:
                                    safe_dirs.append(name)
                            dir_names[:] = safe_dirs
                            for name in file_names:
                                candidate = current / name
                                if candidate.is_symlink():
                                    source_changed = True
                                    continue
                                info = candidate.stat(follow_symlinks=False)
                                if not stat.S_ISREG(info.st_mode):
                                    source_changed = True
                                    continue
                                relative = str(
                                    PurePosixPath(source_name)
                                    / PurePosixPath(
                                        candidate.relative_to(source).as_posix()
                                    )
                                )
                                final_files[relative] = candidate
                    if final_files.keys() != current_files.keys():
                        source_changed = True
                    if not source_changed:
                        for relative, candidate in final_files.items():
                            if not _same_signature(
                                source_signatures.get(relative),
                                _stat_signature(candidate.stat(follow_symlinks=False)),
                            ):
                                source_changed = True
                                break
                else:
                    try:
                        final_info = source.stat(follow_symlinks=False)
                        source_changed = (
                            source_changed
                            or not stat.S_ISREG(final_info.st_mode)
                            or not _same_signature(
                                source_signatures.get(source_name),
                                _stat_signature(final_info),
                            )
                        )
                    except OSError:
                        source_changed = True

                if source_changed:
                    errors.append(
                        f"{source_name}: legacy source changed after verification; "
                        "the original was preserved for the next backup cycle"
                    )
                    continue

                if is_directory:
                    shutil.rmtree(source)
                else:
                    source.unlink()
                removed_count += 1
                removed_bytes += int(blocker.get("bytes", total_bytes))
            except OSError as exc:
                errors.append(f"{source_name}: could not retire migrated source: {exc}")

        pending = self.legacy_backup_blockers()
        pending_count = len(pending)
        pending_bytes = sum(int(item.get("bytes", 0)) for item in pending)
        return removed_count, removed_bytes, pending_count, pending_bytes, errors

    def _count_legacy_sources(self) -> tuple[int, int]:
        count = 0
        total = 0
        for instance in self.instances:
            backup_dir = self._legacy_backup_dir(instance)
            if backup_dir.is_symlink() or not backup_dir.is_dir():
                continue
            for source in backup_dir.glob("*.zip"):
                if source.is_symlink() or not source.is_file():
                    continue
                try:
                    count += 1
                    total += source.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        return count, total

    def legacy_source_inventory(self) -> list[dict[str, Any]]:
        """List old backup stores outside the current-ID ZIP migration path.

        This deliberately does not delete, move, or silently assign retired-ID
        archives. A previous installation may have belonged to a different
        server, and a same-looking filename is not enough proof for restoring
        its world into a current instance. Sources are surfaced in the Backup
        UI until they are explicitly mapped/migrated.
        """
        base = self._legacy_backup_base()
        if base.is_symlink():
            raise FleetBackupError(
                f"Legacy backup store must not be a symbolic link: {base}"
            )
        if base.exists() and not base.is_dir():
            raise FleetBackupError(f"Legacy backup store is not a directory: {base}")

        instances = list(self.instances)
        active_ids = {str(item.id) for item in instances}
        slugs = {
            re.sub(r"[^a-z0-9]+", "-", str(item.name).casefold()).strip("-"): item
            for item in instances
        }

        def matching_instance(name: str):
            lowered = name.casefold()
            matches = [
                item
                for slug, item in slugs.items()
                if slug and (lowered == slug or lowered.startswith(slug + "-"))
            ]
            return matches[0] if len(matches) == 1 else None

        def measure_directory(folder: Path) -> tuple[int, int]:
            total = 0
            count = 0
            for current, dir_names, file_names in os.walk(
                folder, topdown=True, followlinks=False
            ):
                current_path = Path(current)
                dir_names[:] = [
                    name for name in dir_names if not (current_path / name).is_symlink()
                ]
                for name in file_names:
                    candidate = current_path / name
                    if candidate.is_symlink():
                        continue
                    try:
                        info = candidate.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        count += 1
                        total += info.st_size
            return total, count

        inventory: list[dict[str, Any]] = []

        def add(
            source: Path,
            *,
            kind: str,
            instance=None,
            status: str,
            reason: str,
            source_key: str | None = None,
        ) -> None:
            try:
                info = source.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    size, count = measure_directory(source)
                elif stat.S_ISREG(info.st_mode):
                    size, count = info.st_size, 1
                else:
                    return
            except OSError:
                return
            inventory.append(
                {
                    "path": source_key or source.relative_to(base).as_posix(),
                    "kind": kind,
                    "instance_id": str(instance.id) if instance is not None else None,
                    "instance_name": str(instance.name)
                    if instance is not None
                    else None,
                    "status": status,
                    "bytes": size,
                    "files": count,
                    "reason": reason,
                }
            )

        try:
            entries = (
                sorted(base.iterdir(), key=lambda item: item.name.casefold())
                if base.is_dir()
                else []
            )
        except OSError as exc:
            raise FleetBackupError(
                f"Could not enumerate legacy backup sources in {base}: {exc}"
            ) from exc

        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name == "manual":
                    try:
                        manual_entries = sorted(
                            entry.iterdir(), key=lambda item: item.name.casefold()
                        )
                    except OSError:
                        continue
                    for source in manual_entries:
                        if source.is_symlink() or not source.is_dir():
                            continue
                        if source.name == "failed-imports":
                            for failed in sorted(
                                source.iterdir(), key=lambda item: item.name.casefold()
                            ):
                                if failed.is_dir() and not failed.is_symlink():
                                    add(
                                        failed,
                                        kind="failed_import_data",
                                        status="review",
                                        reason=(
                                            "Partial import data, not a complete "
                                            "registered-server restore point."
                                        ),
                                    )
                            continue
                        instance = matching_instance(source.name)
                        add(
                            source,
                            kind="manual_server_snapshot",
                            instance=instance,
                            status="mapped_pending" if instance else "unassigned",
                            reason=(
                                "Historical full-server folder is not yet represented "
                                "inside the two retained fleet generations."
                                if instance
                                else "Server ownership could not be matched safely."
                            ),
                        )
                    continue

                try:
                    retired_id = str(uuid.UUID(entry.name))
                except ValueError:
                    continue
                if retired_id in active_ids:
                    # The normal per-current-instance migration already
                    # accounts for this folder in each new generation.
                    continue
                archives = [
                    item
                    for item in entry.glob("*.zip")
                    if item.is_file() and not item.is_symlink()
                ]
                if archives:
                    # Preserve the entire retired-ID backup folder as one
                    # opaque source. This includes the ZIPs and their JSON slot
                    # labels without guessing which active server owns them.
                    add(
                        entry,
                        kind="retired_instance_backup_store",
                        status="unassigned",
                        reason=(
                            f"Historical backup data for retired instance ID {retired_id}; "
                            "preserved as opaque legacy material without assigning it "
                            "to a current server."
                        ),
                    )
                continue

            if not entry.is_file():
                continue
            lowered = entry.name.casefold()
            if not lowered.endswith((".tar.gz", ".tgz")) or "world" not in lowered:
                continue
            instance = matching_instance(entry.name)
            add(
                entry,
                kind="flat_legacy_world_archive",
                instance=instance,
                status="mapped_pending" if instance else "unassigned",
                reason=(
                    "Flat legacy world archive needs a safe archive conversion and "
                    "two-generation migration."
                    if instance
                    else "Archive filename does not identify one registered server."
                ),
            )

        # Capture named backup roots which live outside the default Crucible
        # backup directory. The paths remain opaque and unassigned unless a
        # separate explicit mapping exists.
        for source_key, source, kind, reason in self._additional_legacy_backup_roots():
            if source.is_symlink():
                continue
            add(
                source,
                kind=kind,
                status="unassigned",
                reason=reason,
                source_key=source_key,
            )

        inventory.sort(key=lambda item: str(item["path"]).casefold())
        return inventory

    def legacy_backup_blockers(self) -> list[dict[str, Any]]:
        """Return historical backup sources that must be migrated before scheduling.

        Partial failed-import folders are reported for review but are not
        complete server/world backup restore points. Retired-ID archives,
        manual server snapshots and flat world archives are blockers because
        enabling an unattended timer while those copies remain outside the
        two-generation set would make the visible retention promise false.
        """
        return [
            item
            for item in self.legacy_source_inventory()
            if item["kind"] != "failed_import_data"
        ]

    def _cleanup_stale_staging(self) -> None:
        """Remove abandoned partial generations only while holding the fleet lock."""
        for candidate in self.staging_dir.iterdir():
            try:
                if candidate.is_symlink() or not candidate.is_dir():
                    candidate.unlink(missing_ok=True)
                else:
                    self._make_generation_removable(candidate)
                    shutil.rmtree(candidate)
            except OSError as exc:
                raise FleetBackupError(
                    f"Could not clean abandoned fleet backup staging data {candidate}: {exc}"
                ) from exc

    @staticmethod
    def _seal_generation(generation: Path) -> None:
        """Make every stored file and directory read-only before publication."""
        for current_text, dir_names, file_names in os.walk(
            generation, topdown=False, followlinks=False
        ):
            current = Path(current_text)
            for name in file_names:
                candidate = current / name
                if candidate.is_symlink():
                    raise FleetBackupError(
                        f"Symbolic link appeared in fleet snapshot staging: {candidate}"
                    )
                try:
                    mode = stat.S_IMODE(candidate.stat(follow_symlinks=False).st_mode)
                    candidate.chmod(mode & 0o555 | 0o400)
                except OSError as exc:
                    raise FleetBackupError(
                        f"Could not seal fleet snapshot file {candidate}: {exc}"
                    ) from exc
            for name in dir_names:
                candidate = current / name
                if candidate.is_symlink():
                    raise FleetBackupError(
                        f"Symbolic link appeared in fleet snapshot staging: {candidate}"
                    )
                try:
                    candidate.chmod(0o555)
                except OSError as exc:
                    raise FleetBackupError(
                        f"Could not seal fleet snapshot directory {candidate}: {exc}"
                    ) from exc
            current.chmod(0o555)
            # Persist each directory's child entries bottom-up. File contents
            # are fsynced while copied; without directory fsyncs, a power loss
            # could still lose names after the manifest had been published.
            FleetBackupManager._fsync_directory_strict(current)

    @staticmethod
    def _make_generation_removable(generation: Path) -> None:
        """Restore directory write permissions so a retired generation can be unlinked."""
        for current_text, dir_names, _file_names in os.walk(
            generation, topdown=True, followlinks=False
        ):
            current = Path(current_text)
            try:
                current.chmod(0o700)
            except OSError:
                pass
            for name in dir_names:
                candidate = current / name
                if not candidate.is_symlink():
                    try:
                        candidate.chmod(0o700)
                    except OSError:
                        pass

    def _prune_after_publish(self) -> list[str]:
        summaries = self.list_generations()
        removed: list[str] = []
        for old in summaries[DEFAULT_KEEP_GENERATIONS:]:
            try:
                self._make_generation_removable(old.path)
                shutil.rmtree(old.path)
                removed.append(old.generation_id)
            except OSError as exc:
                raise FleetBackupError(
                    f"New generation committed, but the oldest generation "
                    f"{old.generation_id} could not be pruned: {exc}"
                ) from exc
        return removed

    # ------------------------------------------------------------------
    # One server snapshot
    # ------------------------------------------------------------------

    def _snapshot_instance(
        self,
        stage: Path,
        instance: ServerInstance,
        previous_manifest: dict[str, Any] | None,
    ) -> dict[str, Any]:
        server_root = Path(instance.path).expanduser()
        if server_root.is_symlink():
            raise FleetBackupError(
                f"{instance.name}: the registered server folder is a symbolic link; refusing to back up through it."
            )
        try:
            server_root = server_root.resolve(strict=True)
        except OSError as exc:
            raise FleetBackupError(
                f"{instance.name}: server folder is unavailable: {exc}"
            ) from exc
        if not server_root.is_dir():
            raise FleetBackupError(
                f"{instance.name}: server folder is not a directory."
            )

        status = self.tmux.get_status(instance)
        was_saving_disabled = False
        save_off_confirmed = False
        if status not in ("running", "stopped"):
            raise FleetBackupError(
                f"{instance.name}: status is {status!r}; only confirmed running/stopped servers can be backed up."
            )

        if status == "running":
            was_saving_disabled = self._quiesce_running_server(instance)
            save_off_confirmed = True

        try:
            snapshot = self._copy_server_tree(
                stage=stage,
                instance=instance,
                server_root=server_root,
                capture_status=status,
                previous_manifest=previous_manifest,
            )
            final_status = self.tmux.get_status(instance)
            if final_status != status:
                raise FleetBackupError(
                    f"{instance.name}: server status changed from {status!r} "
                    f"to {final_status!r} during the snapshot; the generation was not committed."
                )
            return snapshot
        finally:
            if status == "running" and save_off_confirmed and not was_saving_disabled:
                self._restore_autosave(instance)

    def _quiesce_running_server(self, instance: ServerInstance) -> bool:
        """Flush the active world, disable periodic saves, then flush once more.

        Returns True if the server was already in save-off state before this
        run. Such a server is left exactly as found; Crucible never changes an
        owner's deliberately-disabled autosave setting.
        """
        try:
            disabled_reply = self._send_console_and_wait(
                instance,
                "save-off",
                re.compile(
                    r"(?:automatic saving is now disabled|saving is already turned off)",
                    re.IGNORECASE,
                ),
            )
        except ConsoleCommandUnacknowledged as exc:
            # The command was accepted, so the server may now be in save-off
            # state even though its confirmation was lost. The prior state is
            # unknowable; prefer restoring normal saving over leaving a live
            # world frozen indefinitely. A failed recovery is called out
            # explicitly so the owner can inspect the console.
            try:
                if self.tmux.get_status(instance) != "running":
                    raise FleetBackupError(
                        "The server is no longer confirmed running; save-on could not be sent."
                    )
                self._restore_autosave(instance)
            except Exception as restore_exc:
                raise FleetBackupError(
                    f"{instance.name}: save-off confirmation was not received and "
                    "Crucible could not confirm that normal autosaving was restored. "
                    "The save state is unknown; inspect the server console before continuing. "
                    f"Recovery failure: {restore_exc}"
                ) from restore_exc
            raise FleetBackupError(
                f"{instance.name}: save-off was sent but its confirmation was lost; "
                "Crucible sent save-on as a safety recovery. No backup was created."
            ) from exc
        already_off = "already" in disabled_reply.casefold()

        try:
            self._send_console_and_wait(
                instance,
                "save-all",
                re.compile(
                    r"(?:saved the game|unable to save the game)", re.IGNORECASE
                ),
                success_pattern=re.compile(r"saved the game", re.IGNORECASE),
            )
        except Exception as exc:
            original_error = str(exc)
            if not already_off:
                try:
                    self._restore_autosave(instance)
                except Exception as restore_exc:
                    raise FleetBackupError(
                        f"{instance.name}: world flush failed and autosaving could not be re-enabled. "
                        "The server may still be in save-off mode; re-enable autosaving in its console "
                        f"after checking the state. Original failure: {original_error}. "
                        f"Restore failure: {restore_exc}"
                    ) from restore_exc
            raise
        return already_off

    def _restore_autosave(self, instance: ServerInstance) -> None:
        self._send_console_and_wait(
            instance,
            "save-on",
            re.compile(
                r"(?:automatic saving is now enabled|saving is already turned on)",
                re.IGNORECASE,
            ),
        )

    def _send_console_and_wait(
        self,
        instance: ServerInstance,
        command: str,
        expected: re.Pattern[str],
        *,
        success_pattern: re.Pattern[str] | None = None,
    ) -> str:
        try:
            before = self.tmux.capture_pane_tail(
                instance, max_lines=300, max_chars=16000
            )
        except TypeError:
            before = self.tmux.capture_pane_tail(instance)
        ok, detail = self.tmux.send_command_result(instance, command)
        if not ok:
            raise FleetBackupError(
                f"{instance.name}: could not send {command!r} to the server console: {detail}"
            )

        deadline = time.monotonic() + self.command_timeout_s
        last_text = before
        while time.monotonic() < deadline:
            try:
                current_status = self.tmux.get_status(instance)
            except Exception as exc:
                raise ConsoleCommandUnacknowledged(
                    f"{instance.name}: could not re-check server state after sending {command!r}: {exc}"
                ) from exc
            if current_status != "running":
                raise ConsoleCommandUnacknowledged(
                    f"{instance.name}: server stopped or became unmanageable while waiting for {command!r}."
                )
            try:
                try:
                    after = self.tmux.capture_pane_tail(
                        instance, max_lines=300, max_chars=16000
                    )
                except TypeError:
                    after = self.tmux.capture_pane_tail(instance)
            except Exception as exc:
                raise ConsoleCommandUnacknowledged(
                    f"{instance.name}: console output became unavailable after sending {command!r}: {exc}"
                ) from exc
            added = self._pane_delta(last_text, after)
            match = expected.search(added)
            if match:
                if success_pattern is not None and not success_pattern.search(added):
                    raise FleetBackupError(
                        f"{instance.name}: Minecraft reported a failed save: {match.group(0)}"
                    )
                return match.group(0)
            # Some tmux/terminal configurations wrap or trim capture output.
            # Counting occurrences catches a newly-emitted confirmation even
            # when the fixed-size pane tail has shifted.
            for candidate in expected.finditer(after):
                if len(list(expected.finditer(after))) > len(
                    list(expected.finditer(before))
                ):
                    if success_pattern is not None and not success_pattern.search(
                        after
                    ):
                        raise FleetBackupError(
                            f"{instance.name}: Minecraft reported a failed save."
                        )
                    return candidate.group(0)
            last_text = after
            self._sleep(self.poll_interval_s)

        raise ConsoleCommandUnacknowledged(
            f"{instance.name}: did not receive Minecraft confirmation for {command!r} "
            f"within {self.command_timeout_s:.0f} seconds; no snapshot was committed."
        )

    @staticmethod
    def _pane_delta(before: str, after: str) -> str:
        if after.startswith(before):
            return after[len(before) :]
        limit = min(len(before), len(after), 2000)
        for overlap in range(limit, 0, -1):
            if before[-overlap:] == after[:overlap]:
                return after[overlap:]
        # A completely replaced/rotated pane tail has no trustworthy
        # append boundary. Scanning it could accept a stale confirmation from
        # an earlier command; fail closed and let the occurrence-count check
        # below accept only a demonstrably new response.
        return ""

    def _legacy_backup_base(self) -> Path:
        """Locate the old per-instance and manual backup store without creating it."""
        if self.legacy_backup_base is None:
            from .backup_manager import BASE_DIR

            return Path(BASE_DIR).expanduser()
        return self.legacy_backup_base.expanduser()

    def _legacy_source_location(self, source_name: str) -> tuple[Path, Path]:
        """Resolve a stored source key to an original path and its stable root."""
        relative = _safe_relative(source_name)
        if relative.parts[0] == "external":
            if len(relative.parts) < 2:
                raise FleetBackupError(
                    f"Invalid external legacy source path: {source_name}"
                )
            if relative.parts[1] == "ServerBackups":
                root = Path.home() / "ServerBackups"
            elif relative.parts[1] == "CrucibleServers":
                root = Path.home() / "CrucibleServers"
            else:
                raise FleetBackupError(
                    f"Unknown external legacy source root: {source_name}"
                )
            inner = PurePosixPath(*relative.parts[2:])
            return root.joinpath(*inner.parts), root
        root = self._legacy_backup_base()
        return root.joinpath(*relative.parts), root

    def _additional_legacy_backup_roots(self) -> list[tuple[str, Path, str, str]]:
        """Find explicit out-of-registry backup roots on the user's machine.

        These are intentionally limited to the named ServerBackups root and
        clearly named pre-update/preflight directories adjacent to managed
        servers. Arbitrary unregistered directories are never swept into
        automatic backup retention.
        """
        if self.legacy_backup_base is not None:
            return []
        candidates: list[tuple[str, Path, str, str]] = []
        server_backups = Path.home() / "ServerBackups"
        if server_backups.is_dir() and not server_backups.is_symlink():
            candidates.append(
                (
                    "external/ServerBackups",
                    server_backups,
                    "external_backup_root",
                    "Historical manual/rollback backup collection. Preserved as opaque files; ownership is not inferred.",
                )
            )

        servers_root = Path.home() / "CrucibleServers"
        active_paths = {
            Path(instance.path).expanduser().resolve() for instance in self.instances
        }
        if servers_root.is_dir() and not servers_root.is_symlink():
            try:
                children = sorted(
                    servers_root.iterdir(), key=lambda item: item.name.casefold()
                )
            except OSError as exc:
                raise FleetBackupError(
                    f"Could not inspect unregistered server rollback roots: {exc}"
                ) from exc
            for child in children:
                lowered = child.name.casefold()
                if (
                    child.is_symlink()
                    or not child.is_dir()
                    or child.resolve() in active_paths
                    or not (
                        ".pre-update-" in lowered
                        or "preflight" in lowered
                        or ".rollback-" in lowered
                    )
                ):
                    continue
                candidates.append(
                    (
                        f"external/CrucibleServers/{child.name}",
                        child,
                        "unregistered_server_rollback",
                        "Unregistered pre-update/preflight server snapshot. Preserved opaquely and not assigned to an active server.",
                    )
                )
        return candidates

    def _legacy_backup_dir(self, instance: ServerInstance) -> Path:
        """Locate the old per-instance world ZIP store without creating it."""
        base = self._legacy_backup_base()
        instance_id = str(instance.id)
        if (
            not instance_id
            or instance_id in (".", "..")
            or Path(instance_id).name != instance_id
            or "/" in instance_id
            or "\\" in instance_id
        ):
            raise FleetBackupError(
                f"{instance.name}: unsafe instance ID for legacy backup directory."
            )
        return base / instance_id

    @staticmethod
    def _validate_legacy_world_archive(path: Path) -> None:
        """Validate that a legacy ZIP is a safe, intact, restorable world archive."""
        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = archive.infolist()
                if not infos or len(infos) > 2_000_000:
                    raise FleetBackupError(
                        f"Legacy world archive is empty or has too many entries: {path.name}"
                    )
                roots: set[str] = set()
                has_level_dat = False
                for info in infos:
                    name = info.filename
                    relative = PurePosixPath(name)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if (
                        "\x00" in name
                        or "\\" in name
                        or relative.is_absolute()
                        or ".." in relative.parts
                        or not relative.parts
                    ):
                        raise FleetBackupError(
                            f"Legacy world archive contains an unsafe path: {name!r}"
                        )
                    roots.add(relative.parts[0])
                    file_type = stat.S_IFMT(mode)
                    if stat.S_ISLNK(mode) or (
                        file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                    ):
                        raise FleetBackupError(
                            f"Legacy world archive contains a link/device: {name!r}"
                        )
                    if (
                        len(relative.parts) == 2
                        and relative.parts[1] == "level.dat"
                        and not info.is_dir()
                        and info.file_size > 0
                    ):
                        has_level_dat = True
                if len(roots) != 1 or not has_level_dat:
                    raise FleetBackupError(
                        f"Legacy archive is not a single complete world save: {path.name}"
                    )
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise FleetBackupError(
                        f"Legacy world archive CRC failed at {bad_member}: {path.name}"
                    )
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            if isinstance(exc, FleetBackupError):
                raise
            raise FleetBackupError(
                f"Could not validate legacy world archive {path.name}: {exc}"
            ) from exc

    def _snapshot_archive_file(
        self,
        *,
        source: Path | None,
        destination: Path,
        relative_path: str,
        previous_entry: dict[str, Any] | None,
        previous_path: Path | None,
        instance_name: str,
    ) -> dict[str, Any]:
        """Store one legacy archive as an immutable copy or verified prior-generation link."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source is None:
            if previous_entry is None or previous_path is None:
                raise FleetBackupError(
                    f"{instance_name}: legacy archive source disappeared without a prior snapshot: {relative_path}"
                )
            expected_digest = str(previous_entry.get("sha256", ""))
            if (
                previous_path.is_symlink()
                or not previous_path.is_file()
                or previous_path.stat().st_size != int(previous_entry["size"])
                or previous_path.stat().st_mode & 0o222
                or not expected_digest
                or _hash_file(previous_path) != expected_digest
            ):
                raise FleetBackupError(
                    f"{instance_name}: prior generation's legacy archive is missing, writable or checksum-invalid: {relative_path}"
                )
            try:
                os.link(previous_path, destination)
                reused = True
                self._bytes_reused += int(previous_entry["size"])
            except OSError:
                copied = _copy_and_hash_snapshot(
                    previous_path, destination, str(previous_entry["sha256"])
                )
                self._bytes_copied += copied
                reused = True
            return {
                **previous_entry,
                "path": relative_path,
                "reused_from_previous": reused,
            }

        try:
            info = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise FleetBackupError(
                f"{instance_name}: could not stat legacy archive {source.name}: {exc}"
            ) from exc
        if source.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise FleetBackupError(
                f"{instance_name}: legacy archive is not a regular file: {source.name}"
            )
        current_signature = _stat_signature(info)
        size = int(info.st_size)
        old_digest = str(previous_entry.get("sha256", "")) if previous_entry else ""
        previous_snapshot_valid = False
        if (
            previous_entry is not None
            and previous_path is not None
            and previous_path.is_file()
            and not previous_path.is_symlink()
        ):
            try:
                previous_snapshot_valid = (
                    previous_path.stat().st_size == int(previous_entry["size"])
                    and not (previous_path.stat().st_mode & 0o222)
                    and bool(old_digest)
                    and _hash_file(previous_path) == old_digest
                )
            except (OSError, KeyError, ValueError, TypeError):
                previous_snapshot_valid = False

        if (
            previous_entry is not None
            and previous_path is not None
            and _same_signature(previous_entry.get("source_stat"), current_signature)
            and previous_snapshot_valid
            and previous_path.stat().st_size == size
        ):
            try:
                os.link(previous_path, destination)
                self._bytes_reused += size
                return {
                    "path": relative_path,
                    "size": size,
                    "sha256": old_digest,
                    "mode": stat.S_IMODE(info.st_mode),
                    "source_stat": current_signature,
                    "reused_from_previous": True,
                }
            except OSError:
                copied = _copy_and_hash_snapshot(previous_path, destination, old_digest)
                self._bytes_copied += copied
                return {
                    "path": relative_path,
                    "size": size,
                    "sha256": old_digest,
                    "mode": stat.S_IMODE(info.st_mode),
                    "source_stat": current_signature,
                    "reused_from_previous": True,
                }

        digest, copied, source_signature, copied_size = _copy_and_hash_live(
            source, destination
        )
        self._bytes_copied += copied
        reused = False
        if (
            previous_entry is not None
            and previous_path is not None
            and old_digest == digest
            and previous_snapshot_valid
        ):
            destination.unlink(missing_ok=True)
            try:
                os.link(previous_path, destination)
                self._bytes_reused += copied_size
                self._bytes_copied -= copied
                reused = True
            except OSError:
                fallback_copied = _copy_and_hash_snapshot(
                    previous_path, destination, digest
                )
                self._bytes_copied += fallback_copied
        if not reused and _hash_file(destination) != digest:
            raise FleetBackupError(
                f"{instance_name}: legacy archive checksum mismatch after copy: {source.name}"
            )
        return {
            "path": relative_path,
            "size": copied_size,
            "sha256": digest,
            "mode": stat.S_IMODE(info.st_mode),
            "source_stat": source_signature,
            "reused_from_previous": reused,
        }

    def _snapshot_legacy_sources(
        self,
        *,
        stage: Path,
        previous_manifest: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Carry unmapped retired/manual restore material into each fleet generation.

        These sources are deliberately opaque: their bytes and original relative
        paths are preserved without guessing which live server owns a retired ID.
        Their external originals are retired only after the newest two generations
        contain an identical verified file inventory.
        """
        inventory = self.legacy_source_inventory()
        current_sources = {
            str(item["path"]): item
            for item in inventory
            if item.get("kind") != "failed_import_data"
        }
        excluded = [
            {
                "path": str(item.get("path", "")),
                "kind": str(item.get("kind", "")),
                "reason": str(item.get("reason", "not a complete backup source")),
                "bytes": int(item.get("bytes", 0)),
                "files": int(item.get("files", 0)),
            }
            for item in inventory
            if item.get("kind") == "failed_import_data"
        ]

        previous_sources = {
            str(item["source"]): item
            for item in (
                previous_manifest.get("legacy_sources", []) if previous_manifest else []
            )
            if isinstance(item, dict) and isinstance(item.get("source"), str)
        }
        previous_generation = (
            Path(previous_manifest["_generation_path"])
            if previous_manifest and previous_manifest.get("_generation_path")
            else None
        )

        def source_files(
            source: Path,
        ) -> tuple[list[tuple[Path, PurePosixPath]], list[dict[str, str]]]:
            if source.is_file() and not source.is_symlink():
                return [(source, PurePosixPath(source.name))], []
            if not source.is_dir() or source.is_symlink():
                raise FleetBackupError(
                    f"Legacy backup source is not a regular file or directory: {source}"
                )

            found: list[tuple[Path, PurePosixPath]] = []
            skipped: list[dict[str, str]] = []
            for current_text, dir_names, file_names in os.walk(
                source, topdown=True, followlinks=False
            ):
                current = Path(current_text)
                safe_dirs: list[str] = []
                for name in sorted(dir_names, key=str.casefold):
                    candidate = current / name
                    if candidate.is_symlink():
                        skipped.append(
                            {
                                "path": candidate.relative_to(source).as_posix(),
                                "reason": "symbolic-link directory was not followed",
                            }
                        )
                    else:
                        safe_dirs.append(name)
                dir_names[:] = safe_dirs

                for name in sorted(file_names, key=str.casefold):
                    candidate = current / name
                    relative = PurePosixPath(candidate.relative_to(source).as_posix())
                    if candidate.is_symlink():
                        skipped.append(
                            {
                                "path": relative.as_posix(),
                                "reason": "symbolic-link file was not followed",
                            }
                        )
                        continue
                    try:
                        info = candidate.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise FleetBackupError(
                            f"Could not inspect legacy source file {candidate}: {exc}"
                        ) from exc
                    if not stat.S_ISREG(info.st_mode):
                        skipped.append(
                            {
                                "path": relative.as_posix(),
                                "reason": "non-regular filesystem object was not copied",
                            }
                        )
                        continue
                    found.append((candidate, relative))
            found.sort(key=lambda row: row[1].as_posix().casefold())
            return found, skipped

        results: list[dict[str, Any]] = []
        all_source_names = set(current_sources) | set(previous_sources)
        for source_name in sorted(all_source_names, key=str.casefold):
            source_relative = _safe_relative(source_name)
            source, _ = self._legacy_source_location(source_name)
            source_info = current_sources.get(source_name)
            previous = previous_sources.get(source_name)

            if source_info is not None:
                if source.is_symlink():
                    raise FleetBackupError(
                        f"Legacy backup source became a symbolic link: {source_name}"
                    )
                file_paths, skipped = source_files(source)
                if source.is_dir():
                    source_type = "directory"
                else:
                    source_type = "file"
            else:
                # The old external source was already retired after two
                # verified generations. Carry it forward from the retained
                # immutable copy without recreating an external duplicate.
                file_paths = []
                skipped = []
                source_type = str(previous.get("source_type", "directory"))
                for old_file in previous.get("files", []):
                    old_source_path = _safe_relative(str(old_file["source_path"]))
                    old_relative_to_source = (
                        PurePosixPath(
                            *old_source_path.parts[len(source_relative.parts) :]
                        )
                        if len(old_source_path.parts) > len(source_relative.parts)
                        else PurePosixPath(old_source_path.name)
                    )
                    old_generation_file = (
                        previous_generation / _safe_relative(str(old_file["path"]))
                        if previous_generation is not None
                        else None
                    )
                    if old_generation_file is None or old_generation_file.is_symlink():
                        raise FleetBackupError(
                            f"Previous generation is missing legacy source data: {source_name}"
                        )
                    file_paths.append((old_generation_file, old_relative_to_source))

            old_files = {
                str(item["source_path"]): item
                for item in (previous.get("files", []) if previous else [])
                if isinstance(item, dict) and isinstance(item.get("source_path"), str)
            }
            records: list[dict[str, Any]] = []
            for source_file, relative_inside in file_paths:
                if source_type == "file":
                    old_source_file = str(source_relative)
                else:
                    old_source_file = str(source_relative / relative_inside)

                if source_info is None:
                    old_entry = old_files.get(old_source_file)
                    if old_entry is None:
                        raise FleetBackupError(
                            f"Legacy carry-forward manifest is incomplete: {source_name}/{relative_inside}"
                        )
                    previous_path = source_file
                    live_source: Path | None = None
                else:
                    old_entry = old_files.get(old_source_file)
                    previous_path = (
                        previous_generation / _safe_relative(str(old_entry["path"]))
                        if old_entry is not None and previous_generation is not None
                        else None
                    )
                    live_source = source_file

                if source_type == "file":
                    snapshot_relative = (
                        PurePosixPath("legacy-sources") / source_relative
                    )
                else:
                    snapshot_relative = (
                        PurePosixPath("legacy-sources")
                        / source_relative
                        / relative_inside
                    )
                file_record = self._snapshot_archive_file(
                    source=live_source,
                    destination=stage.joinpath(*snapshot_relative.parts),
                    relative_path=snapshot_relative.as_posix(),
                    previous_entry=old_entry,
                    previous_path=previous_path,
                    instance_name=f"legacy source {source_name}",
                )
                file_record["source_path"] = old_source_file
                records.append(file_record)

            records.sort(key=lambda item: str(item["source_path"]).casefold())
            fingerprint = _sha256_bytes(
                _canonical_json(
                    [
                        {
                            "source_path": item["source_path"],
                            "sha256": item["sha256"],
                            "size": item["size"],
                        }
                        for item in records
                    ]
                )
            )
            source_size = sum(int(item["size"]) for item in records)
            results.append(
                {
                    "source": source_name,
                    "kind": str(
                        source_info.get("kind")
                        if source_info is not None
                        else previous.get("kind", "legacy_backup_source")
                    ),
                    "instance_id": (
                        source_info.get("instance_id")
                        if source_info is not None
                        else previous.get("instance_id")
                    ),
                    "instance_name": (
                        source_info.get("instance_name")
                        if source_info is not None
                        else previous.get("instance_name")
                    ),
                    "status": (
                        source_info.get("status")
                        if source_info is not None
                        else previous.get("status", "preserved")
                    ),
                    "reason": (
                        source_info.get("reason")
                        if source_info is not None
                        else previous.get(
                            "reason", "Preserved in retained fleet generations."
                        )
                    ),
                    "source_type": source_type,
                    "bytes": source_size,
                    "file_count": len(records),
                    "tree_sha256": fingerprint,
                    "files": records,
                    "excluded": skipped
                    if source_info is not None
                    else list(previous.get("excluded", [])),
                }
            )

        results.sort(key=lambda item: str(item["source"]).casefold())
        excluded.sort(key=lambda item: str(item["path"]).casefold())
        return results, excluded

    def _snapshot_legacy_world_archives(
        self,
        *,
        instance: ServerInstance,
        server_stage: Path,
        previous_manifest: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, int]]]:
        """Migrate old world-only ZIPs into the immutable fleet generation."""
        backup_dir = self._legacy_backup_dir(instance)
        if backup_dir.is_symlink():
            raise FleetBackupError(
                f"{instance.name}: legacy world backup directory is a symbolic link."
            )
        if backup_dir.exists() and not backup_dir.is_dir():
            raise FleetBackupError(
                f"{instance.name}: legacy world backup path is not a directory."
            )

        archive_root = server_stage / "legacy-world-archives"
        previous_archives = {
            str(item["filename"]): item
            for item in (
                previous_manifest.get("legacy_world_archives", [])
                if previous_manifest
                else []
            )
            if isinstance(item, dict) and isinstance(item.get("filename"), str)
        }
        prior_generation = (
            Path(previous_manifest["_generation_path"])
            if previous_manifest and previous_manifest.get("_generation_path")
            else None
        )
        previous_archive_root = (
            prior_generation
            / "servers"
            / _server_key(instance.id)
            / "legacy-world-archives"
            if prior_generation
            else None
        )

        sources: dict[str, Path] = {}
        excluded: list[dict[str, str]] = []
        initial_inventory: list[tuple[str, bool]] = []
        if backup_dir.is_dir():
            for candidate in sorted(
                backup_dir.iterdir(), key=lambda item: item.name.casefold()
            ):
                if candidate.is_symlink():
                    if candidate.suffix.casefold() == ".zip":
                        raise FleetBackupError(
                            f"{instance.name}: refusing a symbolic link in legacy world archives: {candidate.name}"
                        )
                    excluded.append(
                        {
                            "path": candidate.name,
                            "reason": "symbolic link not followed",
                        }
                    )
                    continue
                if candidate.is_file() and candidate.suffix.casefold() == ".zip":
                    sources[candidate.name] = candidate
                    metadata_path = candidate.with_name(candidate.name + ".json")
                    initial_inventory.append((candidate.name, metadata_path.is_file()))
                elif candidate.name.endswith(".zip.partial"):
                    excluded.append(
                        {
                            "path": candidate.name,
                            "reason": "incomplete temporary legacy archive",
                        }
                    )

        records: list[dict[str, Any]] = []
        archive_root.mkdir(parents=True, exist_ok=True)

        # Current external archives take precedence. A new archive is fully
        # validated once, then later generations can safely link its verified
        # immutable copy while the source remains unchanged.
        for filename, source in sources.items():
            old = previous_archives.get(filename)
            old_path = (
                previous_archive_root / filename
                if previous_archive_root is not None
                else None
            )
            current_sig = _stat_signature(source.stat(follow_symlinks=False))
            unchanged = (
                old is not None
                and old_path is not None
                and _same_signature(old.get("source_stat"), current_sig)
                and old_path.is_file()
                and not old_path.is_symlink()
                and old_path.stat().st_size == source.stat().st_size
                and not (old_path.stat().st_mode & 0o222)
            )
            if not unchanged:
                self._validate_legacy_world_archive(source)
            relative = f"legacy-world-archives/{filename}"
            file_record = self._snapshot_archive_file(
                source=source,
                destination=archive_root / filename,
                relative_path=relative,
                previous_entry=old,
                previous_path=old_path,
                instance_name=instance.name,
            )
            metadata = None
            metadata_path = source.with_name(source.name + ".json")
            if metadata_path.is_file() and not metadata_path.is_symlink():
                try:
                    raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                    label = raw.get("slot_name") if isinstance(raw, dict) else None
                    metadata = (
                        label.strip()
                        if isinstance(label, str) and label.strip()
                        else None
                    )
                except (OSError, ValueError):
                    metadata = None
            records.append(
                {
                    "filename": filename,
                    **file_record,
                    "slot_name": metadata,
                    "created_at_utc": datetime.fromtimestamp(
                        source.stat().st_mtime, UTC
                    )
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                }
            )

        # Carry forward archives already migrated into a previous complete
        # generation even after their old external source has been retired.
        for filename, old in previous_archives.items():
            if filename in sources:
                continue
            if previous_archive_root is None:
                raise FleetBackupError(
                    f"{instance.name}: previous manifest refers to legacy archive {filename} without a source generation."
                )
            old_path = previous_archive_root / filename
            relative = f"legacy-world-archives/{filename}"
            file_record = self._snapshot_archive_file(
                source=None,
                destination=archive_root / filename,
                relative_path=relative,
                previous_entry=old,
                previous_path=old_path,
                instance_name=instance.name,
            )
            records.append(
                {
                    "filename": filename,
                    **file_record,
                    "slot_name": old.get("slot_name"),
                    "created_at_utc": old.get("created_at_utc"),
                }
            )

        final_inventory: list[tuple[str, bool]] = []
        if backup_dir.is_dir():
            for candidate in sorted(
                backup_dir.iterdir(), key=lambda item: item.name.casefold()
            ):
                if (
                    candidate.is_file()
                    and not candidate.is_symlink()
                    and candidate.suffix.casefold() == ".zip"
                ):
                    metadata_path = candidate.with_name(candidate.name + ".json")
                    final_inventory.append((candidate.name, metadata_path.is_file()))
        if initial_inventory != final_inventory:
            raise FleetBackupError(
                f"{instance.name}: legacy world archive inventory changed during the fleet snapshot."
            )

        final_source_stats = []
        for filename, source in sources.items():
            try:
                final_source_stats.append(
                    _stat_signature(source.stat(follow_symlinks=False))
                )
            except OSError as exc:
                raise FleetBackupError(
                    f"{instance.name}: legacy archive disappeared during snapshot: {filename}: {exc}"
                ) from exc
            record = next(item for item in records if item["filename"] == filename)
            if not _same_signature(record.get("source_stat"), final_source_stats[-1]):
                raise FleetBackupError(
                    f"{instance.name}: legacy archive changed after it was copied: {filename}"
                )

        records.sort(key=lambda item: item["filename"].casefold())
        excluded.sort(key=lambda item: item["path"].casefold())
        return records, excluded, final_source_stats

    def _copy_server_tree(
        self,
        *,
        stage: Path,
        instance: ServerInstance,
        server_root: Path,
        capture_status: str,
        previous_manifest: dict[str, Any] | None,
    ) -> dict[str, Any]:
        instance_key = _server_key(instance.id)
        server_stage = stage / "servers" / instance_key
        files_root = server_stage / "files"
        files_root.mkdir(parents=True, exist_ok=True)

        previous_files = {
            str(item["path"]): item
            for item in (
                previous_manifest.get("files", []) if previous_manifest else []
            )
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        previous_files_root: Path | None = None
        if previous_manifest is not None:
            prior_path = previous_manifest.get("_generation_path")
            if prior_path:
                previous_files_root = (
                    Path(prior_path) / "servers" / instance_key / "files"
                )
            else:
                prior_id = previous_manifest.get("_generation_id")
                if prior_id:
                    previous_generation = self.generations_dir / str(prior_id)
                    previous_files_root = (
                        previous_generation / "servers" / instance_key / "files"
                    )

        relative_files, relative_dirs, excluded = _walk_server(server_root)
        directories: list[dict[str, Any]] = []
        for relative in relative_dirs:
            source_dir = server_root / relative
            info = source_dir.stat(follow_symlinks=False)
            target_dir = files_root / relative
            target_dir.mkdir(parents=True, exist_ok=True)
            directories.append(
                {
                    "path": relative.as_posix(),
                    "mode": stat.S_IMODE(info.st_mode),
                }
            )

        file_records: list[dict[str, Any]] = []
        for relative in relative_files:
            rel = relative.as_posix()
            source = server_root / relative
            destination = files_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)

            try:
                current_stat = source.stat(follow_symlinks=False)
            except OSError as exc:
                raise FleetBackupError(
                    f"{instance.name}: could not stat {rel}: {exc}"
                ) from exc
            current_signature = _stat_signature(current_stat)
            old_entry = previous_files.get(rel)
            old_path = previous_files_root / relative if previous_files_root else None

            record: dict[str, Any]
            reused = False
            digest: str | None = None
            size = int(current_stat.st_size)

            if (
                old_entry is not None
                and old_path is not None
                and _same_signature(old_entry.get("source_stat"), current_signature)
                and old_path.is_file()
                and not old_path.is_symlink()
                and old_path.stat().st_size == size
            ):
                digest = str(old_entry.get("sha256", ""))
                try:
                    if not digest or old_path.stat().st_mode & 0o222:
                        raise OSError("previous snapshot file is not immutable")
                    os.link(old_path, destination)
                    reused = True
                    self._bytes_reused += size
                except OSError:
                    copied = _copy_and_hash_snapshot(old_path, destination, digest)
                    self._bytes_copied += copied
                    reused = True
            else:
                new_hash, copied, source_signature, size = _copy_and_hash_live(
                    source, destination
                )
                self._bytes_copied += copied
                digest = new_hash
                # If bytes did not change even though source metadata did,
                # prefer a link to the old immutable copy to avoid duplicate
                # storage between generations.
                if (
                    old_entry is not None
                    and old_path is not None
                    and old_path.is_file()
                    and new_hash == old_entry.get("sha256")
                    and not old_path.is_symlink()
                ):
                    destination.unlink(missing_ok=True)
                    try:
                        os.link(old_path, destination)
                        reused = True
                        self._bytes_reused += size
                        self._bytes_copied -= copied
                    except OSError:
                        _copy_and_hash_snapshot(old_path, destination, new_hash)
                        reused = True
                current_signature = source_signature

            if digest is None:
                raise FleetBackupError(f"{instance.name}: could not hash {rel}.")
            # Verify fresh copies after writing. Reused inodes inherit the hash
            # from the previously verified immutable generation.
            if not reused and _hash_file(destination) != digest:
                raise FleetBackupError(
                    f"{instance.name}: post-write checksum mismatch for {rel}."
                )

            record = {
                "path": rel,
                "size": size,
                "sha256": digest,
                "mode": stat.S_IMODE(current_stat.st_mode),
                "source_stat": current_signature,
                "reused_from_previous": reused,
            }
            file_records.append(record)

        legacy_archives, legacy_archive_excluded, _legacy_archive_source_stats = (
            self._snapshot_legacy_world_archives(
                instance=instance,
                server_stage=server_stage,
                previous_manifest=previous_manifest,
            )
        )

        # Detect a server folder that changed structurally during the walk.
        # New/removed world or config files will be picked up next cycle, but
        # a snapshot should not claim to cover a mixed directory inventory.
        final_files, final_dirs, final_excluded = _walk_server(server_root)
        initial_names = [path.as_posix() for path in relative_files]
        final_names = [path.as_posix() for path in final_files]
        initial_dirs = [path.as_posix() for path in relative_dirs]
        final_dir_names = [path.as_posix() for path in final_dirs]
        initial_excluded = [(item["path"], item["reason"]) for item in excluded]
        final_excluded_names = [
            (item["path"], item["reason"]) for item in final_excluded
        ]
        if (
            initial_names != final_names
            or initial_dirs != final_dir_names
            or initial_excluded != final_excluded_names
        ):
            raise FleetBackupError(
                f"{instance.name}: server file/directory inventory changed while the backup was running."
            )

        # Also catch files modified after their own copy completed but before
        # the whole-server walk ended. This closes the gap left by per-file
        # before/after descriptor checks when a stopped folder is edited by
        # another process during a long snapshot.
        for record in file_records:
            source = server_root / _safe_relative(record["path"])
            try:
                final_signature = _stat_signature(source.stat(follow_symlinks=False))
            except OSError as exc:
                raise FleetBackupError(
                    f"{instance.name}: source file disappeared during final verification: "
                    f"{record['path']}: {exc}"
                ) from exc
            if not _same_signature(record.get("source_stat"), final_signature):
                raise FleetBackupError(
                    f"{instance.name}: source file changed after it was copied: {record['path']}"
                )

        active_world = _active_world_name(server_root)
        worlds = _world_inventory(server_root, active_world)

        # A server that has never generated a world is still restorable:
        # include its configs, loader, scripts and mods, and state explicitly
        # that no world save existed at this snapshot time.
        registry_metadata = (
            instance.to_dict()
            if callable(getattr(instance, "to_dict", None))
            else {
                "id": str(instance.id),
                "name": str(instance.name),
                "path": str(instance.path),
                "version": str(getattr(instance, "version", "")),
            }
        )
        return {
            "id": str(instance.id),
            "name": str(instance.name),
            "path": str(server_root),
            "registry": registry_metadata,
            "version": str(instance.version),
            "minecraft_version": str(instance.minecraft_version),
            "loader": str(instance.loader),
            "loader_version": str(instance.loader_version),
            "tmux_session": str(instance.tmux_session),
            "capture_status": "running (save-off/save-all/save-on)"
            if capture_status == "running"
            else "stopped",
            "active_world": active_world,
            "world_saves": worlds,
            "files": file_records,
            "legacy_world_archives": legacy_archives,
            "legacy_archive_excluded": legacy_archive_excluded,
            "directories": directories,
            "excluded": excluded,
            "excluded_roots": [item for item in excluded if "/" not in item["path"]],
        }

    # ------------------------------------------------------------------
    # Verification and restore
    # ------------------------------------------------------------------

    def _load_manifest(self, generation_path: Path) -> dict[str, Any] | None:
        manifest_path = generation_path / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        manifest_id = manifest.pop("manifest_sha256", None)
        actual = _sha256_bytes(_canonical_json(manifest))
        manifest["manifest_sha256"] = manifest_id
        if manifest_id != actual:
            return None
        if (
            manifest.get("schema") != GENERATION_SCHEMA
            or manifest.get("status") != "complete"
            or generation_path.name != manifest.get("generation_id")
        ):
            return None
        manifest["_generation_path"] = str(generation_path)
        return manifest

    def verify_generation(
        self, generation_path: Path, *, deep: bool = False
    ) -> dict[str, Any]:
        generation_path = Path(generation_path)
        if generation_path.is_symlink() or not generation_path.is_dir():
            return {
                "ok": False,
                "errors": ["generation directory is missing or is a symbolic link"],
            }
        manifest = self._load_manifest(generation_path)
        if manifest is None:
            return {
                "ok": False,
                "errors": [
                    "missing, malformed, or checksum-invalid generation manifest"
                ],
            }
        errors: list[str] = []
        try:
            if generation_path.stat(follow_symlinks=False).st_mode & 0o222:
                errors.append("generation root is writable; snapshot is not immutable")
            manifest_info = (generation_path / "manifest.json").stat(
                follow_symlinks=False
            )
            if manifest_info.st_mode & 0o222:
                errors.append(
                    "generation manifest is writable; snapshot is not immutable"
                )
        except OSError as exc:
            errors.append(f"could not inspect generation permissions: {exc}")

        servers = manifest.get("instances", [])
        if not isinstance(servers, list) or len(servers) != int(
            manifest.get("servers", -1)
        ):
            errors.append("manifest server count does not match the server inventory")
            servers = []
        seen_ids: set[str] = set()
        file_count = 0
        logical_bytes = 0
        for server in servers:
            if not isinstance(server, dict):
                errors.append("malformed server entry in generation manifest")
                continue
            server_id = str(server.get("id", ""))
            if not server_id or server_id in seen_ids:
                errors.append(
                    "missing or duplicate server identity in generation manifest"
                )
                continue
            seen_ids.add(server_id)
            servers_root = generation_path / "servers"
            server_root = servers_root / _server_key(server_id)
            files_root = server_root / "files"
            roots_safe = True
            for label, directory in (
                ("servers", servers_root),
                ("server", server_root),
                ("files", files_root),
            ):
                try:
                    if directory.is_symlink() or not directory.is_dir():
                        errors.append(
                            f"{server.get('name', server_id)}: missing or unsafe {label} snapshot directory"
                        )
                        roots_safe = False
                    elif directory.stat(follow_symlinks=False).st_mode & 0o222:
                        errors.append(
                            f"{server.get('name', server_id)}: {label} snapshot directory is writable"
                        )
                        roots_safe = False
                except OSError as exc:
                    errors.append(
                        f"{server.get('name', server_id)}: could not inspect {label} snapshot directory: {exc}"
                    )
                    roots_safe = False
            if not roots_safe:
                continue
            try:
                for directory_entry in server.get("directories", []):
                    relative_dir = _safe_relative(str(directory_entry["path"]))
                    target_dir = files_root.joinpath(*relative_dir.parts)
                    if target_dir.is_symlink() or not target_dir.is_dir():
                        errors.append(
                            f"{server.get('name', server_id)}: missing or unsafe directory {relative_dir}"
                        )
                        continue
                    if target_dir.stat(follow_symlinks=False).st_mode & 0o222:
                        errors.append(
                            f"{server.get('name', server_id)}: snapshot directory is writable: {relative_dir}"
                        )
            except (OSError, KeyError, ValueError, TypeError, FleetBackupError) as exc:
                errors.append(f"{server_id}: invalid directory entry ({exc})")
            for entry in server.get("files", []):
                file_count += 1
                try:
                    relative = _safe_relative(str(entry["path"]))
                    target = files_root.joinpath(*relative.parts)
                    if target.is_symlink() or not target.is_file():
                        errors.append(
                            f"{server.get('name', server_id)}: missing or unsafe {relative}"
                        )
                        continue
                    info = target.stat(follow_symlinks=False)
                    if info.st_size != int(entry["size"]):
                        errors.append(
                            f"{server.get('name', server_id)}: wrong size for {relative}"
                        )
                        continue
                    if info.st_mode & 0o222:
                        errors.append(
                            f"{server.get('name', server_id)}: snapshot file is writable: {relative}"
                        )
                        continue
                    logical_bytes += info.st_size
                    # No file or directory component inside a generation may
                    # be a symlink, even when the final file itself is regular.
                    parent = files_root
                    for component in relative.parts[:-1]:
                        parent = parent / component
                        if parent.is_symlink() or not parent.is_dir():
                            raise FleetBackupError(
                                f"unsafe directory component while checking {relative}"
                            )
                    if deep and _hash_file(target) != entry["sha256"]:
                        errors.append(
                            f"{server.get('name', server_id)}: checksum mismatch for {relative}"
                        )
                except (
                    OSError,
                    KeyError,
                    ValueError,
                    TypeError,
                    FleetBackupError,
                ) as exc:
                    errors.append(f"{server_id}: invalid file entry ({exc})")

            archive_entries = server.get("legacy_world_archives", [])
            if archive_entries:
                archive_root = server_root / "legacy-world-archives"
                try:
                    if archive_root.is_symlink() or not archive_root.is_dir():
                        errors.append(
                            f"{server.get('name', server_id)}: legacy world archive directory is missing or unsafe"
                        )
                    elif archive_root.stat(follow_symlinks=False).st_mode & 0o222:
                        errors.append(
                            f"{server.get('name', server_id)}: legacy archive directory is writable"
                        )
                except OSError as exc:
                    errors.append(
                        f"{server.get('name', server_id)}: could not inspect legacy archive directory: {exc}"
                    )

            for archive_entry in archive_entries:
                file_count += 1
                try:
                    relative = _safe_relative(str(archive_entry["path"]))
                    if (
                        len(relative.parts) != 2
                        or relative.parts[0] != "legacy-world-archives"
                        or relative.parts[1] != str(archive_entry["filename"])
                    ):
                        raise FleetBackupError(
                            "legacy archive path does not match its filename"
                        )
                    target = server_root.joinpath(*relative.parts)
                    if target.is_symlink() or not target.is_file():
                        errors.append(
                            f"{server.get('name', server_id)}: missing or unsafe legacy archive {relative}"
                        )
                        continue
                    info = target.stat(follow_symlinks=False)
                    if info.st_size != int(archive_entry["size"]):
                        errors.append(
                            f"{server.get('name', server_id)}: wrong size for legacy archive {relative}"
                        )
                        continue
                    if info.st_mode & 0o222:
                        errors.append(
                            f"{server.get('name', server_id)}: legacy archive is writable: {relative}"
                        )
                        continue
                    logical_bytes += info.st_size
                    if deep and _hash_file(target) != archive_entry["sha256"]:
                        errors.append(
                            f"{server.get('name', server_id)}: checksum mismatch for legacy archive {relative}"
                        )
                except (
                    OSError,
                    KeyError,
                    ValueError,
                    TypeError,
                    FleetBackupError,
                ) as exc:
                    errors.append(f"{server_id}: invalid legacy archive entry ({exc})")

        legacy_sources = manifest.get("legacy_sources", [])
        if not isinstance(legacy_sources, list):
            errors.append("manifest legacy-source inventory is malformed")
            legacy_sources = []
        seen_legacy_sources: set[str] = set()
        for source_record in legacy_sources:
            if not isinstance(source_record, dict):
                errors.append("malformed legacy-source entry in generation manifest")
                continue
            source_name = str(source_record.get("source", ""))
            try:
                source_relative = _safe_relative(source_name)
                if source_name in seen_legacy_sources:
                    raise FleetBackupError("duplicate legacy-source path")
                seen_legacy_sources.add(source_name)
                source_type = str(source_record.get("source_type", ""))
                if source_type not in {"file", "directory"}:
                    raise FleetBackupError("unsupported legacy-source type")
            except (ValueError, FleetBackupError) as exc:
                errors.append(f"invalid legacy-source record ({exc})")
                continue

            source_files = source_record.get("files", [])
            if not isinstance(source_files, list):
                errors.append(
                    f"{source_name}: legacy-source file inventory is malformed"
                )
                continue
            source_file_names: set[str] = set()
            for entry in source_files:
                file_count += 1
                try:
                    snapshot_relative = _safe_relative(str(entry["path"]))
                    source_file = _safe_relative(str(entry["source_path"]))
                    expected_prefix = PurePosixPath("legacy-sources") / source_relative
                    if source_type == "file":
                        if (
                            snapshot_relative != expected_prefix
                            or source_file != source_relative
                        ):
                            raise FleetBackupError(
                                "single-file legacy source path mismatch"
                            )
                    else:
                        if (
                            snapshot_relative.parts[: len(expected_prefix.parts)]
                            != expected_prefix.parts
                            or source_file.parts[: len(source_relative.parts)]
                            != source_relative.parts
                        ):
                            raise FleetBackupError(
                                "directory legacy source path escapes its root"
                            )
                    target = generation_path.joinpath(*snapshot_relative.parts)
                    if target.is_symlink() or not target.is_file():
                        errors.append(
                            f"legacy source {source_name}: missing or unsafe file {snapshot_relative}"
                        )
                        continue
                    info = target.stat(follow_symlinks=False)
                    if info.st_size != int(entry["size"]):
                        errors.append(
                            f"legacy source {source_name}: wrong size for {snapshot_relative}"
                        )
                        continue
                    if info.st_mode & 0o222:
                        errors.append(
                            f"legacy source {source_name}: snapshot file is writable: {snapshot_relative}"
                        )
                        continue
                    logical_bytes += info.st_size
                    if source_file.as_posix() in source_file_names:
                        raise FleetBackupError("duplicate source-relative file path")
                    source_file_names.add(source_file.as_posix())
                    parent = generation_path
                    for component in snapshot_relative.parts[:-1]:
                        parent = parent / component
                        if parent.is_symlink() or not parent.is_dir():
                            raise FleetBackupError(
                                f"unsafe directory component while checking {snapshot_relative}"
                            )
                    if deep and _hash_file(target) != entry["sha256"]:
                        errors.append(
                            f"legacy source {source_name}: checksum mismatch for {snapshot_relative}"
                        )
                except (
                    OSError,
                    KeyError,
                    ValueError,
                    TypeError,
                    FleetBackupError,
                ) as exc:
                    errors.append(
                        f"{source_name}: invalid legacy-source file entry ({exc})"
                    )

            if len(source_file_names) != int(source_record.get("file_count", -1)):
                errors.append(f"{source_name}: legacy-source file count mismatch")
            if sum(
                int(item.get("size", 0))
                for item in source_files
                if isinstance(item, dict)
            ) != int(source_record.get("bytes", -1)):
                errors.append(f"{source_name}: legacy-source logical size mismatch")

        if file_count != int(manifest.get("file_count", -1)):
            errors.append("manifest file count does not match the file inventory")
        if logical_bytes != int(manifest.get("logical_bytes", -1)):
            errors.append(
                "manifest logical byte count does not match the file inventory"
            )
        return {
            "ok": not errors,
            "errors": errors,
            "manifest": manifest,
            "file_count": file_count,
            "logical_bytes": logical_bytes,
        }

    def restore_server(
        self,
        generation_id: str,
        instance_id: str,
        target_directory: Path,
        *,
        deep_verify: bool = True,
    ) -> Path:
        """Restore one server snapshot to a new, separate directory; never overwrites."""
        source_generation = self.generations_dir / generation_id
        verification = self.verify_generation(source_generation, deep=deep_verify)
        if not verification["ok"]:
            raise FleetBackupError(
                "Refusing to restore an invalid generation: "
                + "; ".join(verification["errors"][:8])
            )

        manifest = verification["manifest"]
        server = next(
            (
                row
                for row in manifest["instances"]
                if str(row["id"]) == str(instance_id)
            ),
            None,
        )
        if server is None:
            raise FleetBackupError(
                f"Generation {generation_id} does not contain server {instance_id}."
            )

        target = Path(target_directory).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = Path(os.path.abspath(target))
        if target.exists() or target.is_symlink():
            raise FleetBackupError(f"Restore target already exists: {target}")

        # A restore is a recovery drill into a separate, new directory. Never
        # let a caller accidentally place that output inside (or over) any
        # registered live server folder, including an instance whose path is
        # currently missing on disk.
        for instance in self.instances:
            live_server = Path(instance.path).expanduser().resolve()
            if (
                target == live_server
                or live_server in target.parents
                or target in live_server.parents
            ):
                raise FleetBackupError(
                    "Refusing to restore a fleet snapshot into or over a "
                    f"registered server folder: {live_server}"
                )

        parent_chain = list(reversed(target.parent.parents)) + [target.parent]
        for directory in parent_chain:
            if directory.is_symlink():
                raise FleetBackupError(
                    f"Refusing a symbolic link in the restore target path: {directory}"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        server_root = (
            source_generation / "servers" / _server_key(str(instance_id)) / "files"
        )
        stage = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
        )
        try:
            for directory in server.get("directories", []):
                relative = _safe_relative(str(directory["path"]))
                destination = stage.joinpath(*relative.parts)
                destination.mkdir(parents=True, exist_ok=True)
            for entry in server.get("files", []):
                relative = _safe_relative(str(entry["path"]))
                source = server_root.joinpath(*relative.parts)
                destination = stage.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination, follow_symlinks=False)
                os.chmod(destination, int(entry["mode"]))
                if deep_verify and _hash_file(destination) != entry["sha256"]:
                    raise FleetBackupError(
                        f"Restore verification failed for {relative}."
                    )

            # Older named world-slot ZIPs are also preserved in each fleet
            # generation. Keep them in an isolated hidden recovery folder in
            # the restored server root; they are not copied into the live
            # Minecraft world's chunk tree or the pack's mods/config folders.
            archive_entries = server.get("legacy_world_archives", [])
            if archive_entries:
                archive_restore_root = stage / ".crucible-legacy-world-archives"
                archive_restore_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                archive_restore_root.chmod(0o700)
                archive_index = []
                archive_root = (
                    source_generation
                    / "servers"
                    / _server_key(str(instance_id))
                    / "legacy-world-archives"
                )
                for archive_entry in archive_entries:
                    filename = str(archive_entry["filename"])
                    if Path(filename).name != filename:
                        raise FleetBackupError(
                            f"Unsafe legacy archive filename in manifest: {filename!r}"
                        )
                    source = archive_root / filename
                    destination = archive_restore_root / filename
                    if source.is_symlink() or not source.is_file():
                        raise FleetBackupError(
                            f"Legacy world archive is missing or unsafe: {filename}"
                        )
                    shutil.copyfile(source, destination, follow_symlinks=False)
                    destination.chmod(0o600)
                    if (
                        deep_verify
                        and _hash_file(destination) != archive_entry["sha256"]
                    ):
                        raise FleetBackupError(
                            f"Legacy world archive restore checksum failed: {filename}"
                        )
                    archive_index.append(
                        {
                            "filename": filename,
                            "slot_name": archive_entry.get("slot_name"),
                            "created_at_utc": archive_entry.get("created_at_utc"),
                            "sha256": archive_entry["sha256"],
                            "size": archive_entry["size"],
                        }
                    )
                index_path = archive_restore_root / "index.json"
                index_path.write_text(
                    json.dumps(archive_index, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                index_path.chmod(0o600)

            for directory in sorted(
                server.get("directories", []),
                key=lambda item: len(PurePosixPath(item["path"]).parts),
                reverse=True,
            ):
                relative = _safe_relative(str(directory["path"]))
                directory_path = stage.joinpath(*relative.parts)
                if directory_path.is_dir():
                    os.chmod(directory_path, int(directory["mode"]))
            os.replace(stage, target)
            return target
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def restore_legacy_source(
        self,
        generation_id: str,
        source_name: str,
        target_path: Path,
        *,
        deep_verify: bool = True,
    ) -> Path:
        """Restore opaque legacy backup material to a new path without overwriting.

        Retired-ID archives, manual snapshots and unregistered server rollback
        trees remain deliberately unmapped. This method restores their exact
        captured file tree to a caller-chosen new location; it never puts data
        into a live registered server directory automatically.
        """
        source_generation = self.generations_dir / generation_id
        verification = self.verify_generation(source_generation, deep=deep_verify)
        if not verification["ok"]:
            raise FleetBackupError(
                "Refusing to restore an invalid generation: "
                + "; ".join(verification["errors"][:8])
            )

        source_record = next(
            (
                item
                for item in verification["manifest"].get("legacy_sources", [])
                if isinstance(item, dict) and item.get("source") == source_name
            ),
            None,
        )
        if source_record is None:
            raise FleetBackupError(
                f"Generation {generation_id} does not contain legacy source {source_name!r}."
            )

        source_relative = _safe_relative(source_name)
        source_type = str(source_record.get("source_type", ""))
        if source_type not in {"file", "directory"}:
            raise FleetBackupError(
                f"Unsupported legacy source type for {source_name!r}."
            )

        target = Path(target_path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = Path(os.path.abspath(target))
        if not target.name:
            raise FleetBackupError(
                "Legacy restore target must name a new file or directory."
            )
        if target.exists() or target.is_symlink():
            raise FleetBackupError(f"Legacy restore target already exists: {target}")
        for instance in self.instances:
            live_server = Path(instance.path).expanduser().resolve()
            if (
                target == live_server
                or live_server in target.parents
                or target in live_server.parents
            ):
                raise FleetBackupError(
                    "Refusing to restore opaque historical data into or over a "
                    f"registered server folder: {live_server}"
                )
        target_parent = target.parent
        current = Path(target_parent.anchor or "/")
        for component in target_parent.parts[1:]:
            current = current / component
            if current.is_symlink():
                raise FleetBackupError(
                    f"Refusing a symbolic link in the legacy restore target path: {current}"
                )
        target_parent.mkdir(parents=True, exist_ok=True)

        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{target.name}.legacy-restore-", dir=target_parent
            )
        )
        try:
            entries = source_record.get("files", [])
            if not isinstance(entries, list):
                raise FleetBackupError(
                    f"Legacy source {source_name!r} has a malformed file inventory."
                )
            for entry in entries:
                source_rel = _safe_relative(str(entry["source_path"]))
                if (
                    source_rel.parts[: len(source_relative.parts)]
                    != source_relative.parts
                ):
                    raise FleetBackupError(
                        f"Legacy source file escapes its root: {source_rel}"
                    )
                if source_type == "file":
                    if source_rel != source_relative:
                        raise FleetBackupError(
                            f"Single-file legacy source has an unexpected member: {source_rel}"
                        )
                    relative_inside = PurePosixPath(target.name)
                else:
                    relative_inside = PurePosixPath(
                        *source_rel.parts[len(source_relative.parts) :]
                    )
                    if not relative_inside.parts:
                        raise FleetBackupError(
                            f"Directory legacy source has an empty member path: {source_rel}"
                        )

                snapshot_rel = _safe_relative(str(entry["path"]))
                snapshot_file = source_generation.joinpath(*snapshot_rel.parts)
                if snapshot_file.is_symlink() or not snapshot_file.is_file():
                    raise FleetBackupError(
                        f"Legacy generation file is missing or unsafe: {snapshot_rel}"
                    )
                destination = stage.joinpath(*relative_inside.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(snapshot_file, destination, follow_symlinks=False)
                os.chmod(destination, int(entry["mode"]))
                if deep_verify and _hash_file(destination) != entry["sha256"]:
                    raise FleetBackupError(
                        f"Legacy restore verification failed for {source_rel}."
                    )

            if source_type == "file":
                staged_file = stage / target.name
                os.replace(staged_file, target)
                shutil.rmtree(stage, ignore_errors=True)
            else:
                os.replace(stage, target)
            return target
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    # ------------------------------------------------------------------
    # Durable metadata and utilities
    # ------------------------------------------------------------------

    def _write_state(self, payload: dict[str, Any]) -> None:
        try:
            self._write_json_atomic(self.state_path, payload)
        except OSError as exc:
            # State metadata is helpful but does not invalidate a generation
            # that has already been committed and verified.
            print(f"[crucible] could not update fleet backup state: {exc}")

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, path)
            FleetBackupManager._fsync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _restrict_directory(path: Path) -> None:
        try:
            path.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _fsync_directory_strict(path: Path) -> None:
        """Flush a backup directory or fail before any prior generation is pruned."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise FleetBackupError(
                f"Could not durably flush fleet backup directory {path}: {exc}"
            ) from exc

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        # Best-effort metadata flush for the diagnostic state file. Snapshot
        # publication uses _fsync_directory_strict and never prunes on failure.
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
