import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from crucible.data.instance_model import ServerInstance
from crucible.process.crash_recovery import (
    CRASHED_HANDLED,
    RUNNING,
    STOPPED_CLEAN,
    HeartbeatStore,
    detect_torn_log,
    reconcile,
)


def _make_instance(tmp_root: Path, name: str = "Main") -> ServerInstance:
    path = tmp_root / name
    path.mkdir(parents=True, exist_ok=True)
    return ServerInstance(str(path), name, id=f"{name}-id", tmux_session=name)


class HeartbeatStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self.store = HeartbeatStore(self._dir)

    def test_mark_running_then_read(self):
        self.store.mark_running("inst-1", "MySession", boot_id="boot-a")
        data = self.store.read("inst-1")
        self.assertEqual(data["state"], RUNNING)
        self.assertEqual(data["boot_id"], "boot-a")
        self.assertEqual(data["tmux_session"], "MySession")

    def test_mark_stopped_clean_preserves_boot_id(self):
        self.store.mark_running("inst-1", "MySession", boot_id="boot-a")
        self.store.mark_stopped_clean("inst-1")
        data = self.store.read("inst-1")
        self.assertEqual(data["state"], STOPPED_CLEAN)
        self.assertEqual(data["boot_id"], "boot-a")

    def test_mark_crashed_handled(self):
        self.store.mark_running("inst-1", "MySession", boot_id="boot-a")
        self.store.mark_crashed_handled("inst-1")
        self.assertEqual(self.store.read("inst-1")["state"], CRASHED_HANDLED)

    def test_read_missing_returns_none(self):
        self.assertIsNone(self.store.read("nope"))

    def test_read_corrupt_file_returns_none(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.read("broken"))

    def test_write_is_atomic_no_stray_temp_files(self):
        self.store.mark_running("inst-1", "MySession", boot_id="boot-a")
        leftovers = [p for p in self._dir.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp_root = Path(tempfile.mkdtemp())
        self.state_dir = Path(tempfile.mkdtemp())
        self.store = HeartbeatStore(self.state_dir)

    def _tmux(self, running: bool) -> MagicMock:
        tmux = MagicMock()
        tmux.is_running.return_value = running
        return tmux

    def test_detects_host_crash_when_boot_id_changed_and_session_gone(self):
        instance = _make_instance(self.tmp_root)
        self.store.mark_running(instance.id, instance.tmux_session, boot_id="boot-old")

        reports = reconcile(
            [instance],
            self._tmux(running=False),
            store=self.store,
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].instance_id, instance.id)
        # Must be marked handled so a later launch does not re-report it.
        self.assertEqual(self.store.read(instance.id)["state"], CRASHED_HANDLED)

    def test_no_report_when_boot_id_unchanged(self):
        instance = _make_instance(self.tmp_root)
        self.store.mark_running(instance.id, instance.tmux_session, boot_id="boot-same")

        import crucible.process.crash_recovery as cr
        orig = cr.get_boot_id
        cr.get_boot_id = lambda: "boot-same"
        try:
            reports = reconcile([instance], self._tmux(running=False), store=self.store)
        finally:
            cr.get_boot_id = orig
        self.assertEqual(reports, [])

    def test_no_report_when_session_still_running(self):
        instance = _make_instance(self.tmp_root)
        self.store.mark_running(instance.id, instance.tmux_session, boot_id="boot-old")

        reports = reconcile([instance], self._tmux(running=True), store=self.store)
        self.assertEqual(reports, [])
        # Not a crash -- heartbeat state must be left untouched.
        self.assertEqual(self.store.read(instance.id)["state"], RUNNING)

    def test_no_report_when_stopped_clean(self):
        instance = _make_instance(self.tmp_root)
        self.store.mark_running(instance.id, instance.tmux_session, boot_id="boot-old")
        self.store.mark_stopped_clean(instance.id)

        reports = reconcile([instance], self._tmux(running=False), store=self.store)
        self.assertEqual(reports, [])

    def test_no_report_when_crash_already_witnessed_live(self):
        instance = _make_instance(self.tmp_root)
        self.store.mark_running(instance.id, instance.tmux_session, boot_id="boot-old")
        self.store.mark_crashed_handled(instance.id)

        reports = reconcile([instance], self._tmux(running=False), store=self.store)
        self.assertEqual(reports, [])

    def test_no_report_when_no_heartbeat_ever_recorded(self):
        instance = _make_instance(self.tmp_root)
        reports = reconcile([instance], self._tmux(running=False), store=self.store)
        self.assertEqual(reports, [])


class TornLogDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp_root = Path(tempfile.mkdtemp())

    def test_null_padded_tail_without_shutdown_line_is_flagged(self):
        instance = _make_instance(self.tmp_root)
        logs = Path(instance.path) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "fml-server-latest.log"
        content = b"[INFO] Gathering id map for writing to world save\n" + b"\x00" * 4096
        log_path.write_bytes(content)

        evidence = detect_torn_log(instance)
        self.assertIsNotNone(evidence)

    def test_graceful_shutdown_line_is_not_flagged(self):
        instance = _make_instance(self.tmp_root)
        logs = Path(instance.path) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "fml-server-latest.log"
        log_path.write_bytes(b"[INFO] Stopping the server\n[INFO] Saving worlds\n")

        self.assertIsNone(detect_torn_log(instance))

    def test_missing_log_is_not_flagged(self):
        instance = _make_instance(self.tmp_root)
        self.assertIsNone(detect_torn_log(instance))


if __name__ == "__main__":
    unittest.main()
