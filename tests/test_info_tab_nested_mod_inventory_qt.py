from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from crucible.data.instance_model import ServerInstance
from crucible.ui.tabs import info_tab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _server(root: Path) -> ServerInstance:
    (root / "mods" / "IC2").mkdir(parents=True)
    (root / "mods" / "main-mod.jar").write_bytes(b"top-level mod")
    (root / "mods" / "IC2" / "bundled-lib.jar").write_bytes(b"nested library")
    (root / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    (root / "start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return ServerInstance(str(root), "Example Server", tmux_session="example-server")


def test_info_tab_load_reuses_one_nested_jar_inventory_for_validation_and_display(
    tmp_path, monkeypatch
):
    app = _app()
    instance = _server(tmp_path / "server")
    original = instance.get_bundled_jars
    calls = []

    def scan_bundled_jars():
        calls.append("scan")
        return original()

    monkeypatch.setattr(instance, "get_bundled_jars", scan_bundled_jars)
    # The System tab has its own /proc sampling behavior. It is not the subject
    # of this test, so keep this Qt harness limited to InfoTab's validation UI.
    monkeypatch.setattr(info_tab.SystemTab, "load", lambda _self, _instance: None)

    tab = info_tab.InfoTab()
    try:
        tab.load(instance)

        labels = [item.text() for item in tab.findChildren(QLabel)]
        visible_text = "\n".join(labels)

        assert calls == ["scan"]
        assert "Nested jar(s) in mods/ subdirectory" in visible_text
        assert "1 enabled  +  1 bundled (unmanaged)" in visible_text
    finally:
        tab.close()
        app.processEvents()


def test_validation_uses_supplied_nested_jar_inventory_without_rescanning(tmp_path, monkeypatch):
    instance = _server(tmp_path / "server")
    bundled = [tmp_path / "server" / "mods" / "IC2" / "bundled-lib.jar"]

    def unexpected_scan():
        raise AssertionError("validate() repeated the nested mods/ scan")

    monkeypatch.setattr(instance, "get_bundled_jars", unexpected_scan)

    problems = instance.validate(bundled_jars=bundled)

    assert any("Nested jar(s) in mods/ subdirectory" in item for item in problems)


def test_standalone_validation_keeps_its_existing_nested_jar_scan(tmp_path):
    instance = _server(tmp_path / "server")

    problems = instance.validate()

    assert any("Nested jar(s) in mods/ subdirectory" in item for item in problems)
