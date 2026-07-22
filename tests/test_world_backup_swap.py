import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

# backup_manager is intentionally Qt-aware; provide the minimal import surface
# needed to test its pure filesystem logic in a headless environment (same
# shim pattern as tests/test_security_boundaries.py).
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
from crucible.data.backup_manager import BackupManager, SwapResult
from crucible.data.instance_model import (
    ServerInstance, dimension_label, is_dimension_dir_name,
)


def _make_server(tmp_root: Path, level_name: str = "World", with_dims: bool = True) -> Path:
    server_dir = tmp_root / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "server.properties").write_text(f"level-name={level_name}\nserver-port=25565\n")
    world = server_dir / level_name
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"LVL" * 40)
    if with_dims:
        for d in ("DIM1", "DIM-1"):
            dd = world / d / "region"
            dd.mkdir(parents=True, exist_ok=True)
            (dd / "r.0.0.mca").write_bytes(b"R" * 30)
    return server_dir


def _manager(tmp_root: Path, server_dir: Path, name: str, backups_subdir: str) -> BackupManager:
    inst = ServerInstance(str(server_dir), name)
    backup_mod.BASE_DIR = tmp_root / backups_subdir
    return BackupManager(inst)


class WorldIdentificationTests(unittest.TestCase):
    def test_is_dimension_dir_name(self):
        self.assertTrue(is_dimension_dir_name("DIM1"))
        self.assertTrue(is_dimension_dir_name("DIM-1"))
        self.assertTrue(is_dimension_dir_name("dim7"))
        self.assertFalse(is_dimension_dir_name("DIMENSION"))
        self.assertFalse(is_dimension_dir_name("world"))
        self.assertFalse(is_dimension_dir_name("DIM"))
        self.assertFalse(is_dimension_dir_name("DIM-"))

    def test_dimension_label_known_and_unknown(self):
        self.assertEqual(dimension_label("DIM1"), "The End")
        self.assertEqual(dimension_label("DIM-1"), "The Nether")
        self.assertIn("Custom dimension", dimension_label("DIM7"))

    def test_world_root_path_reads_level_name(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp, level_name="MyWorld")
        inst = ServerInstance(str(server_dir), "srv")
        self.assertEqual(inst.world_root_path(), server_dir / "MyWorld")

    def test_world_root_path_defaults_when_properties_missing(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = tmp / "empty_server"
        server_dir.mkdir()
        inst = ServerInstance(str(server_dir), "srv")
        self.assertEqual(inst.world_root_path(), server_dir / "world")
        self.assertEqual(inst.world_dimension_dirs(), [])
        self.assertEqual(inst.world_size_bytes(), 0)

    def test_world_dimension_dirs_and_size(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp, level_name="World")
        inst = ServerInstance(str(server_dir), "srv")
        dims = inst.world_dimension_dirs()
        self.assertEqual(sorted(d.name for d in dims), ["DIM-1", "DIM1"])
        # level.dat (120 bytes) + 2 regions * 30 bytes
        self.assertEqual(inst.world_size_bytes(), 120 + 30 + 30)


class NamedSlotAndPruneTests(unittest.TestCase):
    def test_named_backup_survives_prune_unnamed_does_not(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp)
        mgr = _manager(tmp, server_dir, "TestSrv", "backups1")

        mgr.create_backup(slot_name="Checkpoint A")
        mgr.create_backup()
        mgr.create_backup()

        entries = mgr.list_backups()
        self.assertEqual(len(entries), 3)
        named = [e for e in entries if e.slot_name]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0].slot_name, "Checkpoint A")

        deleted = mgr.prune_old(keep_count=1)
        self.assertEqual(deleted, 1)
        remaining = mgr.list_backups()
        self.assertEqual(len(remaining), 2)
        self.assertEqual(sum(1 for e in remaining if e.slot_name), 1)

    def test_rename_slot_sets_and_clears(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp)
        mgr = _manager(tmp, server_dir, "TestSrv", "backups2")
        mgr.create_backup()
        entry = mgr.list_backups()[0]
        self.assertIsNone(entry.slot_name)

        mgr.rename_slot(entry, "Renamed")
        self.assertEqual(mgr.list_backups()[0].slot_name, "Renamed")

        mgr.rename_slot(entry, "")
        self.assertIsNone(mgr.list_backups()[0].slot_name)

    def test_delete_backup_removes_sidecar(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp)
        mgr = _manager(tmp, server_dir, "TestSrv", "backups3")
        mgr.create_backup(slot_name="X")
        entry = mgr.list_backups()[0]
        sidecar = mgr._metadata_path(entry.path)
        self.assertTrue(sidecar.exists())

        mgr.delete_backup(entry)
        self.assertFalse(entry.path.exists())
        self.assertFalse(sidecar.exists())

    def test_unnamed_backups_have_no_sidecar_and_stay_backward_compatible(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp)
        mgr = _manager(tmp, server_dir, "TestSrv", "backups4")
        path = mgr.create_backup()
        sidecar = mgr._metadata_path(path)
        self.assertFalse(sidecar.exists())
        entry = mgr.list_backups()[0]
        self.assertIsNone(entry.slot_name)
        self.assertEqual(entry.display_name, entry.filename)


class BackupIntegrityTests(unittest.TestCase):
    def test_missing_level_dat_in_archive_raises_and_leaves_no_backup(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp)
        mgr = _manager(tmp, server_dir, "TestSrv", "backups5")

        orig = BackupManager.__dict__["_verify_level_dat_present"]

        def boom(zf, all_files):
            raise OSError("simulated corruption")

        BackupManager._verify_level_dat_present = staticmethod(boom)
        try:
            with self.assertRaises(OSError):
                mgr.create_backup()
        finally:
            BackupManager._verify_level_dat_present = orig

        self.assertEqual(mgr.list_backups(), [])
        self.assertEqual(list(mgr.backup_dir().glob("*.zip.partial")), [])


class WorldSwapTests(unittest.TestCase):
    def test_swap_replaces_world_and_creates_safety_backup(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp, level_name="World")
        mgr = _manager(tmp, server_dir, "TestSrv", "backups6")
        inst = mgr._instance

        original_backup = mgr.create_backup(slot_name="Original")

        world_root = inst.world_root_path()
        (world_root / "level.dat").write_bytes(b"MUTATED" * 10)
        (world_root / "junk.txt").write_text("should disappear after swap")
        shutil.rmtree(world_root / "DIM-1")

        entry = next(e for e in mgr.list_backups() if e.slot_name == "Original")
        result = mgr.swap_world(entry)

        self.assertIsInstance(result, SwapResult)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.pre_swap_backup_path)
        self.assertIsNotNone(result.pre_swap_dir)
        self.assertTrue(result.pre_swap_dir.exists())

        self.assertFalse((world_root / "junk.txt").exists())
        self.assertTrue((world_root / "DIM-1" / "region" / "r.0.0.mca").exists())
        self.assertEqual((world_root / "level.dat").read_bytes(), b"LVL" * 40)

        all_entries = mgr.list_backups()
        presafety = [e for e in all_entries if e.slot_name and e.slot_name.startswith("Pre-swap safety backup")]
        self.assertEqual(len(presafety), 1)

        self.assertTrue((result.pre_swap_dir / "junk.txt").exists())

    def test_prune_pre_swap_dirs_removes_old_ones(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp, level_name="World")
        mgr = _manager(tmp, server_dir, "TestSrv", "backups7")
        inst = mgr._instance

        mgr.create_backup(slot_name="Original")
        entry = next(e for e in mgr.list_backups() if e.slot_name == "Original")
        result = mgr.swap_world(entry)

        self.assertEqual(mgr.prune_pre_swap_dirs(keep_days=999), 0)
        deleted = mgr.prune_pre_swap_dirs(keep_days=0)
        self.assertEqual(deleted, 1)
        self.assertFalse(result.pre_swap_dir.exists())
        self.assertEqual(mgr.list_pre_swap_dirs(), [])

    def test_swap_refuses_mismatched_level_name(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp, level_name="World")
        mgr = _manager(tmp, server_dir, "TestSrv", "backups8")

        mgr.create_backup(slot_name="WorldBackup")
        (server_dir / "server.properties").write_text("level-name=DifferentWorld\nserver-port=25565\n")

        entry = next(e for e in mgr.list_backups() if e.slot_name == "WorldBackup")
        with self.assertRaisesRegex(ValueError, "level-name"):
            mgr.swap_world(entry)

        # Confirm nothing was touched: the (now-orphaned) old world folder
        # must be untouched since we refused before making any changes.
        self.assertTrue((server_dir / "World" / "level.dat").exists())

    def test_swap_rolls_back_on_verification_failure(self):
        tmp = Path(tempfile.mkdtemp())
        server_dir = _make_server(tmp, level_name="World")
        mgr = _manager(tmp, server_dir, "TestSrv", "backups9")
        inst = mgr._instance

        mgr.create_backup(slot_name="Good")
        world_root = inst.world_root_path()
        (world_root / "marker.txt").write_text("pre-swap original content")

        entry = next(e for e in mgr.list_backups() if e.slot_name == "Good")

        orig_verify = BackupManager.__dict__["_verify_swapped_world"]

        def boom(self, world_root, entry):
            raise OSError("simulated verification failure")

        BackupManager._verify_swapped_world = boom
        try:
            with self.assertRaises(OSError):
                mgr.swap_world(entry)
        finally:
            BackupManager._verify_swapped_world = orig_verify

        self.assertTrue((world_root / "marker.txt").exists())
        self.assertEqual((world_root / "marker.txt").read_text(), "pre-swap original content")
        self.assertEqual(mgr.list_pre_swap_dirs(), [])

    def test_swap_into_never_started_server_has_no_safety_backup(self):
        tmp = Path(tempfile.mkdtemp())

        src_server = tmp / "src"
        src_world = src_server / "World"
        src_world.mkdir(parents=True)
        (src_world / "level.dat").write_bytes(b"L" * 20)
        (src_server / "server.properties").write_text("level-name=World\n")
        src_mgr = _manager(tmp, src_server, "SrcSrv", "backups10")
        backup_path = src_mgr.create_backup(slot_name="FreshWorld")

        dest_server = tmp / "dest"
        dest_server.mkdir(parents=True)
        (dest_server / "server.properties").write_text("level-name=World\n")
        dest_inst = ServerInstance(str(dest_server), "DestSrv")
        dest_mgr = BackupManager(dest_inst)
        shutil.copy(backup_path, dest_mgr.backup_dir() / backup_path.name)
        sidecar = src_mgr._metadata_path(backup_path)
        if sidecar.exists():
            shutil.copy(sidecar, dest_mgr.backup_dir() / sidecar.name)

        entry = next(e for e in dest_mgr.list_backups() if e.slot_name == "FreshWorld")
        self.assertFalse(dest_inst.world_root_path().exists())

        result = dest_mgr.swap_world(entry)
        self.assertTrue(result.ok)
        self.assertIsNone(result.pre_swap_backup_path)
        self.assertIsNone(result.pre_swap_dir)
        self.assertTrue((dest_inst.world_root_path() / "level.dat").exists())


if __name__ == "__main__":
    unittest.main()
