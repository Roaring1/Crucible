"""Fail-closed provider project URLs and dialog opening paths."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6.QtWidgets import QApplication

from crucible.mods.modrinth import ModHit
from crucible.ui.add_mod_dialog import AddModDialog
from crucible.ui.modpack_dialog import ModpackDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_curseforge_url_requires_valid_numeric_id_and_provider_slug():
    assert ModHit("cf:123", "", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", "   ", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", ".", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", "..", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", "bad\r\nslug", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", "bad" + chr(0x202E) + "slug", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", "bad" + chr(0xD800) + "slug", "Example", "", 0).page_url() == ""
    assert ModHit("cf:bad-id", "example-mod", "Example", "", 0).page_url() == ""
    assert ModHit("cf:", "example-mod", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", " example-mod", "Example", "", 0).page_url() == ""
    assert ModHit("cf:123", "example-mod", "Example", "", 0).page_url() == (
        "https://www.curseforge.com/minecraft/mc-mods/example-mod"
    )
    assert ModHit("cf:123", "unsafe slug/fragment", "Example", "", 0).page_url() == (
        "https://www.curseforge.com/minecraft/mc-mods/unsafe%20slug%2Ffragment"
    )


def test_invalid_or_missing_project_ids_fail_closed():
    assert ModHit("", "example", "Example", "", 0).page_url() == ""
    assert ModHit("bad/id", "example", "Example", "", 0).page_url() == ""
    assert ModHit("-", "example", "Example", "", 0).page_url() == ""
    assert ModHit("_", "example", "Example", "", 0).page_url() == ""
    assert ModHit("bad id", "example", "Example", "", 0).page_url() == ""
    assert ModHit("bad\x00id", "example", "Example", "", 0).page_url() == ""
    assert ModHit("bad" + chr(0x202E) + "id", "example", "Example", "", 0).page_url() == ""
    assert ModHit(None, "example", "Example", "", 0).page_url() == ""


def test_modrinth_url_uses_encoded_slug_or_safe_id_fallback():
    assert ModHit("example-id", "", "Example", "", 0).page_url() == (
        "https://modrinth.com/mod/example-id"
    )
    assert ModHit("example-id", "example-mod", "Example", "", 0).page_url() == (
        "https://modrinth.com/mod/example-mod"
    )
    assert ModHit("example-id", "unsafe slug/fragment", "Example", "", 0).page_url() == (
        "https://modrinth.com/mod/unsafe%20slug%2Ffragment"
    )
    assert ModHit("example-id", "bad\nslug", "Example", "", 0).page_url() == ""
    assert ModHit("example-id", "..", "Example", "", 0).page_url() == ""


def _new_add_mod_dialog():
    instance = SimpleNamespace(loader="fabric", minecraft_version="1.21.1")
    with patch.object(AddModDialog, "_do_search", lambda *_a, **_kw: None):
        dialog = AddModDialog(instance)
    return dialog


def _new_modpack_dialog():
    manager = SimpleNamespace(instances=[])
    with patch.object(ModpackDialog, "_do_search", lambda *_a, **_kw: None):
        dialog = ModpackDialog(manager)
    return dialog


@pytest.mark.parametrize("kind", ["mod", "modpack"])
def test_empty_page_url_is_reported_and_never_opened(qapp, kind):
    dialog = _new_add_mod_dialog() if kind == "mod" else _new_modpack_dialog()
    hit = SimpleNamespace(page_url=lambda: "", title="Example")
    if kind == "mod":
        dialog._view = [hit]
    else:
        dialog._all_hits = [hit]
    dialog._results.blockSignals(True)  # isolate _open_page from selection-slot behavior
    dialog._results.addItem("Example")
    dialog._results.setCurrentRow(0)
    with patch("PyQt6.QtWidgets.QMessageBox.information") as info, \
         patch("PyQt6.QtGui.QDesktopServices.openUrl") as open_url:
        dialog._open_page()
    info.assert_called_once()
    assert info.call_args.args[1] == "Project page unavailable"
    assert "No guessed URL was opened." in info.call_args.args[2]
    open_url.assert_not_called()
    dialog.reject()


@pytest.mark.parametrize("kind", ["mod", "modpack"])
def test_valid_page_url_is_opened_verbatim(qapp, kind):
    expected = (
        "https://www.curseforge.com/minecraft/mc-mods/example-mod"
        if kind == "mod"
        else "https://modrinth.com/mod/example-pack"
    )
    dialog = _new_add_mod_dialog() if kind == "mod" else _new_modpack_dialog()
    hit = SimpleNamespace(page_url=lambda: expected, title="Example")
    if kind == "mod":
        dialog._view = [hit]
    else:
        dialog._all_hits = [hit]
    dialog._results.blockSignals(True)  # isolate _open_page from selection-slot behavior
    dialog._results.addItem("Example")
    dialog._results.setCurrentRow(0)
    with patch("PyQt6.QtWidgets.QMessageBox.information") as info, \
         patch("PyQt6.QtGui.QDesktopServices.openUrl") as open_url:
        dialog._open_page()
    info.assert_not_called()
    open_url.assert_called_once()
    assert open_url.call_args.args[0].toString() == expected
    dialog.reject()
