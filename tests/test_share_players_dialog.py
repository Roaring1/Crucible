from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtWidgets import QApplication

from crucible.exporters import client_export
from crucible.exporters.client_export import ExportResult
from crucible.mods import distribution_catalog
from crucible.sharing.gofile import GofileUploadResult
from crucible.ui.share_players_dialog import SharePlayersDialog, _PrepareWorker, _UploadWorker, _next_pack_path, _verified_cached_pack


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
        catalog = distribution_catalog.load_catalog()
        manifest = {
            "schemaVersion": 1,
            "format": "prism",
            "minecraftVersion": "1.21.1",
            "loader": "neoforge",
            "loaderVersion": "21.1.233",
            "manifestPath": manifest_path,
            "distributionCatalog": {
                "version": catalog.catalog_version,
                "revision": catalog.revision,
            },
            "distributionReview": [{
                "path": "Demo Server/.minecraft/mods/example.jar",
                "filename": "example.jar",
                "sha256": jar_hash,
                "modId": "",
                "modVersion": "",
                "decision": "unverified",
                "projectUrl": "",
                "evidenceUrl": "",
                "verifiedOn": "",
                "basis": "No curated record matches this exact JAR SHA-256.",
                "license": "",
                "licenseUrl": "",
                "conditions": [],
                "alternativeDownloadUrl": "",
                "notes": "",
                "bundled": True,
            }],
            "requiredDownloads": [],
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

    def test_worker_reuses_verified_pack_and_rebuilds_after_input_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_root = root / "player-packs"
            out_root.mkdir(mode=0o700)
            server = root / "server"
            (server / "mods").mkdir(parents=True)
            (server / "config").mkdir()
            (server / "config" / "server.toml").write_text("server_value = 1\n", encoding="utf-8")
            source = root / "working-prism"
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / ".minecraft" / "config").mkdir()
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.233"},
                ],
            }), encoding="utf-8")
            client_config = source / ".minecraft" / "config" / "client.toml"
            client_config.write_text("client_value = 1\n", encoding="utf-8")
            instance = SimpleNamespace(
                name="CachePack", path=str(server), minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.233",
            )

            first_path = _next_pack_path(out_root, instance.name)
            first_outcomes = []
            first_worker = _PrepareWorker(instance, first_path, str(source), False)
            first_worker.done.connect(first_outcomes.append)
            first_worker.run()
            self.assertEqual(len(first_outcomes), 1)
            self.assertTrue(first_outcomes[0].ok, first_outcomes[0].error)
            first_result = first_outcomes[0]
            self.assertTrue(first_result.input_fingerprint)
            cache_sidecar = first_path.with_suffix(first_path.suffix + ".crucible-cache.json")
            self.assertTrue(cache_sidecar.is_file())
            with zipfile.ZipFile(first_path, "r") as archive:
                manifest = json.loads(archive.read(first_result.manifest_path))
            self.assertEqual(manifest["inputFingerprint"], first_result.input_fingerprint)
            self.assertNotIn(str(source), json.dumps(manifest))
            if os.name == "posix":
                self.assertEqual(first_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(cache_sidecar.stat().st_mode & 0o777, 0o600)

            second_path = _next_pack_path(out_root, instance.name)
            second_outcomes = []
            second_worker = _PrepareWorker(instance, second_path, str(source), False)
            second_worker.done.connect(second_outcomes.append)
            with patch(
                "crucible.ui.share_players_dialog.client_export.export",
                side_effect=AssertionError("unchanged inputs should reuse the ZIP"),
            ):
                second_worker.run()
            self.assertEqual(len(second_outcomes), 1)
            self.assertTrue(second_outcomes[0].ok, second_outcomes[0].error)
            self.assertEqual(Path(second_outcomes[0].path), first_path)
            self.assertEqual(second_outcomes[0].archive_sha256, first_result.archive_sha256)
            self.assertTrue(any("Reused an existing local ZIP" in warning for warning in second_outcomes[0].warnings))
            self.assertFalse(second_path.exists(), "the unused filename reservation should be removed on a cache hit")

            client_config.write_text("client_value = 2\n", encoding="utf-8")
            third_path = _next_pack_path(out_root, instance.name)
            third_outcomes = []
            third_worker = _PrepareWorker(instance, third_path, str(source), False)
            third_worker.done.connect(third_outcomes.append)
            third_worker.run()
            self.assertEqual(len(third_outcomes), 1)
            self.assertTrue(third_outcomes[0].ok, third_outcomes[0].error)
            self.assertEqual(Path(third_outcomes[0].path), third_path)
            self.assertNotEqual(third_outcomes[0].input_fingerprint, first_result.input_fingerprint)
            self.assertTrue(third_path.with_suffix(third_path.suffix + ".crucible-cache.json").is_file())

    def test_cache_rejects_archive_with_forged_distribution_permission_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_root = root / "player-packs"
            out_root.mkdir(mode=0o700)
            server = root / "server"
            (server / "mods").mkdir(parents=True)
            (server / "config").mkdir()
            with zipfile.ZipFile(server / "mods" / "example.jar", "w") as jar:
                jar.writestr("fabric.mod.json", json.dumps({
                    "id": "examplemod", "name": "Example Mod", "version": "1.2.3", "environment": "*"
                }))
            source = root / "working-prism"
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.233"},
                ],
            }), encoding="utf-8")
            instance = SimpleNamespace(
                name="ForgedCatalogCache", path=str(server), minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.233",
            )
            archive_path = _next_pack_path(out_root, instance.name)
            outcomes = []
            worker = _PrepareWorker(instance, archive_path, str(source), False)
            worker.done.connect(outcomes.append)
            worker.run()
            self.assertTrue(outcomes[0].ok, outcomes[0].error)
            result = outcomes[0]
            sidecar = archive_path.with_suffix(archive_path.suffix + ".crucible-cache.json")

            with zipfile.ZipFile(archive_path, "r") as archive:
                entries = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
            replacement = out_root / "replacement.tmp"
            with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, payload in entries:
                    if name == result.manifest_path:
                        manifest = json.loads(payload)
                        row = manifest["distributionReview"][0]
                        row["decision"] = "permitted"
                        payload = json.dumps(manifest).encode("utf-8")
                    archive.writestr(name, payload)
            replacement.replace(archive_path)

            # Simulate a self-consistent stale cache entry: update the sidecar's
            # archive checksum too, so only catalog-to-manifest validation catches it.
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            metadata["archiveSha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            sidecar.write_text(json.dumps(metadata), encoding="utf-8")
            self.assertIsNone(_verified_cached_pack(archive_path, result.input_fingerprint))

    def test_worker_rebuilds_instead_of_reusing_a_corrupted_cache_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_root = root / "player-packs"
            out_root.mkdir(mode=0o700)
            server = root / "server"
            (server / "mods").mkdir(parents=True)
            (server / "config").mkdir()
            source = root / "working-prism"
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.233"},
                ],
            }), encoding="utf-8")
            instance = SimpleNamespace(
                name="CorruptCache", path=str(server), minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.233",
            )

            first_path = _next_pack_path(out_root, instance.name)
            first_outcomes = []
            first_worker = _PrepareWorker(instance, first_path, str(source), False)
            first_worker.done.connect(first_outcomes.append)
            first_worker.run()
            self.assertTrue(first_outcomes[0].ok, first_outcomes[0].error)
            first_path.write_bytes(b"corrupted archive bytes")

            second_path = _next_pack_path(out_root, instance.name)
            second_outcomes = []
            second_worker = _PrepareWorker(instance, second_path, str(source), False)
            second_worker.done.connect(second_outcomes.append)
            second_worker.run()

            self.assertTrue(second_outcomes[0].ok, second_outcomes[0].error)
            self.assertEqual(Path(second_outcomes[0].path), second_path)
            with zipfile.ZipFile(second_path, "r") as archive:
                self.assertIsNone(archive.testzip())
                manifest = json.loads(archive.read(second_outcomes[0].manifest_path))
            self.assertEqual(manifest["inputFingerprint"], second_outcomes[0].input_fingerprint)

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
                "crucible.ui.share_players_dialog.client_export.compute_input_fingerprint",
                return_value="b" * 64,
            ), patch(
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
            self.assertIn("DISTRIBUTION RIGHTS (curated, exact-artifact SHA-256 catalog)", report)
            self.assertIn("UNVERIFIED JAR — no reviewed catalog record for these exact bytes", report)
            self.assertIn("UNVERIFIED RESOURCE PACK — check distribution terms separately: ExamplePack.zip", report)
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

    def test_modified_zip_is_rejected_before_file_review_or_upload(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "pack.zip"
            result = self._result_archive(archive_path)

            with zipfile.ZipFile(archive_path, "r") as archive:
                entries = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in entries:
                    if name.endswith("/.minecraft/config/test.toml"):
                        payload = b"changed after build = true\\n"
                    archive.writestr(name, payload)

            dialog._on_pack_ready(result)

            self.assertEqual(dialog._phase, "error")
            self.assertIsNone(dialog._result)
            self.assertIn("local ZIP changed after preparation", dialog._status.text())
            self.assertFalse(dialog._upload_btn.isEnabled())

        dialog.close()

    def test_manifest_cannot_claim_permission_without_a_current_catalog_record(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "pack.zip"
            result = self._result_archive(archive_path)

            with zipfile.ZipFile(archive_path, "r") as archive:
                entries = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in entries:
                    if name == result.manifest_path:
                        manifest = json.loads(payload)
                        row = manifest["distributionReview"][0]
                        row.update({
                            "decision": "permitted",
                            "projectUrl": "https://example.org/project",
                            "evidenceUrl": "https://example.org/claimed-permission",
                            "verifiedOn": "2026-10-01",
                            "basis": "Synthetic test claim; no real permission.",
                            "license": "Example-License",
                            "licenseUrl": "https://example.org/license",
                        })
                        payload = json.dumps(manifest).encode("utf-8")
                    archive.writestr(name, payload)
            result.archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            dialog._on_pack_ready(result)

            self.assertEqual(dialog._phase, "error")
            self.assertIsNone(dialog._result)
            self.assertIn("unknown artifact hash", dialog._status.text())
            self.assertFalse(dialog._upload_btn.isEnabled())

        dialog.close()

    def test_manifest_cannot_claim_permission_for_unknown_jar_hash(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "pack.zip"
            result = self._result_archive(archive_path)

            with zipfile.ZipFile(archive_path, "r") as archive:
                entries = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in entries:
                    if name == result.manifest_path:
                        manifest = json.loads(payload)
                        row = manifest["distributionReview"][0]
                        row.update({
                            "decision": "permitted",
                            "projectUrl": "https://example.org/fake-project",
                            "evidenceUrl": "https://example.org/fake-permission",
                            "verifiedOn": "2026-10-01",
                            "basis": "Fabricated permission claim for a hash not present in the current catalog.",
                            "license": "Fake-License",
                            "licenseUrl": "https://example.org/fake-license",
                        })
                        payload = json.dumps(manifest).encode("utf-8")
                    archive.writestr(name, payload)
            result.archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            dialog._on_pack_ready(result)

            self.assertEqual(dialog._phase, "error")
            self.assertIsNone(dialog._result)
            self.assertIn("manifest claims a curated distribution decision for an unknown artifact hash", dialog._status.text())
            self.assertFalse(dialog._upload_btn.isEnabled())

        dialog.close()

    def test_manifest_without_distribution_catalog_fails_closed(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "pack.zip"
            result = self._result_archive(archive_path)

            with zipfile.ZipFile(archive_path, "r") as archive:
                entries = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in entries:
                    if name == result.manifest_path:
                        manifest = json.loads(payload)
                        manifest.pop("distributionCatalog")
                        manifest.pop("distributionReview")
                        manifest.pop("requiredDownloads")
                        payload = json.dumps(manifest).encode("utf-8")
                    archive.writestr(name, payload)
            result.archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            dialog._on_pack_ready(result)

            self.assertEqual(dialog._phase, "error")
            self.assertIsNone(dialog._result)
            self.assertIn("must include complete distribution-catalog", dialog._status.text())
            self.assertFalse(dialog._upload_btn.isEnabled())

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
            result.archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            dialog._on_pack_ready(result)

            self.assertEqual(dialog._phase, "error")
            self.assertIsNone(dialog._result)
            self.assertIn("manifest root is not a JSON object", dialog._status.text())
            self.assertFalse(dialog._upload_btn.isEnabled())

        dialog.close()

    def test_review_explains_curated_prohibited_mod_alternative_download(self):
        dialog = self._dialog()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "mods").mkdir(parents=True)
            jar = server / "mods" / "examplemod-1.2.3.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("fabric.mod.json", json.dumps({
                    "id": "examplemod", "name": "Example Mod", "version": "1.2.3", "environment": "*"
                }))
            alternative = "https://downloads.example.org/examplemod"
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schemaVersion": 1,
                "catalogVersion": "ui-test-1",
                "records": [{
                    "sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
                    "modId": "examplemod",
                    "modVersion": "1.2.3",
                    "decision": "prohibited",
                    "projectUrl": "https://example.org/examplemod",
                    "evidenceUrl": "https://example.org/redistribution",
                    "verifiedOn": "2026-10-01",
                    "basis": "The source terms prohibit redistributing this exact artifact.",
                    "license": "",
                    "licenseUrl": "",
                    "conditions": [],
                    "alternativeDownloadUrl": alternative,
                    "notes": "Synthetic test fixture only.",
                }],
            }), encoding="utf-8")
            output = root / "player.zip"
            instance = SimpleNamespace(
                name="Demo Server", path=str(server), minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.233",
            )
            original_loader = distribution_catalog.load_catalog
            def load_test_catalog(path=None):
                return original_loader(catalog_path if path is None else path)

            with patch.object(distribution_catalog, "load_catalog", side_effect=load_test_catalog):
                result = client_export.export(instance, output, fmt="prism")
                self.assertTrue(result.ok, result.error)
                dialog._on_pack_ready(result)

            report = dialog._review.toPlainText()
            self.assertIn("NOT BUNDLED — REQUIRED MANUAL MOD DOWNLOADS", report)
            self.assertIn(alternative, report)
            self.assertIn("CRUCIBLE-MOD-DOWNLOADS.txt", report)
            self.assertIn("1 required manual mod download(s)", dialog._license_ack.text())
            self.assertIn("DISTRIBUTION RIGHTS", report)
            with zipfile.ZipFile(output) as archive:
                members = set(archive.namelist())
                self.assertNotIn("Demo Server/.minecraft/mods/examplemod-1.2.3.jar", members)
                self.assertIn("Demo Server/CRUCIBLE-MOD-DOWNLOADS.txt", members)

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
