"""Shared validation for Minecraft's configured world directory.

This module is deliberately filesystem-only so all destructive world workflows
can reject unsafe paths before they back up, rename, or remove anything.
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


class UnsafeWorldPathError(ValueError):
    """The configured world path cannot be used safely."""


def safe_world_component(value: str) -> bool:
    """Return whether a level-name is a single portable directory component."""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or value in {"", ".", ".."}
    ):
        return False
    if any(char in value for char in ("/", "\\", ":", "\x00")):
        return False
    native = Path(value)
    windows = PureWindowsPath(value)
    return (
        not native.is_absolute()
        and len(native.parts) == 1
        and not windows.is_absolute()
        and not windows.drive
        and len(windows.parts) == 1
    )


def _logical_property_lines(text: str) -> list[str]:
    """Join Java-properties continuation lines and remove their escape slash."""
    logical: list[str] = []
    pending: str | None = None
    for physical in text.splitlines():
        if pending is None:
            pending = physical.lstrip()
        else:
            pending += physical.lstrip()
        trailing_slashes = len(pending) - len(pending.rstrip("\\"))
        if trailing_slashes % 2:
            pending = pending[:-1]
            continue
        logical.append(pending)
        pending = None
    if pending is not None:
        logical.append(pending)
    return logical


def _java_property_key_value(line: str) -> tuple[str, str] | None:
    """Parse a Java-properties key and raw value, including escaped key chars."""
    text = line.lstrip()
    if not text or text.startswith(("#", "!")):
        return None

    key: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 1
            if i >= len(text):
                break
            escaped = text[i]
            if escaped == "u":
                digits = text[i + 1:i + 5]
                if len(digits) != 4 or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
                    raise ValueError("malformed Unicode escape in server.properties key")
                key.append(chr(int(digits, 16)))
                i += 5
                continue
            key.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(escaped, escaped))
            i += 1
            continue
        if char in " \t\f=:":
            break
        key.append(char)
        i += 1

    while i < len(text) and text[i] in " \t\f":
        i += 1
    if i < len(text) and text[i] in "=: ":
        separator = text[i]
        i += 1
        if separator != " ":
            while i < len(text) and text[i] in " \t\f":
                i += 1
    return "".join(key), text[i:]


def configured_level_name(root: str | Path) -> str | None:
    """Return Java's effective level-name; None means an unsafe explicit value."""
    server_root = Path(root).expanduser()
    props = server_root / "server.properties"
    try:
        lines = _logical_property_lines(props.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return "world"
    except OSError:
        return None

    found = False
    candidate: str | None = None
    try:
        for line in lines:
            parsed = _java_property_key_value(line)
            if parsed is not None and parsed[0] == "level-name":
                found = True
                candidate = parsed[1]
    except ValueError:
        return None

    if not found:
        return "world"
    if not candidate or not safe_world_component(candidate):
        return None
    return candidate


def world_name_issue(root: str | Path) -> str | None:
    """Explain why a configured world root/dimension cannot be manipulated."""
    server_root = Path(root).expanduser()
    if server_root.is_symlink():
        return "The registered server folder is a symbolic link; Crucible will not modify a world through it."
    level = configured_level_name(server_root)
    if level is None:
        return (
            "server.properties has an unsafe level-name path. Use a single folder "
            "name without path separators, a drive, '.' or '..' before modifying the world."
        )
    root_resolved = server_root.resolve(strict=False)
    for name in (level, f"{level}_nether", f"{level}_the_end"):
        path = server_root / name
        if path.is_symlink():
            return (
                f"The configured world path {name!r} is a symbolic link. "
                "Crucible refuses to copy or move a world through a symlink."
            )
        try:
            if path.resolve(strict=False).parent != root_resolved:
                return f"The configured world path {name!r} resolves outside the server folder."
        except OSError:
            return f"The configured world path {name!r} could not be resolved safely."
    return None


def configured_world_path(root: str | Path) -> Path:
    """Return the world root only when it is a safe direct child of the server."""
    server_root = Path(root).expanduser()
    issue = world_name_issue(server_root)
    if issue:
        raise UnsafeWorldPathError(issue)
    level = configured_level_name(server_root)
    if level is None:
        raise UnsafeWorldPathError("The configured world name is unsafe.")
    candidate = server_root / level
    resolved_root = server_root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate.parent != resolved_root or candidate.is_symlink():
        raise UnsafeWorldPathError(
            "The configured world root must be a non-symlink directory directly inside the server folder."
        )
    return candidate


def world_names(root: str | Path) -> list[str]:
    """Return present, safe world/dimension directory names."""
    server_root = Path(root).expanduser()
    if world_name_issue(server_root):
        return []
    level = configured_level_name(server_root)
    if level is None:
        return []
    names = [level, f"{level}_nether", f"{level}_the_end"]
    return [
        name for name in names
        if (server_root / name).is_dir() and not (server_root / name).is_symlink()
    ]
