from __future__ import annotations

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
