from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import zipfile

from PyQt6.QtWidgets import QApplication, QMessageBox

from crucible.sharing.history import ShareHistoryStore
from crucible.ui.share_history_dialog import ShareHistoryDialog


class ShareHistoryDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_history_view_copies_link_and_clear_does_not_delete_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packs = root / "player-packs"
            packs.mkdir(mode=0o700)
            archive = packs / "player-pack.zip"
            with zipfile.ZipFile(archive, "w") as pack:
                pack.writestr("manifest.json", "{}")
            link = "https://gofile.io/d/SyntheticLink123"
            store = ShareHistoryStore(root / "share-history" / "events.json")
            store.append(
                event="pack-prepared",
                instance_name="Example Server",
                archive_sha256="a" * 64,
                input_fingerprint="b" * 64,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                upload_status="not-attempted",
            )
            store.append(
                event="upload-succeeded",
                instance_name="Example Server",
                archive_sha256="a" * 64,
                input_fingerprint="b" * 64,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                provider="Gofile",
                share_url=link,
                upload_status="succeeded",
            )

            dialog = ShareHistoryDialog(store)
            self.assertEqual(dialog._table.rowCount(), 2)
            self.assertIn("not re-verified", dialog._table.item(0, 6).text())
            self.assertEqual(dialog._table.item(0, 6).toolTip(), link)
            dialog._table.selectRow(0)
            self.assertTrue(dialog._open_zip.isEnabled())
            clipboard = Mock()
            with patch.object(QApplication, "clipboard", return_value=clipboard):
                dialog._copy_selected_link()
            clipboard.setText.assert_called_once_with(link)

            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog._clear_history()
            self.assertEqual(store.load(), [])
            self.assertTrue(archive.is_file())
            dialog.close()

    def test_history_view_never_opens_zip_paths_outside_player_packs_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "some-other-file.zip"
            with zipfile.ZipFile(external, "w") as pack:
                pack.writestr("marker.txt", "not a Crucible player pack")
            store = ShareHistoryStore(root / "share-history" / "events.json")
            store.append(
                event="pack-prepared",
                instance_name="Untrusted history entry",
                local_path=external,
                size_bytes=external.stat().st_size,
                upload_status="not-attempted",
            )
            dialog = ShareHistoryDialog(store)
            dialog._table.selectRow(0)
            self.assertFalse(dialog._open_zip.isEnabled())
            with patch("crucible.ui.share_history_dialog.QDesktopServices.openUrl") as opener:
                dialog._open_selected_zip()
            opener.assert_not_called()
            self.assertIn("outside Crucible's player-packs folder", dialog._status.text())
            dialog.close()

    def test_delete_local_zip_requires_confirmation_and_preserves_remote_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packs = root / "player-packs"
            packs.mkdir(mode=0o700)
            archive = packs / "pack-to-delete.zip"
            with zipfile.ZipFile(archive, "w") as pack:
                pack.writestr("manifest.json", "{}")
            cache_sidecar = archive.with_suffix(archive.suffix + ".crucible-cache.json")
            cache_sidecar.write_text('{"schemaVersion":1}', encoding="utf-8")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            link = "https://gofile.io/d/KeepRemoteLink123"
            store = ShareHistoryStore(root / "share-history" / "events.json")
            store.append(
                event="upload-succeeded",
                instance_name="Example Server",
                archive_sha256=digest,
                input_fingerprint="b" * 64,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                provider="Gofile",
                share_url=link,
                upload_status="succeeded",
                size_verified=True,
                md5_verified=False,
            )

            dialog = ShareHistoryDialog(store)
            dialog._table.selectRow(0)
            self.assertTrue(dialog._delete_zip.isEnabled())
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.No,
            ) as ask:
                dialog._delete_selected_zip()
            ask.assert_called_once()
            self.assertTrue(archive.is_file())
            self.assertTrue(cache_sidecar.is_file())

            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ) as ask:
                dialog._table.selectRow(0)
                dialog._delete_selected_zip()
            ask.assert_called_once()
            self.assertFalse(archive.exists())
            self.assertFalse(cache_sidecar.exists())
            events = store.load()
            self.assertEqual(events[-1]["event"], "local-zip-deleted")
            self.assertEqual(events[-1]["archiveSha256"], digest)
            self.assertEqual(events[-1]["shareUrl"], link)
            self.assertIn("remote Gofile file/link is unchanged", dialog._status.text())
            dialog.close()

    def test_delete_refuses_zip_whose_checksum_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packs = root / "player-packs"
            packs.mkdir(mode=0o700)
            archive = packs / "changed.zip"
            with zipfile.ZipFile(archive, "w") as pack:
                pack.writestr("file.txt", "first")
            original_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            store = ShareHistoryStore(root / "share-history" / "events.json")
            store.append(
                event="pack-prepared",
                instance_name="Example Server",
                archive_sha256=original_digest,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                upload_status="not-attempted",
            )
            with zipfile.ZipFile(archive, "w") as pack:
                pack.writestr("file.txt", "changed after review")

            dialog = ShareHistoryDialog(store)
            dialog._table.selectRow(0)
            self.assertTrue(dialog._delete_zip.isEnabled())
            with patch("crucible.ui.share_history_dialog.QMessageBox.question") as ask:
                dialog._delete_selected_zip()
            ask.assert_not_called()
            self.assertTrue(archive.is_file())
            self.assertIn("checksum no longer matches", dialog._status.text())
            dialog.close()

    def test_delete_refuses_suspicious_cache_sidecar_without_removing_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packs = root / "player-packs"
            packs.mkdir(mode=0o700)
            archive = packs / "safe.zip"
            with zipfile.ZipFile(archive, "w") as pack:
                pack.writestr("file.txt", "data")
            sidecar = archive.with_suffix(archive.suffix + ".crucible-cache.json")
            target = root / "sidecar-target.json"
            target.write_text("keep", encoding="utf-8")
            try:
                sidecar.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            store = ShareHistoryStore(root / "share-history" / "events.json")
            store.append(
                event="pack-prepared",
                instance_name="Example Server",
                archive_sha256=digest,
                local_path=archive,
                size_bytes=archive.stat().st_size,
                upload_status="not-attempted",
            )
            dialog = ShareHistoryDialog(store)
            dialog._table.selectRow(0)
            with patch("crucible.ui.share_history_dialog.QMessageBox.question") as ask:
                dialog._delete_selected_zip()
            ask.assert_not_called()
            self.assertTrue(archive.is_file())
            self.assertTrue(sidecar.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")
            dialog.close()

    def test_history_view_handles_malformed_file_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "share-history" / "events.json"
            path.parent.mkdir()
            path.write_text("not json", encoding="utf-8")
            store = ShareHistoryStore(path)
            dialog = ShareHistoryDialog(store)
            self.assertEqual(dialog._table.rowCount(), 0)
            self.assertIn("Could not read local share history safely", dialog._status.text())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
