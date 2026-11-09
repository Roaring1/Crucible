from __future__ import annotations

import os
import threading
import time
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
    main_thread = threading.get_ident()
    scan_threads = []

    def scan_bundled_jars():
        calls.append("scan")
        scan_threads.append(threading.get_ident())
        return original()

    monkeypatch.setattr(instance, "get_bundled_jars", scan_bundled_jars)
    # The System tab has its own /proc sampling behavior. It is not the subject
    # of this test, so keep this Qt harness limited to InfoTab's validation UI.
    monkeypatch.setattr(info_tab.SystemTab, "load", lambda _self, _instance: None)
    monkeypatch.setattr(info_tab.SystemTab, "showEvent", lambda _self, _event: None)

    tab = info_tab.InfoTab()
    try:
        tab.load(instance)
        assert calls == []
        assert any(
            item.text() == "Checking server files and nested mod inventory…"
            for item in tab.findChildren(QLabel)
        )

        tab.show()
        deadline = time.monotonic() + 3.0
        while tab._thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

        labels = [item.text() for item in tab.findChildren(QLabel)]
        visible_text = "\n".join(labels)

        assert tab._thread is None
        assert calls == ["scan"]
        assert len(scan_threads) == 1 and scan_threads[0] != main_thread
        assert "Nested jar(s) in mods/ subdirectory" in visible_text
        assert "1 enabled  +  1 bundled (unmanaged)" in visible_text
    finally:
        tab.close()
        app.processEvents()


def test_info_tab_discards_old_inventory_when_selected_server_changes(
    tmp_path, monkeypatch
):
    app = _app()
    first = _server(tmp_path / "first")
    second = _server(tmp_path / "second")
    first.name = "First Server"
    second.name = "Second Server"
    original_scan = first.get_bundled_jars
    first_started = threading.Event()
    release_first = threading.Event()

    def slow_first_scan():
        first_started.set()
        assert release_first.wait(3.0)
        return original_scan()

    monkeypatch.setattr(first, "get_bundled_jars", slow_first_scan)
    monkeypatch.setattr(info_tab.SystemTab, "load", lambda _self, _instance: None)
    monkeypatch.setattr(info_tab.SystemTab, "showEvent", lambda _self, _event: None)

    tab = info_tab.InfoTab()
    try:
        tab.load(first)
        tab.show()
        assert first_started.wait(1.0)

        tab.load(second)
        assert tab._refresh_pending
        assert any(
            item.text() == "Checking server files and nested mod inventory…"
            for item in tab.findChildren(QLabel)
        )
        release_first.set()

        deadline = time.monotonic() + 4.0
        while tab._thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

        labels = [item.text() for item in tab.findChildren(QLabel)]
        assert tab._thread is None
        assert "Second Server" in labels
        assert "First Server" not in labels
    finally:
        release_first.set()
        if tab._thread is not None:
            deadline = time.monotonic() + 3.0
            while tab._thread is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
        tab.close()
        app.processEvents()


def test_info_tab_hidden_during_scan_retries_on_next_show(tmp_path, monkeypatch):
    app = _app()
    instance = _server(tmp_path / "server")
    original_scan = instance.get_bundled_jars
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    def first_scan_blocks_once():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(3.0)
        return original_scan()

    monkeypatch.setattr(instance, "get_bundled_jars", first_scan_blocks_once)
    monkeypatch.setattr(info_tab.SystemTab, "load", lambda _self, _instance: None)
    monkeypatch.setattr(info_tab.SystemTab, "showEvent", lambda _self, _event: None)

    tab = info_tab.InfoTab()
    try:
        tab.load(instance)
        tab.show()
        assert first_started.wait(1.0)

        tab.hide()
        assert tab._refresh_pending
        release_first.set()

        deadline = time.monotonic() + 3.0
        while tab._thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert tab._thread is None
        assert tab._refresh_pending

        tab.show()
        deadline = time.monotonic() + 3.0
        while tab._thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

        labels = [item.text() for item in tab.findChildren(QLabel)]
        assert tab._thread is None
        assert len(calls) == 2
        assert "Example Server" in labels
    finally:
        release_first.set()
        if tab._thread is not None:
            deadline = time.monotonic() + 3.0
            while tab._thread is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
        tab.close()
        app.processEvents()


def test_info_tab_reports_unreadable_nested_inventory_without_stale_health(
    tmp_path, monkeypatch
):
    app = _app()
    instance = _server(tmp_path / "server")

    def denied_scan():
        raise PermissionError("synthetic mods inventory denial")

    monkeypatch.setattr(instance, "get_bundled_jars", denied_scan)
    monkeypatch.setattr(info_tab.SystemTab, "load", lambda _self, _instance: None)
    monkeypatch.setattr(info_tab.SystemTab, "showEvent", lambda _self, _event: None)

    tab = info_tab.InfoTab()
    try:
        tab.load(instance)
        assert "Checking" in tab._health_label.text()
        tab.show()

        deadline = time.monotonic() + 3.0
        while tab._thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

        assert tab._thread is None
        assert "validation unavailable" in tab._health_label.text()
        assert "synthetic mods inventory denial" in tab._health_label.text()
        assert any(
            "could not read this server folder" in item.text()
            for item in tab.findChildren(QLabel)
        )
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
