"""Safe XDG desktop shortcuts for one registered Crucible server.

A shortcut is a small desktop-entry file which invokes Crucible's own
shortcut-action CLI command with the stable instance ID. It never embeds
a display name in the command and never uses a shell.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__

APPLICATIONS_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    / "applications"
)


class ShortcutError(RuntimeError):
    """A shortcut could not be safely created or removed."""


@dataclass(frozen=True)
class ShortcutResult:
    created: tuple[Path, ...] = ()
    removed: tuple[Path, ...] = ()


def shortcut_key(instance_id: str) -> str:
    return hashlib.sha256(str(instance_id).encode("utf-8")).hexdigest()[:16]


def shortcut_filename(instance_id: str, action: str) -> str:
    if action not in {"start", "stop"}:
        raise ValueError("shortcut action must be start or stop")
    return f"crucible-{shortcut_key(instance_id)}-{action}.desktop"


def _desktop_quote(value: str | Path) -> str:
    """Quote one Desktop Entry Exec argument; this is not shell quoting."""
    text = str(value)
    if not text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ShortcutError("A desktop shortcut argument contains a control character")
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"').replace(chr(96), "\\" + chr(96)).replace("$", "\\$")
    escaped = escaped.replace("%", "%%")
    return f'"{escaped}"'


def _desktop_string(value: str | Path) -> str:
    text = str(value)
    if not text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ShortcutError("A desktop shortcut value contains a control character")
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _prepare_directory(directory: str | Path) -> Path:
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise ShortcutError("Shortcut destination must be an absolute directory")
    if path.exists() and path.is_symlink():
        raise ShortcutError(f"Refusing a symbolic-link shortcut destination: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o755)
    except OSError as exc:
        raise ShortcutError(f"Could not create shortcut destination {path}: {exc}") from exc
    if path.is_symlink() or not path.is_dir():
        raise ShortcutError(f"Shortcut destination is not a real directory: {path}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise ShortcutError(f"Shortcut destination is not writable: {path}")
    return path.resolve()


def _module_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _icon_path(action: str) -> Path:
    name = "play.svg" if action == "start" else "stop.svg"
    return Path(__file__).resolve().parent / "ui" / "assets" / "menu-icons" / name


def _entry_contents(instance, action: str) -> str:
    if action not in {"start", "stop"}:
        raise ValueError("shortcut action must be start or stop")
    action_label = "Start" if action == "start" else "Stop"
    verb = "gracefully stop" if action == "stop" else "start"
    # Preserve the venv entrypoint path; resolving it can bypass pyvenv.cfg.
    python = Path(sys.executable)
    package_root = _module_root()
    icon = _icon_path(action)
    display_name = _desktop_string(
        "Crucible: " + action_label + " " + str(instance.name)
    )
    env_tool = shutil.which("env") or "/usr/bin/env"
    exec_line = " ".join(
        _desktop_quote(arg)
        for arg in (
            env_tool,
            f"PYTHONPATH={package_root}",
            python,
            "-m",
            "crucible",
            "shortcut-action",
            action,
            str(instance.id),
        )
    )
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        f"Name={display_name}\n"
        f"Comment=Use Crucible to {verb} this registered Minecraft server.\n"
        f"Exec={exec_line}\n"
        f"Path={_desktop_string(Path.home())}\n"
        f"Icon={_desktop_string(icon)}\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "Categories=Game;\n"
        f"X-Crucible-Version={__version__}\n"
        f"X-Crucible-Instance={_desktop_string(instance.id)}\n"
        f"X-Crucible-Action={action}\n"
    )


def _atomic_write_desktop(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ShortcutError(f"Refusing to replace a symbolic link: {path}")
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise ShortcutError(f"Refusing to replace a non-regular file: {path}")
    fd = -1
    temporary = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".crucible-shortcut-", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o755)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as exc:
        raise ShortcutError(f"Could not write desktop shortcut {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def create_shortcuts(
    instance,
    *,
    destination: str,
    desktop_directory: str | Path,
    include_start: bool = True,
    include_stop: bool = True,
) -> ShortcutResult:
    """Create/update selected Start/Stop shortcuts at chosen XDG locations."""
    if destination not in {"desktop", "applications", "both"}:
        raise ValueError("destination must be desktop, applications, or both")
    actions = [
        action
        for action, enabled in (("start", include_start), ("stop", include_stop))
        if enabled
    ]
    if not actions:
        raise ShortcutError("Choose at least one shortcut action")

    directories: list[Path] = []
    if destination in {"desktop", "both"}:
        directories.append(_prepare_directory(desktop_directory))
    if destination in {"applications", "both"}:
        directories.append(_prepare_directory(APPLICATIONS_DIR))

    created: list[Path] = []
    for directory in directories:
        for action in actions:
            path = directory / shortcut_filename(instance.id, action)
            _atomic_write_desktop(path, _entry_contents(instance, action))
            created.append(path)
    return ShortcutResult(created=tuple(created))


def remove_shortcuts(
    instance_id: str,
    *,
    desktop_directories: tuple[str | Path, ...] = (),
    include_applications: bool = True,
) -> ShortcutResult:
    """Remove only this app's exact generated files for the given stable ID."""
    directories = [
        Path(item).expanduser()
        for item in desktop_directories
        if str(item).strip()
    ]
    if include_applications:
        directories.append(APPLICATIONS_DIR)
    removed: list[Path] = []
    for raw_directory in directories:
        if raw_directory.exists() and raw_directory.is_symlink():
            raise ShortcutError(f"Refusing a symbolic-link shortcut directory: {raw_directory}")
        if not raw_directory.is_dir():
            continue
        directory = raw_directory.resolve()
        for action in ("start", "stop"):
            path = directory / shortcut_filename(instance_id, action)
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink():
                raise ShortcutError(f"Refusing to remove a symbolic-link or non-regular shortcut: {path}")
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ShortcutError(f"Refusing to remove a symbolic-link or non-regular shortcut: {path}")
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                raise ShortcutError(f"Could not remove shortcut {path}: {exc}") from exc
    return ShortcutResult(removed=tuple(removed))
