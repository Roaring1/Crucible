"""Server rail artwork stays in Crucible-owned data and never edits a pack."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QImage

from crucible.data import server_icons


def _image(path: Path, width: int = 128, height: int = 64) -> Path:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0xFF4F8A67)
    assert image.save(str(path), "PNG")
    return path


def test_server_icon_resolution_prefers_custom_then_pack_then_stable_svg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "private-icons"
    monkeypatch.setattr(server_icons, "_ICON_STORE", store)
    server = tmp_path / "server"
    server.mkdir()
    instance = SimpleNamespace(id="stable-server-id", path=str(server))

    fallback = server_icons.resolve_icon_path(instance)
    assert fallback.suffix == ".svg"
    assert fallback.is_file()
    assert fallback.parent.name == "server-icons"

    pack_icon = _image(server / "server-icon.png")
    assert server_icons.resolve_icon_path(instance) == pack_icon

    custom_source = _image(tmp_path / "owner-image.png", 320, 80)
    custom = server_icons.set_custom_icon(instance.id, custom_source)
    assert custom.parent == store
    assert custom.suffix == ".png"
    assert server_icons.resolve_icon_path(instance) == custom


def test_resolve_uses_existing_prism_icon_key_without_copying_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "private-icons"
    monkeypatch.setattr(server_icons, "_ICON_STORE", store)

    prism_root = tmp_path / "PrismLauncher"
    instance_root = prism_root / "instances" / "Pack"
    instance_root.mkdir(parents=True)
    (instance_root / "instance.cfg").write_text(
        "name=Pack\niconKey=pack_custom\n",
        encoding="utf-8",
    )
    (prism_root / "prismlauncher.cfg").write_text(
        "IconsDir=custom-icons\n",
        encoding="utf-8",
    )
    icons_dir = prism_root / "custom-icons"
    icons_dir.mkdir()
    prism_icon = _image(icons_dir / "pack_custom.png")

    server = tmp_path / "server"
    server.mkdir()
    instance = SimpleNamespace(
        id="prism-instance-id",
        path=str(server),
        prism_source=str(instance_root),
    )

    resolved = server_icons.resolve_icon_path(instance)
    assert resolved == prism_icon
    assert resolved.read_bytes() == prism_icon.read_bytes()
    assert not store.exists(), (
        "reading the Prism icon must not copy it into Crucible's store"
    )
    assert (instance_root / "instance.cfg").read_text(encoding="utf-8") == (
        "name=Pack\niconKey=pack_custom\n"
    )


def test_pack_server_icon_precedes_prism_instance_icon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_icons, "_ICON_STORE", tmp_path / "private-icons")
    prism_root = tmp_path / "PrismLauncher"
    instance_root = prism_root / "instances" / "Pack"
    instance_root.mkdir(parents=True)
    (instance_root / "instance.cfg").write_text(
        "name=Pack\niconKey=pack_custom\n", encoding="utf-8"
    )
    prism_icons = prism_root / "icons"
    prism_icons.mkdir()
    _image(prism_icons / "pack_custom.png")

    server = tmp_path / "server"
    server.mkdir()
    pack_icon = _image(server / "server-icon.png")
    instance = SimpleNamespace(
        id="pack-first-id", path=str(server), prism_source=str(instance_root)
    )

    assert server_icons.resolve_icon_path(instance) == pack_icon

    custom = server_icons.set_custom_icon(
        instance.id, _image(tmp_path / "owner.png", 64, 64)
    )
    assert server_icons.resolve_icon_path(instance) == custom


def test_all_server_statuses_have_packaged_nonempty_svg_icons() -> None:
    asset_root = Path(server_icons.__file__).resolve().parents[1] / "ui" / "assets"
    statuses = (
        "running",
        "stopped",
        "starting",
        "stopping",
        "tmux_missing",
        "missing",
        "unmanaged",
        "unknown",
    )
    for status in statuses:
        asset = asset_root / "status-icons" / f"{status}.svg"
        assert asset.is_file(), status
        icon = __import__("PyQt6.QtGui", fromlist=["QIcon"]).QIcon(str(asset))
        assert not icon.isNull(), status

    menu_names = (
        "edit",
        "play",
        "stop",
        "folder",
        "archive",
        "copy",
        "search-log",
        "repair",
        "remove",
    )
    for name in menu_names:
        asset = asset_root / "menu-icons" / f"{name}.svg"
        assert asset.is_file(), name
        icon = __import__("PyQt6.QtGui", fromlist=["QIcon"]).QIcon(str(asset))
        assert not icon.isNull(), name


def test_custom_server_icon_is_normalized_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "private-icons"
    monkeypatch.setattr(server_icons, "_ICON_STORE", store)
    source = _image(tmp_path / "source.png", 200, 100)

    result = server_icons.set_custom_icon("instance-1", source)
    decoded = QImage(str(result))
    assert decoded.width() == 96
    assert decoded.height() == 96
    assert result.stat().st_mode & 0o777 == 0o600
    assert store.stat().st_mode & 0o777 == 0o700


def test_custom_icon_clear_restores_pack_or_builtin_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "private-icons"
    monkeypatch.setattr(server_icons, "_ICON_STORE", store)
    server = tmp_path / "server"
    server.mkdir()
    instance = SimpleNamespace(id="instance-2", path=str(server))
    custom = server_icons.set_custom_icon(instance.id, _image(tmp_path / "custom.png"))
    assert server_icons.resolve_icon_path(instance) == custom

    server_icons.clear_custom_icon(instance.id)
    assert not custom.exists()
    assert server_icons.resolve_icon_path(instance).suffix == ".svg"


def test_icon_store_key_cannot_become_a_path_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "private-icons"
    monkeypatch.setattr(server_icons, "_ICON_STORE", store)
    target = server_icons.custom_icon_path("../../outside/../../server")
    assert target.parent == store
    assert target.name.endswith(".png")
    assert "/" not in target.name
    assert "\\" not in target.name


def test_invalid_or_oversized_server_images_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_icons, "_ICON_STORE", tmp_path / "private-icons")
    invalid = tmp_path / "not-an-image.txt"
    invalid.write_text("not an image", encoding="utf-8")
    with pytest.raises(ValueError, match="decode"):
        server_icons.set_custom_icon("instance-3", invalid)

    oversized_dimensions = _image(tmp_path / "huge.png", 4097, 1)
    with pytest.raises(ValueError, match="too large|dimensions"):
        server_icons.set_custom_icon("instance-3", oversized_dimensions)
