"""Per-instance server rail artwork.

Custom images live in Crucible-owned application data, keyed by the stable
instance ID. The server pack is only read for the standard server-icon.png;
the pack itself is never modified by the appearance picker.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImageReader

_ICON_STORE = Path.home() / ".local" / "share" / "crucible" / "server-icons"
_PRESET_NAMES = (
    "grass-block.svg",
    "redstone-block.svg",
    "ender-chest.svg",
    "stone-block.svg",
)
_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_SOURCE_EDGE = 4096
_OUTPUT_SIZE = 96


def _instance_key(instance_id: str) -> str:
    """Hash the registry ID so it can never become a path component."""
    return hashlib.sha256(str(instance_id).encode("utf-8")).hexdigest()


def custom_icon_path(instance_id: str) -> Path:
    """Crucible-owned PNG override for an instance."""
    return _ICON_STORE / f"{_instance_key(instance_id)}.png"


def _preset_path(instance_id: str) -> Path:
    digest = hashlib.sha256(str(instance_id).encode("utf-8")).digest()
    name = _PRESET_NAMES[int.from_bytes(digest[:2], "big") % len(_PRESET_NAMES)]
    return Path(__file__).resolve().parents[1] / "ui" / "assets" / "server-icons" / name


def _readable_image(path: Path) -> bool:
    """Check image format and dimensions without decoding arbitrary huge files."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            return False
        reader = QImageReader(str(path))
        if not reader.canRead():
            return False
        size = reader.size()
        return not (
            size.isValid()
            and (
                size.width() > _MAX_SOURCE_EDGE
                or size.height() > _MAX_SOURCE_EDGE
                or size.width() * size.height() > _MAX_SOURCE_EDGE * _MAX_SOURCE_EDGE
            )
        )
    except OSError:
        return False


def _prism_instance_root(instance) -> Path | None:
    """Return the original Prism instance folder, when it is still available."""
    source = Path(str(getattr(instance, "prism_source", "") or "")).expanduser()
    if source.is_symlink() or not source.is_dir():
        return None
    if (source / "instance.cfg").is_file() and not (
        source / "instance.cfg"
    ).is_symlink():
        return source
    return None


def _read_prism_icon_key(instance_root: Path) -> str | None:
    config = instance_root / "instance.cfg"
    try:
        if (
            config.is_symlink()
            or not config.is_file()
            or config.stat().st_size > 256 * 1024
        ):
            return None
        raw = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Prism/MultiMC instance.cfg is INI-like, but often lacks a section header.
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip().casefold()] = value.strip().strip('"').strip("'")
    icon_key = values.get("iconkey")
    if (
        not icon_key
        or icon_key in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9_. -]{1,120}", icon_key)
    ):
        return None
    return icon_key


def _prism_icons_dir(instance_root: Path) -> Path:
    """Resolve Prism's configured IconsDir without launching or contacting Prism."""
    data_root = instance_root.parent.parent
    config_candidates = (
        Path.home() / ".config" / "PrismLauncher" / "prismlauncher.cfg",
        Path.home()
        / ".var"
        / "app"
        / "org.prismlauncher.PrismLauncher"
        / "config"
        / "PrismLauncher"
        / "prismlauncher.cfg",
        data_root / "prismlauncher.cfg",
    )
    for config in config_candidates:
        if config.is_symlink() or not config.is_file():
            continue
        try:
            if config.stat().st_size > 512 * 1024:
                continue
            raw = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(
            r"^\s*IconsDir\s*=\s*(.*?)\s*$", raw, re.MULTILINE | re.IGNORECASE
        )
        if not match:
            continue
        configured = Path(match.group(1).strip().strip('"').strip("'")).expanduser()
        return configured if configured.is_absolute() else data_root / configured
    return data_root / "icons"


def _prism_icon_path(instance) -> Path | None:
    """Read a user/pack icon already assigned in Prism's local IconsDir.

    No Prism processes are started and no network request is made. The
    original source may no longer exist; default to the standard native and
    Flatpak Prism data roots only when the registry still records a Prism
    source path.
    """
    instance_root = _prism_instance_root(instance)
    if instance_root is None:
        return None
    icon_key = _read_prism_icon_key(instance_root)
    if icon_key is None:
        return None

    roots = [_prism_icons_dir(instance_root)]
    for root in (
        Path.home() / ".local" / "share" / "PrismLauncher" / "icons",
        Path.home()
        / ".var"
        / "app"
        / "org.prismlauncher.PrismLauncher"
        / "data"
        / "PrismLauncher"
        / "icons",
    ):
        if root not in roots:
            roots.append(root)

    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        for suffix in (".png", ".ico", ".svg", ".webp"):
            candidate = root / f"{icon_key}{suffix}"
            if _readable_image(candidate):
                return candidate
    return None


def resolve_icon_path(instance) -> Path:
    """Resolve owner override, server/pack art, Prism icon, then SVG preset."""
    custom = custom_icon_path(instance.id)
    if _readable_image(custom):
        return custom

    server_root = Path(instance.path)
    for filename in ("server-icon.png", "pack-icon.png", "icon.png"):
        pack_icon = server_root / filename
        if _readable_image(pack_icon):
            return pack_icon

    prism_icon = _prism_icon_path(instance)
    if prism_icon is not None:
        return prism_icon

    fallback = _preset_path(instance.id)
    return fallback


def set_custom_icon(instance_id: str, source_path: str | Path) -> Path:
    """Validate, center-crop and privately store a user-selected image as PNG."""
    source = Path(source_path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise ValueError("Choose a regular image file, not a folder or symbolic link.")
    try:
        if source.stat().st_size > _MAX_SOURCE_BYTES:
            raise ValueError("The selected image is larger than 20 MB.")
    except OSError as exc:
        raise OSError(f"Could not read the selected image: {exc}") from exc

    reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and (
        size.width() > _MAX_SOURCE_EDGE
        or size.height() > _MAX_SOURCE_EDGE
        or size.width() * size.height() > _MAX_SOURCE_EDGE * _MAX_SOURCE_EDGE
    ):
        raise ValueError("The selected image is too large to use as a server icon.")

    image = reader.read()
    if image.isNull():
        error = reader.errorString() or "unsupported or damaged image"
        raise ValueError(f"Could not decode the selected image: {error}")
    if image.width() > _MAX_SOURCE_EDGE or image.height() > _MAX_SOURCE_EDGE:
        raise ValueError("The selected image dimensions exceed 4096 × 4096.")

    scaled = image.scaled(
        _OUTPUT_SIZE,
        _OUTPUT_SIZE,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    left = max(0, (scaled.width() - _OUTPUT_SIZE) // 2)
    top = max(0, (scaled.height() - _OUTPUT_SIZE) // 2)
    cropped = scaled.copy(left, top, _OUTPUT_SIZE, _OUTPUT_SIZE)

    target = custom_icon_path(instance_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{_instance_key(instance_id)[:12]}-",
        suffix=".png",
        dir=target.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        if not cropped.save(str(temporary), "PNG"):
            raise OSError("Qt could not encode the selected image as PNG.")
        temporary.chmod(0o600)
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return target


def clear_custom_icon(instance_id: str) -> None:
    """Remove only the Crucible-owned override; pack/default imagery reappears."""
    target = custom_icon_path(instance_id)
    if target.is_symlink():
        raise OSError("Refusing to remove a symbolic link from the icon store.")
    target.unlink(missing_ok=True)


def has_pack_icon(instance) -> bool:
    return _readable_image(Path(instance.path) / "server-icon.png")
