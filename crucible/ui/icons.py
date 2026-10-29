"""Small, local, allow-listed SVG icon family for high-value actions."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QIcon


_ICON_FILES: dict[str, str] = {
    "folder-open": "folder-open.svg",
    "archive": "archive.svg",
    "refresh-cw": "refresh-cw.svg",
}


def local_icon(name: str) -> QIcon:
    """Return a bundled icon by semantic name; unknown names never resolve paths."""
    filename = _ICON_FILES.get(name)
    if filename is None:
        return QIcon()
    icon_path = Path(__file__).resolve().parent.parent / "assets" / "icons" / filename
    if not icon_path.is_file():
        return QIcon()
    return QIcon(str(icon_path))
