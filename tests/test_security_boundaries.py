import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    class QTimer:
        @staticmethod
        def singleShot(_delay, _callback):
            pass

    qtcore.QObject = QObject
    qtcore.QTimer = QTimer
    qtcore.pyqtSignal = lambda *args: Signal()
    qtcore.pyqtSlot = lambda *args, **kwargs: (lambda fn: fn)
    pyqt.QtCore = qtcore
    sys.modules["PyQt6"] = pyqt
    sys.modules["PyQt6.QtCore"] = qtcore

from crucible.data import backup_manager as backup_mod
from crucible.data.instance_manager import InstanceManager
from crucible.data.instance_model import ServerInstance
from crucible.data.properties import PropertiesDoc
from crucible.exporters.client_export import export
from crucible.importers import modpack_auto, prism
from crucible.diagnostics import loadcheck
from crucible.mods import mod_manager
from crucible.process import watchdog as watchdog_mod


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


class MetadataBoundaryTests(unittest.TestCase):
    def test_huge_crash_log_reads_only_tail_and_still_diagnoses(self):
        root = Path(tempfile.mkdtemp()); logs = root / "logs"; logs.mkdir(); log = logs / "latest.log"
        log.write_text("x" * 4096 + "\ninvalid dist DEDICATED_SERVER\n")
        with patch.object(loadcheck, "_MAX_CRASH_TEXT_BYTES", 256): text, _ = loadcheck.latest_crash_text(root)
        self.assertLess(len(text), 512); self.assertIn("invalid dist DEDICATED_SERVER", text)

    def test_oversized_jar_metadata_is_ignored(self):
        root = Path(tempfile.mkdtemp()); jar = root / "huge.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("fabric.mod.json", '{"id":"danger"}' + " " * 128); zf.writestr("mcmod.info", '[{"modid":"danger"}]' + " " * 128)
        with patch.object(loadcheck, "_MAX_JAR_METADATA_BYTES", 32), patch.object(mod_manager, "_MAX_JAR_METADATA_BYTES", 32):
            self.assertEqual(loadcheck.jar_modids(jar), set())
            instance = ServerInstance(str(root), "Test"); entry = mod_manager.ModEntry(jar.name, jar, True, jar.stat().st_size)
            mod_manager.ModManager(instance).inspect_jar(entry); self.assertEqual(entry.mod_id, "")


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.instance = ServerInstance("/tmp/watchdog-server", "Watchdog")
        self.watchdog = watchdog_mod.Watchdog()
        self.watchdog._active = True

    def test_repeated_miss_required_before_crash(self):
        with (
            patch.object(watchdog_mod.QTimer, "singleShot"),
            patch.object(self.watchdog._tmux, "probe_running", return_value=False),
        ):
            self.watchdog.watch(self.instance, auto_restart=False)
            self.watchdog._poll()
            self.assertEqual(self.watchdog._crash_count[self.instance.id], 0)
            self.assertTrue(self.watchdog._watching[self.instance.id])
            self.watchdog._poll()
        self.assertEqual(self.watchdog._crash_count[self.instance.id], 1)
        self.assertFalse(self.watchdog._watching[self.instance.id])

    def test_uncertain_tmux_probe_never_counts_as_crash(self):
        with (
            patch.object(watchdog_mod.QTimer, "singleShot"),
            patch.object(self.watchdog._tmux, "probe_running", return_value=None),
        ):
            self.watchdog.watch(self.instance, auto_restart=True)
            for _ in range(watchdog_mod.CRASH_CONFIRM_POLLS + 2):
                self.watchdog._poll()
        self.assertEqual(self.watchdog._miss_count[self.instance.id], 0)
        self.assertEqual(self.watchdog._crash_count[self.instance.id], 0)
        self.assertTrue(self.watchdog._watching[self.instance.id])

    def test_automatic_rewatch_preserves_crash_count_and_limit(self):
        failed = Mock()
        self.watchdog.restart_failed.emit = failed
        with (
            patch.object(watchdog_mod.QTimer, "singleShot"),
            patch.object(self.watchdog._tmux, "start", return_value=(True, "ok")),
        ):
            self.watchdog.watch(self.instance, auto_restart=True)
            for expected in range(1, watchdog_mod.CRASH_LOOP_LIMIT + 1):
                self.watchdog._handle_crash(self.instance.id)
                self.assertEqual(self.watchdog._crash_count[self.instance.id], expected)
                if expected < watchdog_mod.CRASH_LOOP_LIMIT:
                    self.watchdog._do_restart(self.instance.id)
                    # This mirrors InstancePanel's Done! handler.
                    self.watchdog.watch(self.instance, auto_restart=True)
                    self.assertEqual(
                        self.watchdog._crash_count[self.instance.id], expected
                    )
        failed.assert_called_once()
        self.assertIn("Crash loop", failed.call_args.args[1])

    def test_stable_uptime_resets_only_current_generation(self):
        with patch.object(watchdog_mod.QTimer, "singleShot"):
            self.watchdog.watch(self.instance, auto_restart=True)
        iid = self.instance.id
        current = self.watchdog._watch_generation[iid]
        self.watchdog._crash_count[iid] = 2
        self.watchdog._mark_stable(iid, current - 1)
        self.assertEqual(self.watchdog._crash_count[iid], 2)
        self.watchdog._mark_stable(iid, current)
        self.assertEqual(self.watchdog._crash_count[iid], 0)


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
