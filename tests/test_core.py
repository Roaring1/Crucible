import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from crucible.data.instance_manager import (
    InstanceManager, validate_delete_target, validate_session_name,
)
from crucible.data.instance_model import ServerInstance
from crucible.importers.downloader import DownloadItem, _safe_destination, _download_one
from crucible.importers.prism import _extract_archive
from crucible.process.tmux_manager import TmuxManager
from crucible.process.command_intent import lifecycle_intent


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




class CommandIntentTests(unittest.TestCase):
    def test_exact_stop_is_lifecycle_intent(self):
        for command in ("stop", " STOP ", "/stop", " / stop "):
            self.assertEqual(lifecycle_intent(command), "stop")

    def test_stop_substrings_are_not_lifecycle_intent(self):
        for command in ("say stop", "stopsound @a", "stop now", "stopserver", ""):
            self.assertIsNone(lifecycle_intent(command), command)


class RegistryTransactionTests(unittest.TestCase):
    def _manager_with_instance(self):
        root = Path(tempfile.mkdtemp())
        server = root / "server"
        server.mkdir()
        manager = InstanceManager(root / "cfg")
        instance = manager.add_instance(str(server), "Server")
        return root, manager, instance

    def test_failed_add_does_not_create_memory_only_row(self):
        root, manager, _ = self._manager_with_instance()
        other = root / "other"
        other.mkdir()
        before = list(manager.instances)
        with patch.object(manager, "_write_instances", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.add_instance(str(other), "Other")
        self.assertEqual(manager.instances, before)

    def test_failed_remove_keeps_registry_row_in_memory(self):
        _, manager, instance = self._manager_with_instance()
        with patch.object(manager, "_write_instances", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                manager.remove_instance(instance.id)
        self.assertEqual([row.id for row in manager.instances], [instance.id])

    def test_malformed_registry_blocks_destructive_overwrite(self):
        root = Path(tempfile.mkdtemp())
        cfg = root / "cfg"
        cfg.mkdir()
        registry = cfg / "instances.json"
        registry.write_text('{"instances": [{"name": "missing path"}]}')
        manager = InstanceManager(cfg)
        manager.load()
        self.assertIsNotNone(manager.load_error)
        with self.assertRaisesRegex(RuntimeError, "writes are disabled"):
            manager.save()
        self.assertIn("missing path", registry.read_text())

    def test_external_registry_change_is_detected(self):
        _, manager, _ = self._manager_with_instance()
        self.assertFalse(manager.registry_changed_externally())
        manager.registry_file.write_text('{"version": 1, "instances": []}')
        self.assertTrue(manager.registry_changed_externally())


class DeleteTargetTests(unittest.TestCase):
    def test_delete_guard_rejects_broad_and_symlink_paths(self):
        root = Path(tempfile.mkdtemp()); home = root / "home" / "person"; server = home / "servers" / "one"; server.mkdir(parents=True)
        self.assertEqual(validate_delete_target(str(server), home=home), server.resolve())
        for bad in (Path("/"), home, home.parent):
            with self.assertRaises(ValueError): validate_delete_target(str(bad), home=home)
        link = home / "server-link"; link.symlink_to(server, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"): validate_delete_target(str(link), home=home)


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


class TmuxCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.inst = ServerInstance(str(self.root), "GTNH", tmux_session="gtnh")
        self.tm = TmuxManager()

    def test_status_uses_exact_tmux_target(self):
        import subprocess
        with patch.object(
            self.tm, "_run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            self.assertTrue(self.tm.is_running(self.inst))
        self.assertEqual(run.call_args.args[0], ["tmux", "has-session", "-t", "=gtnh"])

    def test_console_input_is_literal_then_enter(self):
        import subprocess
        calls = []

        def fake(cmd, *args, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(self.tm, "_run", side_effect=fake):
            self.assertTrue(self.tm.send_command(self.inst, "say Space C-c ; $(no-shell)"))
        self.assertEqual(
            calls[0],
            ["tmux", "send-keys", "-t", "=gtnh", "-l", "--", "say Space C-c ; $(no-shell)"],
        )
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "=gtnh", "Enter"])

    def test_removal_probe_blocks_running_session_and_timeout(self):
        import subprocess
        with patch.object(
            self.tm, "_run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            safe, reason = self.tm.safe_to_remove(self.inst)
        self.assertFalse(safe)
        self.assertIn("running", reason)
        with patch.object(
            self.tm, "_run",
            return_value=subprocess.CompletedProcess([], 1, "", "timeout"),
        ):
            safe, reason = self.tm.safe_to_remove(self.inst)
        self.assertFalse(safe)
        self.assertIn("timed out", reason)

    def test_removal_probe_allows_confirmed_absent_session(self):
        import subprocess
        with patch.object(
            self.tm, "_run",
            return_value=subprocess.CompletedProcess([], 1, "", "can't find session"),
        ):
            safe, _ = self.tm.safe_to_remove(self.inst)
        self.assertTrue(safe)

    def test_missing_directory_is_reported_when_no_session_exists(self):
        import subprocess
        missing = ServerInstance(str(self.root / "gone"), "Gone", tmux_session="gone")
        with (
            patch.object(self.tm, "tmux_available", return_value=True),
            patch.object(self.tm, "list_sessions", return_value=[]),
        ):
            self.assertEqual(self.tm.status_map([missing])[missing.id], "missing")

    def test_running_session_remains_controllable_when_files_are_missing(self):
        missing = ServerInstance(str(self.root / "gone"), "Gone", tmux_session="gone")
        with (
            patch.object(self.tm, "tmux_available", return_value=True),
            patch.object(self.tm, "list_sessions", return_value=["gone"]),
        ):
            self.assertEqual(self.tm.status_map([missing])[missing.id], "running")

    def test_konsole_attach_uses_separate_argv(self):
        with (
            patch.object(self.tm, "is_running", return_value=True),
            patch("crucible.process.tmux_manager.subprocess.Popen") as popen,
        ):
            ok, _ = self.tm.attach(self.inst, terminal="konsole")
        self.assertTrue(ok)
        self.assertEqual(
            popen.call_args.args[0],
            ["konsole", "-e", "tmux", "attach", "-t", "=gtnh"],
        )

    def test_status_map_matches_complete_names_only(self):
        other = ServerInstance(str(self.root / "b"), "Other", tmux_session="gtnh-old")
        with (
            patch.object(self.tm, "tmux_available", return_value=True),
            patch.object(self.tm, "list_sessions", return_value=["gtnh-old"]),
        ):
            result = self.tm.status_map([self.inst, other])
        self.assertEqual(result[self.inst.id], "stopped")
        self.assertEqual(result[other.id], "running")


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
