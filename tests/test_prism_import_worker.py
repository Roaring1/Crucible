"""Tests for the single-window Prism/modpack import operation."""
from types import SimpleNamespace
import zipfile

from crucible.ui import add_dialog
from crucible.ui.add_dialog import _PrismImportWorker


def test_import_worker_attempts_missing_server_mod_downloads(monkeypatch, tmp_path):
    source = tmp_path / "example.mrpack"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("modrinth.index.json", "{}")
    target = tmp_path / "server"
    seen = {}
    expected = SimpleNamespace(name="Example pack")

    def fake_import(source, target, **kwargs):
        seen["source"] = source
        seen["target"] = target
        seen.update(kwargs)
        kwargs["download_log_cb"]("Found pack index; starting download")
        kwargs["download_progress_cb"](2, 5)
        return expected

    monkeypatch.setattr(add_dialog, "import_prism_source", fake_import)

    worker = _PrismImportWorker(str(source), str(target))
    logs = []
    progress = []
    finished = []
    worker.log.connect(logs.append)
    worker.progress.connect(lambda done, total: progress.append((done, total)))
    worker.finished.connect(lambda info, error: finished.append((info, error)))

    worker.run()

    assert seen["source"] == str(source)
    assert seen["target"] == str(target)
    assert seen["overwrite"] is False
    assert seen["download_mods"] is True
    assert logs == [
        "Copying pack files and checking the server-mod list…",
        "Found pack index; starting download",
    ]
    assert progress == [(2, 5)]
    assert finished == [(expected, "")]


def test_installed_prism_folder_does_not_warn_about_missing_index(monkeypatch, tmp_path):
    source = tmp_path / "installed-prism-instance"
    source.mkdir()
    target = tmp_path / "server"
    seen = {}

    def fake_import(_source, _target, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(name="Installed pack")

    monkeypatch.setattr(add_dialog, "import_prism_source", fake_import)
    worker = _PrismImportWorker(str(source), str(target))
    finished = []
    worker.finished.connect(lambda info, error: finished.append((info, error)))

    worker.run()

    assert seen["download_mods"] is False
    assert finished[0][1] == ""


def test_import_worker_reports_exceptions_without_raising(monkeypatch):
    def fail_import(*_args, **_kwargs):
        raise OSError("source drive disconnected")

    monkeypatch.setattr(add_dialog, "import_prism_source", fail_import)
    worker = _PrismImportWorker("/source/example.mrpack", "/target")
    finished = []
    worker.finished.connect(lambda info, error: finished.append((info, error)))

    worker.run()

    assert finished == [(None, "source drive disconnected")]


def test_server_pack_archive_without_index_skips_download_attempt(monkeypatch, tmp_path):
    source = tmp_path / "server-pack.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("start.sh", "#!/bin/sh\\n")
    seen = {}

    def fake_import(_source, _target, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(name="Server pack")

    monkeypatch.setattr(add_dialog, "import_prism_source", fake_import)
    worker = _PrismImportWorker(str(source), str(tmp_path / "server"))
    worker.run()

    assert seen["download_mods"] is False
