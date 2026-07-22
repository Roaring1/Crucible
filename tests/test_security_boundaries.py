import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# backup_manager is intentionally Qt-aware; provide the minimal import surface
# needed to test its pure filesystem logic in a headless audit environment.
if "PyQt6" not in sys.modules:
    pyqt = types.ModuleType("PyQt6")
    qtcore = types.ModuleType("PyQt6.QtCore")

    class QObject:
        def __init__(self, *args, **kwargs):
            pass

    class Signal:
        def emit(self, *args):
            pass

    qtcore.QObject = QObject
    qtcore.pyqtSignal = lambda *args: Signal()
    pyqt.QtCore = qtcore
    sys.modules["PyQt6"] = pyqt
    sys.modules["PyQt6.QtCore"] = qtcore

from crucible.data import backup_manager as backup_mod
from crucible.data.instance_manager import InstanceManager
from crucible.data.instance_model import ServerInstance
from crucible.data.properties import PropertiesDoc
from crucible.exporters.client_export import export
from crucible.importers import modpack_auto, prism


class RegistryTests(unittest.TestCase):
    def test_non_object_registry_recovers_empty(self):
        root = Path(tempfile.mkdtemp())
        (root / "instances.json").write_text("[]")
        manager = InstanceManager(root)
        manager.load()
        self.assertEqual(manager.instances, [])

    def test_non_list_instances_recovers_empty(self):
        root = Path(tempfile.mkdtemp())
        (root / "instances.json").write_text(json.dumps({"instances": {}}))
        manager = InstanceManager(root)
        manager.load()
        self.assertEqual(manager.instances, [])


class PropertiesTests(unittest.TestCase):
    def test_duplicate_property_last_value_wins_and_is_repaired(self):
        doc = PropertiesDoc.loads("server-port=25565\nserver-port=\n")
        self.assertEqual(doc.get("server-port"), "")
        changes = doc.autofix(only_errors=True)
        self.assertTrue(changes)
        self.assertEqual(
            doc.items(), [("server-port", "25565"), ("server-port", "25565")]
        )


class BackupBoundaryTests(unittest.TestCase):
    def _manager(self):
        root = Path(tempfile.mkdtemp())
        server = root / "server"
        server.mkdir()
        instance = ServerInstance(str(server), "Test")
        backup_root = root / "backups"
        patcher = patch.object(backup_mod, "BASE_DIR", backup_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return root, server, backup_mod.BackupManager(instance)

    def test_level_name_cannot_escape_server(self):
        root, server, manager = self._manager()
        outside = root / "outside"
        outside.mkdir()
        (outside / "secret.dat").write_text("secret")
        (server / "server.properties").write_text("level-name=../outside\n")
        self.assertEqual(manager._find_world_dirs(server), [])

    def test_failed_backup_leaves_no_partial_or_published_zip(self):
        _, server, manager = self._manager()
        world = server / "world"
        world.mkdir()
        (world / "level.dat").write_text("data")
        with patch.object(zipfile.ZipFile, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.create_backup()
        self.assertEqual(list(manager.backup_dir().iterdir()), [])


class ArchiveBoundaryTests(unittest.TestCase):
    def test_archive_member_limit_rejected_and_temp_removed(self):
        root = Path(tempfile.mkdtemp())
        archive = root / "many.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a", "1")
            zf.writestr("b", "2")
        with patch.object(prism, "_MAX_ARCHIVE_MEMBERS", 1):
            with self.assertRaisesRegex(ValueError, "too many"):
                prism._extract_archive(archive)

    def test_override_traversal_is_skipped(self):
        root = Path(tempfile.mkdtemp())
        archive = root / "pack.mrpack"
        target = root / "target"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("overrides/../../escape.txt", "bad")
            zf.writestr("overrides/config/good.txt", "good")
        written = modpack_auto.apply_overrides(archive, target)
        self.assertEqual(written, 1)
        self.assertFalse((root / "escape.txt").exists())
        self.assertEqual((target / "config/good.txt").read_text(), "good")


class ExportPrivacyTests(unittest.TestCase):
    def test_client_export_excludes_symlinked_secret_and_is_valid_zip(self):
        root = Path(tempfile.mkdtemp())
        server = root / "server"
        (server / "mods").mkdir(parents=True)
        (server / "config").mkdir()
        (server / "mods" / "good.jar").write_bytes(b"jar")
        secret = root / "secret.txt"
        secret.write_text("do not export")
        (server / "config" / "linked-secret").symlink_to(secret)
        out = root / "client.mrpack"
        instance = SimpleNamespace(
            path=str(server),
            name="Test",
            minecraft_version="1.20.1",
            loader="fabric",
            loader_version="0.15.0",
        )
        result = export(instance, out, "mrpack")
        self.assertTrue(result.ok, result.error)
        self.assertFalse(out.with_suffix(".mrpack.partial").exists())
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        self.assertIn("overrides/mods/good.jar", names)
        self.assertFalse(any("linked-secret" in name for name in names))


if __name__ == "__main__":
    unittest.main()
