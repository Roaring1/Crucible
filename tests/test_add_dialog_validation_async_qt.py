from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog

from crucible.data.instance_manager import InstanceManager
from crucible.data.instance_model import ServerInstance
from crucible.ui.add_dialog import AddInstanceDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _server(
    root: Path, *, start_script: bool = True, nested: bool = True
) -> Path:
    (root / "mods").mkdir(parents=True)
    (root / "mods" / "top-level.jar").write_bytes(b"mod")
    if nested:
        (root / "mods" / "nested").mkdir()
        (root / "mods" / "nested" / "bundled.jar").write_bytes(b"library")
    (root / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    if start_script:
        (root / "start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return root


def _wait_for_validation(app: QApplication, dialog: AddInstanceDialog, timeout=4.0) -> None:
    deadline = time.monotonic() + timeout
    while dialog._validation_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert dialog._validation_thread is None


def test_manual_folder_validation_runs_off_gui_thread_and_registers_after_success(
    tmp_path, monkeypatch
):
    app = _app()
    manager = InstanceManager(tmp_path / "registry")
    manager.load()
    server = _server(tmp_path / "server", nested=False)
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []
    original = ServerInstance.validate

    def slow_validate(instance, **kwargs):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(3.0)
        return original(instance, **kwargs)

    monkeypatch.setattr(ServerInstance, "validate", slow_validate)
    dialog = AddInstanceDialog(manager)
    dialog._path_edit.setText(str(server))
    dialog._name_edit.setText("Synthetic Server")
    dialog.show()
    main_thread = threading.get_ident()
    try:
        started_at = time.monotonic()
        dialog._on_accept()
        call_ms = (time.monotonic() - started_at) * 1000
        assert call_ms < 50
        assert started.wait(1.0)
        assert len(worker_threads) == 1
        assert worker_threads[0] != main_thread
        assert not dialog._add_button.isEnabled()
        assert "Checking server files" in dialog._warn_label.text()
        dialog.reject()
        assert dialog.isVisible(), "dialog must not close while validation is active"
    finally:
        release.set()
        _wait_for_validation(app, dialog)

    assert len(worker_threads) == 1, "registry commit must reuse the worker validation result"
    assert len(manager.instances) == 1
    assert dialog.result_instance is manager.instances[0]
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_manual_folder_warnings_still_require_explicit_add_anyway(tmp_path):
    app = _app()
    manager = InstanceManager(tmp_path / "registry")
    manager.load()
    server = _server(tmp_path / "server", start_script=False)
    dialog = AddInstanceDialog(manager)
    dialog._path_edit.setText(str(server))
    dialog._name_edit.setText("Incomplete Server")
    try:
        dialog._on_accept()
        _wait_for_validation(app, dialog)
        assert dialog._pending_manual_registration
        assert dialog._add_button.text() == "Add anyway"
        assert not manager.instances
        assert "No start script found" in dialog._warn_label.text()

        dialog._on_accept()
        assert len(manager.instances) == 1
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        if dialog.isVisible():
            dialog.reject()


def test_imported_candidate_validation_also_runs_off_gui_thread(tmp_path, monkeypatch):
    app = _app()
    manager = InstanceManager(tmp_path / "registry")
    manager.load()
    source = tmp_path / "source"
    source.mkdir()
    target = _server(tmp_path / "Imported Server")
    info = SimpleNamespace(
        name="Imported Server",
        version_label="2.9.0",
        source_type="Prism",
        minecraft_version="1.7.10",
        loader="forge",
        loader_version="14.23.5",
        warnings=["One optional file was skipped"],
    )
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []
    original = ServerInstance.validate

    def slow_validate(instance, **kwargs):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(3.0)
        return original(instance, **kwargs)

    monkeypatch.setattr(ServerInstance, "validate", slow_validate)
    dialog = AddInstanceDialog(manager)
    dialog._import_pending = (str(source), str(target))
    main_thread = threading.get_ident()
    try:
        dialog._complete_prism_import(info, "")
        assert started.wait(1.0)
        assert worker_threads and worker_threads[0] != main_thread
        assert not dialog._add_button.isEnabled()
        assert "Checking server files" in dialog._warn_label.text()
        release.set()
        _wait_for_validation(app, dialog)

        assert dialog._pending_import_registration is not None
        assert not dialog._import_folder_btn.isEnabled()
        assert not dialog._import_archive_btn.isEnabled()
        assert not dialog._advanced_body.isEnabled()
        assert dialog.result_instance is not None
        assert "One optional file was skipped" in dialog._warn_label.text()
        assert "Import complete" not in dialog._warn_label.text()
        assert not manager.instances
        dialog._on_accept()
        assert len(worker_threads) == 1, "import registration must not repeat validation"
        assert len(manager.instances) == 1
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        release.set()
        if dialog.isVisible():
            dialog.reject()


def test_nested_jar_validation_warning_is_bounded_for_large_packs(tmp_path):
    server = _server(tmp_path / "server")
    nested = server / "mods" / "nested"
    for index in range(250):
        (nested / f"bundled-{index:03}.jar").write_bytes(b"library")

    problems = ServerInstance(str(server), "Large Pack").validate()
    warning = next(item for item in problems if "Nested jar(s)" in item)

    assert "251 found" in warning
    assert "and 239 more" in warning
    assert len(warning) < 700


def test_editing_manual_candidate_invalidates_cached_validation(tmp_path):
    app = _app()
    manager = InstanceManager(tmp_path / "registry")
    manager.load()
    incomplete = _server(tmp_path / "incomplete", start_script=False)
    complete = _server(tmp_path / "complete", nested=False)
    dialog = AddInstanceDialog(manager)
    dialog._path_edit.setText(str(incomplete))
    dialog._name_edit.setText("Original")
    try:
        dialog._on_accept()
        _wait_for_validation(app, dialog)
        assert dialog._pending_manual_registration
        assert "No start script found" in dialog._warn_label.text()

        dialog._path_edit.setText(str(complete))
        assert not dialog._pending_manual_registration
        assert dialog._manual_validation_problems is None
        assert dialog._add_button.text() == "Add server"
        assert "Check the folder again" in dialog._warn_label.text()

        dialog._on_accept()
        _wait_for_validation(app, dialog)
        assert len(manager.instances) == 1
        assert Path(manager.instances[0].path) == complete.resolve()
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        if dialog.isVisible():
            dialog.reject()
