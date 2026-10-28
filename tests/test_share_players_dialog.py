from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtWidgets import QApplication

from crucible.exporters.client_export import ExportResult
from crucible.sharing.gofile import GofileUploadResult
from crucible.ui.share_players_dialog import SharePlayersDialog, _PrepareWorker, _UploadWorker, _next_pack_path


class SharePlayersDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self):
        instance = SimpleNamespace(name="Demo Server", path="/not-used", prism_source="")
        return SharePlayersDialog(instance)

    def _result_archive(self, path: Path):
        jar = b"synthetic jar content"
        config = b"distance = 12\n"
        resource_pack = b"synthetic resource pack archive"
        jar_hash = hashlib.sha256(jar).hexdigest()
        config_hash = hashlib.sha256(config).hexdigest()
        resource_pack_hash = hashlib.sha256(resource_pack).hexdigest()
        manifest_path = "Demo Server/crucible-manifest.json"
        manifest = {
            "schemaVersion": 1,
            "format": "prism",
            "minecraftVersion": "1.21.1",
            "loader": "neoforge",
            "loaderVersion": "21.1.233",
            "manifestPath": manifest_path,
            "privacyReview": {
                "scanner": "bounded heuristic pattern scan",
                "coverageComplete": True,
                "warnings": [
                    "Privacy review: possible credential assignment in config/test.toml; inspect before sharing (value not shown)."
                ],
            },
            "files": [
                {"path": "Demo Server/.minecraft/mods/example.jar", "size_bytes": len(jar), "sha256": jar_hash},
                {"path": "Demo Server/.minecraft/config/test.toml", "size_bytes": len(config), "sha256": config_hash},
                {"path": "Demo Server/.minecraft/resourcepacks/ExamplePack.zip", "size_bytes": len(resource_pack), "sha256": resource_pack_hash},
            ],
        }
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Demo Server/.minecraft/mods/example.jar", jar)
            archive.writestr("Demo Server/.minecraft/config/test.toml", config)
            archive.writestr("Demo Server/.minecraft/resourcepacks/ExamplePack.zip", resource_pack)
            archive.writestr(manifest_path, json.dumps(manifest))
        return ExportResult(
            path=str(path),
            fmt="prism",
            mod_count=1,
            warnings=["Bundled JAR redistribution rights are not automatically verified."],
            manifest_path=manifest_path,
            included_files=[
                "Demo Server/.minecraft/mods/example.jar",
                "Demo Server/.minecraft/config/test.toml",
            ],
            archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def test_local_player_pack_name_is_clear_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _next_pack_path(root, "CreatePlus")
            self.assertEqual(first.name, "CreatePlus - Player Pack - v1.zip")
            self.assertTrue(first.exists(), "the filename must be atomically reserved")
            if __import__("os").name == "posix":
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            first.write_bytes(b"keep old pack")
            second = _next_pack_path(root, "CreatePlus")
            self.assertEqual(second.name, "CreatePlus - Player Pack - v2.zip")
            self.assertEqual(first.read_bytes(), b"keep old pack")

    def test_symlinked_player_pack_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "app-data"
            app_data.mkdir()
            real_directory = root / "real-player-packs"
            real_directory.mkdir()
            linked = app_data / "player-packs"
            try:
                linked.symlink_to(real_directory, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            source = root / "working-prism"
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.233"},
                ],
            }), encoding="utf-8")
            dialog = SharePlayersDialog(SimpleNamespace(
                name="Demo Server", path="/not-used", prism_source=str(source)
            ))
            dialog._source.setText(str(source))
            with patch.object(
                QStandardPaths, "writableLocation", return_value=str(app_data)
            ):
                dialog._prepare_pack()
            self.assertEqual(dialog._phase, "error")
            self.assertIn("symbolic link", dialog._status.text())
            self.assertFalse(any(real_directory.iterdir()))
            self.assertFalse(dialog._upload_btn.isEnabled())
            dialog.close()

    def test_permission_failure_removes_the_unprotected_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CreatePlus - Player Pack - v1.zip"
            path.write_bytes(b"synthetic archive bytes")
            instance = SimpleNamespace(name="Demo", path="/not-used")
            worker = _PrepareWorker(instance, path, "/client", False)
            outcomes = []
            worker.done.connect(outcomes.append)
            with patch(
                "crucible.ui.share_players_dialog.client_export.export",
                return_value=ExportResult(
                    path=str(path),
                    fmt="prism",
                    archive_sha256="a" * 64,
                ),
            ), patch.object(Path, "chmod", side_effect=PermissionError("test denied")):
                worker.run()
            self.assertEqual(len(outcomes), 1)
            self.assertFalse(outcomes[0].ok)
            self.assertIn("no upload is allowed", outcomes[0].error.lower())
            self.assertFalse(path.exists())

    def test_optional_token_field_is_masked_and_cleared_when_disabled(self):
        dialog = self._dialog()
        self.assertTrue(dialog._api_token.isHidden())
        self.assertEqual(
            dialog._api_token.echoMode(),
            dialog._api_token.EchoMode.Password,
        )
        dialog._use_api_token.setChecked(True)
        self.assertFalse(dialog._api_token.isHidden())
        dialog._api_token.setText("temporary-owner-token")
        dialog._use_api_token.setChecked(False)
        self.assertEqual(dialog._api_token.text(), "")
        self.assertTrue(dialog._api_token.isHidden())
        self.assertFalse(dialog._api_token.isEnabled())
        # After a failed guest upload the dialog disables the field; opting in
        # must re-enable it so the owner can authenticate on the same ZIP retry.
        dialog._api_token.setEnabled(False)
        dialog._use_api_token.setChecked(True)
        self.assertTrue(dialog._api_token.isEnabled())
        dialog.close()

    def test_resourcepacks_are_off_by_default_and_pause_auto_build_for_review(self):
        dialog = self._dialog()
        self.assertFalse(dialog._resourcepacks.isChecked())
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "profile"
            (source / ".minecraft" / "resourcepacks").mkdir(parents=True)
            (source / ".minecraft" / "resourcepacks" / "RequiredPack.zip").write_bytes(b"pack")
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.233"},
                ],
            }), encoding="utf-8")
            dialog._source.setText(str(source))
            from unittest.mock import patch
            with patch.object(dialog, "_prepare_pack") as prepare:
                dialog.show()
                self.app.processEvents()
                prepare.assert_not_called()
            self.assertIn("resource packs", dialog._status.text())
            self.assertIn("excluded by default", dialog._status.text())
        dialog.close()

    def test_share_path_refuses_to_build_without_working_client_profile(self):
        dialog = self._dialog()
        dialog._prepare_pack()
        self.assertEqual(dialog._phase, "error")
        self.assertIn("Choose the working Prism profile", dialog._status.text())
        self.assertIsNone(dialog._result)
        self.assertFalse(dialog._upload_btn.isEnabled())
        dialog.close()

    def test_upload_stays_disabled_until_all_three_review_approvals(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pack.zip"
            result = self._result_archive(archive)
            dialog._on_pack_ready(result)

            self.assertEqual(dialog._phase, "review")
            self.assertFalse(dialog._upload_btn.isEnabled())

            dialog._review_ack.setChecked(True)
            self.assertFalse(dialog._upload_btn.isEnabled())
            dialog._license_ack.setChecked(True)
            self.assertFalse(dialog._upload_btn.isEnabled())
            dialog._public_ack.setChecked(True)
            self.assertTrue(dialog._upload_btn.isEnabled())

        dialog.close()

    def test_review_lists_exact_files_privacy_findings_and_each_unknown_jar_license(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            result = self._result_archive(Path(tmp) / "pack.zip")
            dialog._built_source = str(Path(tmp) / "validated-prism-profile")
            dialog._on_pack_ready(result)
            report = dialog._review.toPlainText()
            self.assertIn("Validated Prism client source used:", report)
            self.assertIn("validated-prism-profile", report)
            self.assertIn("Demo Server/.minecraft/mods/example.jar", report)
            self.assertIn("Demo Server/.minecraft/config/test.toml", report)
            self.assertIn("credential assignment", report)
            self.assertEqual(report.count("possible credential assignment"), 1)
            self.assertIn("DISTRIBUTION RIGHTS (not automatically verified)", report)
            self.assertIn("UNVERIFIED JAR — check the author's license/distribution terms", report)
            self.assertIn("UNVERIFIED RESOURCE PACK — check distribution terms: ExamplePack.zip", report)
            self.assertIn(result.archive_sha256, report)
            self.assertIn(result.manifest_path, report)
            self.assertIn("cannot guarantee a secret-free pack", dialog._review_ack.text())

        dialog.close()

    def test_success_copies_public_link_automatically_and_enables_manual_actions(self):
        dialog = self._dialog()
        result = GofileUploadResult(
            download_url="https://gofile.io/d/AbCdEf123",
            file_id="synthetic-file-id",
            file_name="CreatePlus - Player Pack - v1.zip",
            file_size=123,
            local_sha256="a" * 64,
            remote_md5="",
            size_verified=False,
            md5_verified=False,
        )
        clipboard = Mock()
        dialog._phase = "uploading"
        with patch.object(QApplication, "clipboard", return_value=clipboard):
            dialog._on_upload_done({"result": result, "error": "", "cancelled": False})
        clipboard.setText.assert_called_once_with(result.download_url)
        self.assertEqual(dialog._link.text(), result.download_url)
        self.assertTrue(dialog._copy_link_btn.isEnabled())
        self.assertTrue(dialog._copy_instructions_btn.isEnabled())
        self.assertTrue(dialog._open_link_btn.isEnabled())
        self.assertIn("copied to the clipboard", dialog._status.text())
        dialog.close()

    def test_malformed_manifest_fails_closed_without_crashing_review(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "pack.zip"
            result = self._result_archive(archive_path)

            with zipfile.ZipFile(archive_path, "r") as archive:
                entries = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in entries:
                    if name == result.manifest_path:
                        payload = b"[]"
                    archive.writestr(name, payload)

            dialog._on_pack_ready(result)

            self.assertEqual(dialog._phase, "error")
            self.assertIsNone(dialog._result)
            self.assertIn("manifest root is not a JSON object", dialog._status.text())
            self.assertFalse(dialog._upload_btn.isEnabled())

        dialog.close()

    def test_retry_after_failure_requires_existing_approved_local_zip(self):
        dialog = self._dialog()
        dialog._phase = "upload-error"
        dialog._result = None
        dialog._refresh_upload_enabled()
        self.assertFalse(dialog._upload_btn.isEnabled())

        with tempfile.TemporaryDirectory() as tmp:
            result = self._result_archive(Path(tmp) / "pack.zip")
            dialog._on_pack_ready(result)
            dialog._review_ack.setChecked(True)
            dialog._license_ack.setChecked(True)
            dialog._public_ack.setChecked(True)
            dialog._phase = "upload-error"
            dialog._refresh_upload_enabled()
            self.assertTrue(dialog._upload_btn.isEnabled())

        dialog.close()


    def test_unexpected_provider_exception_does_not_echo_sensitive_details(self):
        secret = "private-owner-token-value"

        class BrokenProvider:
            name = "Broken test provider"

            def upload_file(
                self, path, *, expected_sha256, progress_cb=None, cancel_event=None
            ):
                raise RuntimeError(f"request failed with Authorization: Bearer {secret}")

        worker = _UploadWorker(
            BrokenProvider(),
            Path("synthetic-pack.zip"),
            "a" * 64,
            threading.Event(),
        )
        outcomes = []
        worker.done.connect(outcomes.append)
        worker.run()

        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0]["cancelled"])
        self.assertIsNone(outcomes[0]["result"])
        self.assertNotIn(secret, outcomes[0]["error"])
        self.assertIn("sensitive details were suppressed", outcomes[0]["error"].lower())


if __name__ == "__main__":
    unittest.main()
