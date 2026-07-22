"""
tests/test_reset_wipe_and_memory.py

Regression tests for the World tab's Reset/Wipe operations
(BackupManager.reset_world / wipe_world) and the Setup tab's suggested
-Xms/-Xmx memory heuristic (setup_tab._suggest_memory_mb).

Both modules are Qt-aware but the logic under test here is pure filesystem /
arithmetic, so we use the same minimal PyQt6 shim pattern already used by
tests/test_world_backup_swap.py and tests/test_security_boundaries.py to
keep this headless and fast.
"""
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "PyQt6" not in sys.modules:
    pyqt = types.ModuleType("PyQt6")

    qtcore = types.ModuleType("PyQt6.QtCore")

    class QObject:
        def __init__(self, *args, **kwargs):
            pass

    class _Signal:
        def emit(self, *args):
            pass

    class Qt:
        class AlignmentFlag:
            AlignRight = 1
            AlignLeft = 2
            AlignVCenter = 4

    class QUrl:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def fromLocalFile(path):
            return QUrl()

    class QTimer:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def singleShot(_delay, _callback):
            pass

    class QThread:
        def __init__(self, *args, **kwargs):
            pass

    def pyqtSignal(*args, **kwargs):
        return _Signal()

    def pyqtSlot(*args, **kwargs):
        def _decorator(fn):
            return fn
        return _decorator

    qtcore.Qt = Qt
    qtcore.QUrl = QUrl
    qtcore.QTimer = QTimer
    qtcore.QObject = QObject
    qtcore.QThread = QThread
    qtcore.pyqtSignal = pyqtSignal
    qtcore.pyqtSlot = pyqtSlot

    qtgui = types.ModuleType("PyQt6.QtGui")

    class QDesktopServices:
        @staticmethod
        def openUrl(*args, **kwargs):
            return True

    qtgui.QDesktopServices = QDesktopServices

    qtwidgets = types.ModuleType("PyQt6.QtWidgets")

    class _Widget:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, _name):
            # Any unstubbed method call (setStyleSheet, addWidget, etc.) is a
            # no-op that returns another no-op-callable, since none of the
            # pure-logic tests below actually build a live widget tree.
            def _noop(*args, **kwargs):
                return None
            return _noop

    for _name in (
        "QWidget", "QVBoxLayout", "QHBoxLayout", "QGridLayout", "QLabel",
        "QPushButton", "QFrame", "QScrollArea", "QApplication", "QMessageBox",
        "QSpinBox", "QComboBox",
    ):
        setattr(qtwidgets, _name, type(_name, (_Widget,), {}))

    pyqt.QtCore = qtcore
    pyqt.QtGui = qtgui
    pyqt.QtWidgets = qtwidgets
    sys.modules["PyQt6"] = pyqt
    sys.modules["PyQt6.QtCore"] = qtcore
    sys.modules["PyQt6.QtGui"] = qtgui
    sys.modules["PyQt6.QtWidgets"] = qtwidgets

from crucible.data import backup_manager as backup_mod
from crucible.data.backup_manager import BackupManager
from crucible.data.instance_model import ServerInstance

# Import setup_tab directly without triggering crucible/ui/tabs/__init__.py,
# which eagerly imports every other tab module (console_tab, world_tab,
# etc.) and would require a much larger PyQt6 shim than this pure-logic test
# needs. We register a lightweight stand-in package in sys.modules first so
# Python's import machinery resolves "crucible.ui.tabs.setup_tab" against
# the real file on disk without ever executing the real package __init__.
import importlib
import crucible.ui as _crucible_ui

_tabs_pkg_name = "crucible.ui.tabs"
if _tabs_pkg_name not in sys.modules:
    _tabs_pkg = types.ModuleType(_tabs_pkg_name)
    _tabs_pkg.__path__ = [str(Path(_crucible_ui.__file__).parent / "tabs")]
    _tabs_pkg.__package__ = _tabs_pkg_name
    sys.modules[_tabs_pkg_name] = _tabs_pkg

_setup_tab = importlib.import_module("crucible.ui.tabs.setup_tab")
_suggest_memory_mb = _setup_tab._suggest_memory_mb


def _make_server(tmp_root: Path, level_name: str = "World", with_dims: bool = True) -> Path:
    server_dir = tmp_root / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "server.properties").write_text(
        f"level-name={level_name}\nserver-port=25565\n"
    )
    world = server_dir / level_name
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"LVL" * 40)
    if with_dims:
        dd = world / "DIM1" / "region"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "r.0.0.mca").write_bytes(b"R" * 30)
    return server_dir


def _manager(tmp_root: Path, server_dir: Path, name: str, backups_subdir: str) -> BackupManager:
    inst = ServerInstance(str(server_dir), name)
    backup_mod.BASE_DIR = tmp_root / backups_subdir
    return BackupManager(inst)


class ResetWorldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="crucible-reset-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.tmp_root = Path(self._tmp)

    def test_reset_world_takes_safety_backup_and_renames_aside(self):
        server_dir = _make_server(self.tmp_root)
        mgr = _manager(self.tmp_root, server_dir, "reset-test", "backups")

        world_root = mgr._instance.world_root_path()
        self.assertTrue(world_root.is_dir())

        result = mgr.reset_world()

        self.assertTrue(result.ok)
        self.assertFalse(world_root.is_dir())
        self.assertIsNotNone(result.pre_swap_backup_path)
        self.assertTrue(result.pre_swap_backup_path.is_file())
        self.assertIsNotNone(result.pre_swap_dir)
        self.assertTrue(result.pre_swap_dir.is_dir())
        self.assertIn(result.pre_swap_dir, mgr.list_pre_swap_dirs())

    def test_reset_world_on_never_started_server_has_no_backup(self):
        server_dir = _make_server(self.tmp_root, with_dims=False)
        world_root = server_dir / "World"
        shutil.rmtree(world_root)
        mgr = _manager(self.tmp_root, server_dir, "reset-empty-test", "backups2")

        result = mgr.reset_world()

        self.assertTrue(result.ok)
        self.assertIsNone(result.pre_swap_backup_path)
        self.assertIsNone(result.pre_swap_dir)

    def test_reset_world_reports_progress(self):
        server_dir = _make_server(self.tmp_root)
        mgr = _manager(self.tmp_root, server_dir, "reset-progress-test", "backups3")
        seen = []
        mgr.reset_world(progress_cb=seen.append)
        self.assertIn(100, seen)


class WipeWorldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="crucible-wipe-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.tmp_root = Path(self._tmp)

    def test_wipe_world_permanently_deletes_with_no_backup(self):
        server_dir = _make_server(self.tmp_root)
        mgr = _manager(self.tmp_root, server_dir, "wipe-test", "backups")
        world_root = mgr._instance.world_root_path()
        self.assertTrue(world_root.is_dir())

        result = mgr.wipe_world()

        self.assertTrue(result.ok)
        self.assertFalse(world_root.exists())
        self.assertEqual(len(mgr.list_backups()), 0)
        self.assertEqual(mgr.list_pre_swap_dirs(), [])

    def test_wipe_world_missing_folder_is_a_harmless_no_op(self):
        server_dir = _make_server(self.tmp_root, with_dims=False)
        world_root = server_dir / "World"
        shutil.rmtree(world_root)
        mgr = _manager(self.tmp_root, server_dir, "wipe-empty-test", "backups2")

        result = mgr.wipe_world()

        self.assertTrue(result.ok)
        self.assertIn("No world folder", result.message)


class SuggestedMemoryHeuristicTests(unittest.TestCase):
    def test_none_or_unknown_total_falls_back_to_a_safe_default(self):
        self.assertEqual(_suggest_memory_mb(None), 4096)
        self.assertEqual(_suggest_memory_mb(0), 4096)

    def test_suggestion_never_exceeds_total_minus_reserve(self):
        total = 8192
        suggested = _suggest_memory_mb(total)
        reserve = max(2048, total * 0.25)
        self.assertLessEqual(suggested, total - reserve + 1)

    def test_suggestion_is_rounded_to_nearest_512_mb(self):
        for total in (4096, 8192, 16384, 32768, 65536):
            suggested = _suggest_memory_mb(total)
            self.assertEqual(suggested % 512, 0)

    def test_suggestion_is_clamped_between_1gb_and_16gb(self):
        self.assertGreaterEqual(_suggest_memory_mb(1024), 1024)
        self.assertLessEqual(_suggest_memory_mb(131072), 16384)

    def test_more_total_ram_never_suggests_less_memory(self):
        low = _suggest_memory_mb(4096)
        high = _suggest_memory_mb(16384)
        self.assertGreaterEqual(high, low)


if __name__ == "__main__":
    unittest.main()
