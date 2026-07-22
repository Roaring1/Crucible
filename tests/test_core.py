import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from crucible.data.instance_manager import InstanceManager, validate_session_name
from crucible.data.instance_model import ServerInstance
from crucible.importers.downloader import DownloadItem, _safe_destination, _download_one
from crucible.importers.prism import _extract_archive
from crucible.process.tmux_manager import TmuxManager


class IdentityTests(unittest.TestCase):
    def test_ambiguous_prefix_never_selects_wrong_server(self):
        m = InstanceManager(Path(tempfile.mkdtemp()))
        m.instances = [
            ServerInstance("/tmp/a", "a", id="abc111"),
            ServerInstance("/tmp/b", "b", id="abc222"),
        ]
        self.assertIsNone(m.get_by_id("abc"))
        self.assertEqual(m.get_by_id("abc1").id, "abc111")

    def test_session_names_are_path_safe(self):
        for bad in ("../x", "a/b", "", ".", "..", "x y"):
            with self.assertRaises(ValueError):
                validate_session_name(bad)
        self.assertEqual(validate_session_name("GTNH-1.2_ok"), "GTNH-1.2_ok")

    def test_duplicate_session_rejected(self):
        root = Path(tempfile.mkdtemp())
        a = root / "a"
        b = root / "b"
        a.mkdir()
        b.mkdir()
        m = InstanceManager(root / "cfg")
        m.add_instance(str(a), "same")
        with self.assertRaisesRegex(ValueError, "already used"):
            m.add_instance(str(b), "same")


class DownloadSafetyTests(unittest.TestCase):
    def test_traversal_and_symlink_escape_rejected(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.assertIsNone(_safe_destination(root, "../../escape.jar"))
        self.assertEqual(_safe_destination(root, "mods/ok.jar"), root / "mods/ok.jar")
        outside = Path(tempfile.mkdtemp())
        (root / "link").symlink_to(outside, target_is_directory=True)
        self.assertIsNone(_safe_destination(root, "link/escape.jar"))

    def test_non_https_url_rejected(self):
        item = DownloadItem("x", "mods/x", urls=["file:///etc/passwd"])
        with self.assertRaisesRegex(RuntimeError, "safe HTTPS"):
            _download_one(item, Path(tempfile.mkdtemp()) / "x", None, 1, None, None)


class ArchiveSafetyTests(unittest.TestCase):
    def test_zip_slip_is_skipped(self):
        d = Path(tempfile.mkdtemp())
        z = d / "evil.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("../../escape.txt", "bad")
            f.writestr("mods/good.jar", "ok")
        out, warnings = _extract_archive(z)
        self.assertFalse((d.parent / "escape.txt").exists())
        self.assertTrue((out / "mods/good.jar").exists())
        self.assertTrue(any("unsafe" in x for x in warnings))


class StartTests(unittest.TestCase):
    def test_start_wrapper_has_no_unbounded_tee(self):
        d = Path(tempfile.mkdtemp())
        (d / "start.sh").write_text("sleep 99")
        inst = ServerInstance(str(d), "safe")
        tm = TmuxManager()
        calls = []

        def fake(cmd, *a, **k):
            calls.append(cmd)
            import subprocess

            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            patch.object(tm, "tmux_available", return_value=True),
            patch.object(tm, "is_running", return_value=False),
            patch.object(tm, "_run", side_effect=fake),
            patch.object(tm, "_verify_started", return_value=(True, "")),
        ):
            ok, _ = tm.start(inst)
        self.assertTrue(ok)
        command = calls[-1][-1]
        self.assertNotIn("tee ", command)
        self.assertIn("_crucible_code", command)


if __name__ == "__main__":
    unittest.main()
