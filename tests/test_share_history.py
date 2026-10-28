from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from crucible.sharing.history import (
    MAX_RECORDS,
    ShareHistoryError,
    ShareHistoryStore,
)


class ShareHistoryStoreTests(unittest.TestCase):
    def test_records_are_private_bounded_and_never_store_raw_tokens_or_exceptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "history"
            store = ShareHistoryStore(directory / "events.json")
            archive = Path(tmp) / "player-pack.zip"
            archive.write_bytes(b"synthetic zip")
            digest = "a" * 64
            fingerprint = "b" * 64
            secret = "super-secret-api-token"

            store.append(
                event="pack-prepared",
                instance_name="Example Server",
                archive_sha256=digest,
                input_fingerprint=fingerprint,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                upload_status="not-attempted",
            )
            store.append(
                event="upload-failed",
                instance_name="Example Server",
                archive_sha256=digest,
                input_fingerprint=fingerprint,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                provider="Gofile",
                upload_status="failed",
                error_code="upload-failed",
            )
            store.append(
                event="upload-succeeded",
                instance_name="Example Server",
                archive_sha256=digest,
                input_fingerprint=fingerprint,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                provider="Gofile",
                share_url="https://gofile.io/d/SyntheticId123",
                upload_status="succeeded",
                size_verified=True,
                md5_verified=False,
            )
            records = store.load()
            self.assertEqual(len(records), 3)
            self.assertEqual(records[-1]["event"], "upload-succeeded")
            self.assertEqual(records[-1]["shareUrl"], "https://gofile.io/d/SyntheticId123")
            self.assertFalse(records[-1]["md5Verified"])

            raw = (directory / "events.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            self.assertNotIn("apiToken", raw)
            self.assertNotIn("Authorization", raw)
            if os.name == "posix":
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual((directory / "events.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual(archive.read_bytes(), b"synthetic zip")

    def test_history_is_bounded_to_latest_fifty_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ShareHistoryStore(Path(tmp) / "history" / "events.json")
            for index in range(MAX_RECORDS + 12):
                store.append(
                    event="pack-prepared",
                    instance_name=f"Server {index}",
                    upload_status="not-attempted",
                )
            records = store.load()
            self.assertEqual(len(records), MAX_RECORDS)
            self.assertEqual(records[-1]["instanceName"], f"Server {MAX_RECORDS + 11}")
            self.assertEqual(records[0]["instanceName"], "Server 12")

    def test_clear_forgets_records_but_never_deletes_local_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "still-here.zip"
            archive.write_bytes(b"keep this")
            store = ShareHistoryStore(root / "history" / "events.json")
            store.append(
                event="pack-prepared",
                instance_name="Server",
                local_path=archive,
                size_bytes=archive.stat().st_size,
                upload_status="not-attempted",
            )
            store.clear()
            self.assertEqual(store.load(), [])
            self.assertEqual(archive.read_bytes(), b"keep this")

    def test_non_string_event_arguments_fail_with_controlled_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ShareHistoryStore(Path(tmp) / "history" / "events.json")
            with self.assertRaises(ShareHistoryError):
                store.append(event=["bad"], instance_name="Server")
            with self.assertRaises(ShareHistoryError):
                store.append(event="upload-failed", instance_name="Server", error_code=["bad"])

    def test_rejects_non_gofile_links_and_invalid_event_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ShareHistoryStore(Path(tmp) / "history" / "events.json")
            with self.assertRaises(ShareHistoryError):
                store.append(
                    event="upload-succeeded",
                    instance_name="Server",
                    provider="Gofile",
                    share_url="https://gofile.io.evil.example/d/AbCd",
                    upload_status="succeeded",
                )
            with self.assertRaises(ShareHistoryError):
                store.append(event="made-up-event", instance_name="Server")

    def test_symlink_history_file_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text('{"schemaVersion":1,"records":[]}', encoding="utf-8")
            history = root / "history.json"
            try:
                history.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            store = ShareHistoryStore(history)
            with self.assertRaises(ShareHistoryError):
                store.append(event="pack-prepared", instance_name="Server")
            self.assertEqual(target.read_text(encoding="utf-8"), '{"schemaVersion":1,"records":[]}')

    def test_unhashable_schema_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history" / "events.json"
            store = ShareHistoryStore(path)
            store.append(event="pack-prepared", instance_name="Server")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"][0]["event"] = ["not", "a", "valid", "event"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ShareHistoryError):
                store.load()

    def test_unknown_fields_are_dropped_on_next_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history" / "events.json"
            store = ShareHistoryStore(path)
            store.append(event="pack-prepared", instance_name="Server")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"][0]["apiToken"] = "must-not-survive"
            path.write_text(json.dumps(payload), encoding="utf-8")

            store.append(event="upload-failed", instance_name="Server", error_code="upload-failed")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("apiToken", raw)
            self.assertNotIn("must-not-survive", raw)


if __name__ == "__main__":
    unittest.main()
