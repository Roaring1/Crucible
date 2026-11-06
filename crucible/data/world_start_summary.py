"""Read-only inspection of the world selected for the next server start.

The scan is intentionally filesystem-only. It never attaches to tmux, starts a
server, reads world contents, or follows symlinks. Call it from a worker thread:
large modded worlds can contain many thousands of region files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorldStartSummary:
    """A point-in-time summary of the configured world and alternate saves."""

    world_name: str
    world_path: Path
    state: str
    size_bytes: int
    newest_file_mtime: float | None
    region_files: int
    player_files: int
    other_developed_worlds: tuple[str, ...] = ()
    previous_world_name: str | None = None
    error: str = ""

    @property
    def unexpected_empty(self) -> bool:
        return self.state in {"new", "incomplete"} and bool(
            self.other_developed_worlds
        )

    @property
    def selection_changed(self) -> bool:
        return bool(
            self.previous_world_name
            and self.previous_world_name != self.world_name
        )

    @property
    def requires_confirmation(self) -> bool:
        # Older registry entries have no previous-world marker. When multiple
        # saves exist, ask once rather than guessing which one the owner meant.
        return (
            self.state in {"new", "incomplete"}
            or self.selection_changed
            or (
                self.previous_world_name is None
                and bool(self.other_developed_worlds)
            )
        )


def _has_regular_files(directory: Path, *, suffix: str | None = None) -> bool:
    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    if suffix is None or entry.name.lower().endswith(suffix):
                        return True
    except OSError:
        return False
    return False


def _is_developed_world(path: Path) -> bool:
    """Recognize alternate vanilla/Forge saves without following symlinks."""
    if path.is_symlink() or not path.is_dir():
        return False
    level_dat = path / "level.dat"
    if level_dat.is_symlink() or not level_dat.is_file():
        return False
    if _has_regular_files(path / "region", suffix=".mca"):
        return True
    if _has_regular_files(path / "playerdata", suffix=".dat"):
        return True

    # Forge/GTNH keeps additional dimensions below DIM*/ or
    # dimensions/<namespace>/<dimension>/. Limit recursive discovery to those
    # known world subtrees rather than walking data/, stats/, datapacks/, etc.
    try:
        with os.scandir(path) as entries:
            dimension_roots = [
                Path(entry.path)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
                and (
                    entry.name.startswith("DIM")
                    or entry.name == "dimensions"
                )
            ]
    except OSError:
        return False

    for dimension_root in dimension_roots:
        for current, directories, files in os.walk(
            dimension_root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            directories[:] = [
                name for name in directories
                if not (current_path / name).is_symlink()
            ]
            if current_path.name == "region" and any(
                name.lower().endswith(".mca")
                and not (current_path / name).is_symlink()
                and (current_path / name).is_file()
                for name in files
            ):
                return True
            if current_path.name == "playerdata" and any(
                name.lower().endswith(".dat")
                and not (current_path / name).is_symlink()
                and (current_path / name).is_file()
                for name in files
            ):
                return True
    return False


def inspect_world_start_state(
    instance_path: str | Path,
    world_name: str,
    previous_world_name: str | None = None,
) -> WorldStartSummary:
    """Inspect the selected save and look for other developed worlds.

    A world name must be one direct child of the instance directory. This keeps
    a malformed level-name from making the pre-start inspection walk outside
    the managed server. Symlinks are never followed.
    """
    instance_root = Path(instance_path).expanduser()
    selected_name = str(world_name or "").strip()
    safe_name = (
        bool(selected_name)
        and selected_name not in {".", ".."}
        and "/" not in selected_name
        and "\\" not in selected_name
        and not Path(selected_name).is_absolute()
    )
    if not safe_name:
        return WorldStartSummary(
            world_name=selected_name or "(empty)",
            world_path=instance_root / selected_name,
            state="unsafe",
            size_bytes=0,
            newest_file_mtime=None,
            region_files=0,
            player_files=0,
            previous_world_name=previous_world_name,
            error="The configured level-name is not a safe single world-folder name.",
        )

    try:
        if instance_root.is_symlink() or not instance_root.is_dir():
            raise OSError("The managed server folder is missing or is a symbolic link.")
        root = instance_root.resolve(strict=True)
        world_root = root / selected_name
        if world_root.is_symlink():
            raise OSError("The selected world folder is a symbolic link.")
        if world_root.exists() and not world_root.is_dir():
            raise OSError("The selected world path exists but is not a directory.")

        other_worlds: list[str] = []
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name == selected_name or not entry.is_dir(follow_symlinks=False):
                    continue
                candidate = Path(entry.path)
                if _is_developed_world(candidate):
                    other_worlds.append(entry.name)
        other_worlds.sort(key=str.casefold)

        if not world_root.is_dir():
            return WorldStartSummary(
                world_name=selected_name,
                world_path=world_root,
                state="new",
                size_bytes=0,
                newest_file_mtime=None,
                region_files=0,
                player_files=0,
                other_developed_worlds=tuple(other_worlds),
                previous_world_name=previous_world_name,
            )

        has_level_dat = (world_root / "level.dat").is_file() and not (
            world_root / "level.dat"
        ).is_symlink()
        total_size = 0
        newest_mtime: float | None = None
        region_files = 0
        player_files = 0
        scan_errors: list[str] = []

        def on_walk_error(error: OSError) -> None:
            scan_errors.append(str(error))

        for current, directories, files in os.walk(
            world_root, topdown=True, followlinks=False, onerror=on_walk_error
        ):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if not (current_path / name).is_symlink()
            ]
            for filename in files:
                file_path = current_path / filename
                if file_path.is_symlink():
                    continue
                try:
                    stat = file_path.stat(follow_symlinks=False)
                except OSError as exc:
                    scan_errors.append(f"{file_path}: {exc}")
                    continue
                if not file_path.is_file():
                    continue
                total_size += stat.st_size
                # session.lock changes on startup and is not a world-save time.
                if filename != "session.lock":
                    newest_mtime = (
                        stat.st_mtime
                        if newest_mtime is None
                        else max(newest_mtime, stat.st_mtime)
                    )
                if current_path.name == "region" and filename.lower().endswith(".mca"):
                    region_files += 1
                if current_path.name == "playerdata" and filename.lower().endswith(".dat"):
                    player_files += 1

        if scan_errors:
            raise OSError(
                "Some selected-world files could not be inspected: "
                + "; ".join(scan_errors[:3])
            )

        if has_level_dat and (region_files or player_files):
            state = "existing"
        elif has_level_dat or total_size:
            state = "incomplete"
        else:
            state = "new"

        return WorldStartSummary(
            world_name=selected_name,
            world_path=world_root,
            state=state,
            size_bytes=total_size,
            newest_file_mtime=newest_mtime,
            region_files=region_files,
            player_files=player_files,
            other_developed_worlds=tuple(other_worlds),
            previous_world_name=previous_world_name,
        )
    except OSError as exc:
        return WorldStartSummary(
            world_name=selected_name,
            world_path=instance_root / selected_name,
            state="unavailable",
            size_bytes=0,
            newest_file_mtime=None,
            region_files=0,
            player_files=0,
            previous_world_name=previous_world_name,
            error=str(exc),
        )
