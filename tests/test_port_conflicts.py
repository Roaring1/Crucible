import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible.data.instance_model import ServerInstance
from crucible.process.tmux_manager import TmuxManager, _listening_ipv4_addresses


class ListeningSocketParserTests(unittest.TestCase):
    def test_reads_only_listening_ipv4_sockets_for_requested_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_file = Path(tmp) / "tcp"
            # 25566 = 0x63DE. Addresses in /proc/net/tcp are little-endian.
            proc_file.write_text(
                "  sl  local_address rem_address   st\n"
                "   0: 00000000:63DE 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 1\n"
                "   1: 0100007F:63DE 0100007F:C001 01 00000000:00000000 00:00000000 00000000 0 0 2\n"
                "   2: 0100007F:63DD 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 3\n",
                encoding="ascii",
            )
            self.assertEqual(_listening_ipv4_addresses(25566, proc_file), ["0.0.0.0"])

    def test_unreadable_proc_file_is_unknown_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            self.assertEqual(_listening_ipv4_addresses(25566, missing), [])


class PortConflictGuardTests(unittest.TestCase):
    def _instance(self, root: Path, *, port="25566", bind_ip="") -> ServerInstance:
        root.mkdir(parents=True, exist_ok=True)
        (root / "server.properties").write_text(
            f"server-port={port}\nserver-ip={bind_ip}\n", encoding="utf-8"
        )
        (root / "start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        return ServerInstance(str(root), "Conflict test")

    def test_wildcard_bind_conflicts_with_any_local_ipv4_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(Path(tmp))
            tm = TmuxManager()
            with patch(
                "crucible.process.tmux_manager._listening_ipv4_addresses",
                return_value=["127.0.0.1"],
            ):
                message = tm._port_conflict_message(inst)
            self.assertIn("Port 25566", message)
            self.assertIn("127.0.0.1:25566", message)
            self.assertIn("did not start", message)

    def test_specific_bind_ignores_listener_on_unrelated_local_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(Path(tmp), bind_ip="192.0.2.55")
            tm = TmuxManager()
            with patch(
                "crucible.process.tmux_manager._listening_ipv4_addresses",
                return_value=["127.0.0.1"],
            ):
                self.assertIsNone(tm._port_conflict_message(inst))

    def test_specific_bind_conflicts_with_matching_or_wildcard_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(Path(tmp), bind_ip="192.0.2.55")
            tm = TmuxManager()
            for listener in ("0.0.0.0", "192.0.2.55"):
                with self.subTest(listener=listener), patch(
                    "crucible.process.tmux_manager._listening_ipv4_addresses",
                    return_value=[listener],
                ):
                    self.assertIsNotNone(tm._port_conflict_message(inst))

    def test_start_refuses_to_create_tmux_session_when_port_is_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(Path(tmp))
            tm = TmuxManager()
            with (
                patch.object(tm, "tmux_available", return_value=True),
                patch.object(tm, "is_running", return_value=False),
                patch.object(tm, "_port_conflict_message", return_value="Port 25566 is busy"),
                patch.object(tm, "_run") as run,
            ):
                ok, message = tm.start(inst)
            self.assertFalse(ok)
            self.assertEqual(message, "Port 25566 is busy")
            run.assert_not_called()

    def test_invalid_or_hostname_bind_config_skips_heuristic(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(Path(tmp), bind_ip="host.example.test")
            tm = TmuxManager()
            with patch(
                "crucible.process.tmux_manager._listening_ipv4_addresses",
                return_value=["0.0.0.0"],
            ):
                self.assertIsNone(tm._port_conflict_message(inst))


if __name__ == "__main__":
    unittest.main()
