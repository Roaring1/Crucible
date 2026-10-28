from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from crucible.exporters.client_export import ExportResult
from crucible.ui.client_export_dialog import ClientExportDialog


class _Manager:
    def __init__(self, fail: bool = False):
        self.updated = []
        self.fail = fail

    def update_instance(self, instance):
        if self.fail:
            raise OSError("registry is read-only")
        self.updated.append((instance.id, instance.prism_source))


class ClientExportDialogSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _source(self, parent: Path) -> Path:
        source = parent / "known-good-prism"
        source.mkdir()
        (source / "mmc-pack.json").write_text(json.dumps({
            "formatVersion": 1,
            "components": [
                {"uid": "net.minecraft", "version": "1.21.1"},
                {"uid": "net.neoforged", "version": "21.1.233"},
            ],
        }), encoding="utf-8")
        return source

    def _result(self):
        return ExportResult(
            path="/tmp/test-client.zip",
            fmt="prism",
            mod_count=2,
            warnings=[],
            manifest_path="Demo/crucible-manifest.json",
            archive_sha256="a" * 64,
        )

    def test_successful_export_remembers_source_in_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            instance = SimpleNamespace(name="Demo", id="instance-1", prism_source="")
            manager = _Manager()
            dialog = ClientExportDialog(instance, manager=manager)
            dialog._source.setText(str(source))
            dialog._remember_source.setChecked(True)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            with patch.object(QMessageBox, "information"):
                dialog._on_done(self._result())
            self.assertEqual(instance.prism_source, str(source.resolve()))
            self.assertEqual(manager.updated, [("instance-1", str(source.resolve()))])
            dialog.close()

    def test_failed_export_does_not_persist_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            instance = SimpleNamespace(name="Demo", id="instance-2", prism_source="")
            manager = _Manager()
            dialog = ClientExportDialog(instance, manager=manager)
            dialog._source.setText(str(source))
            dialog._remember_source.setChecked(True)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            with patch.object(QMessageBox, "warning"):
                dialog._on_done(ExportResult(error="source version mismatch"))
            self.assertEqual(instance.prism_source, "")
            self.assertEqual(manager.updated, [])
            dialog.close()

    def test_unchecked_remember_option_leaves_registry_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            instance = SimpleNamespace(name="Demo", id="instance-3", prism_source="")
            manager = _Manager()
            dialog = ClientExportDialog(instance, manager=manager)
            dialog._source.setText(str(source))
            dialog._remember_source.setChecked(False)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            with patch.object(QMessageBox, "information"):
                dialog._on_done(self._result())
            self.assertEqual(instance.prism_source, "")
            self.assertEqual(manager.updated, [])
            dialog.close()

    def test_registry_failure_restores_in_memory_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            instance = SimpleNamespace(name="Demo", id="instance-4", prism_source="/old/profile")
            manager = _Manager(fail=True)
            dialog = ClientExportDialog(instance, manager=manager)
            dialog._source.setText(str(source))
            dialog._remember_source.setChecked(True)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            with patch.object(QMessageBox, "information") as show:
                dialog._on_done(self._result())
            self.assertEqual(instance.prism_source, "/old/profile")
            self.assertIn("could not remember this Prism source", show.call_args.args[2])
            dialog.close()


if __name__ == "__main__":
    unittest.main()
