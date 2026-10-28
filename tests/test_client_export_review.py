from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crucible.exporters import client_export
from crucible.mods import distribution_catalog


class ClientExportReviewTests(unittest.TestCase):
    def _instance(self, server: Path):
        return SimpleNamespace(
            path=str(server),
            name="CreatePlus",
            minecraft_version="1.21.1",
            loader="neoforge",
            loader_version="21.1.233",
        )

    def test_manifest_hashes_every_packaged_file_and_scans_config_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "config").mkdir(parents=True)
            secret = "sk_live_A1B2C3D4E5F6_G7H8I9J0"
            config = server / "config" / "remote.toml"
            config.write_text(
                f'api_key = "{secret}"\nserver_address = "192.168.1.17"\n',
                encoding="utf-8",
            )
            (server / "defaultconfigs").mkdir()
            (server / "defaultconfigs" / "server.toml").write_text("do_not_ship = true\n", encoding="utf-8")
            (server / "world").mkdir()
            (server / "world" / "level.dat").write_text("world data\n", encoding="utf-8")

            output = root / "CreatePlus.zip"
            result = client_export.export(self._instance(server), output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.archive_sha256)
            self.assertEqual(
                result.archive_sha256,
                hashlib.sha256(output.read_bytes()).hexdigest(),
            )
            self.assertIn("CreatePlus/crucible-manifest.json", result.manifest_path)

            warning_text = "\n".join(result.warnings)
            self.assertIn("credential assignment", warning_text)
            self.assertIn("private IPv4 address", warning_text)
            self.assertIn("config/remote.toml", warning_text)
            self.assertNotIn(secret, warning_text)

            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                members = set(archive.namelist())
                manifest_path = "CreatePlus/crucible-manifest.json"
                self.assertIn(manifest_path, members)
                self.assertFalse(any("/.minecraft/defaultconfigs/" in n for n in members))
                self.assertFalse(any("/.minecraft/world/" in n for n in members))
                manifest = json.loads(archive.read(manifest_path))
                rows = {row["path"]: row for row in manifest["files"]}
                self.assertEqual(members - {manifest_path}, set(rows))
                for name, row in rows.items():
                    payload = archive.read(name)
                    self.assertEqual(row["size_bytes"], len(payload), name)
                    self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest(), name)
                self.assertEqual(manifest["minecraftVersion"], "1.21.1")
                self.assertEqual(manifest["loader"], "neoforge")
                self.assertEqual(manifest["loaderVersion"], "21.1.233")
                self.assertNotIn(secret, archive.read(manifest_path).decode("utf-8"))

    def test_input_fingerprint_changes_when_client_payload_or_options_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "mods").mkdir(parents=True)
            (server / "config").mkdir()
            (server / "config" / "server.toml").write_text("value = 1\n", encoding="utf-8")
            source = root / "client-profile"
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / ".minecraft" / "config").mkdir()
            (source / "mmc-pack.json").write_text(
                json.dumps({"formatVersion": 1, "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.233"},
                ]}), encoding="utf-8"
            )
            client_config = source / ".minecraft" / "config" / "client.toml"
            client_config.write_text("value = 1\n", encoding="utf-8")
            instance = self._instance(server)

            first = client_export.compute_input_fingerprint(
                instance, client_source=str(source), include_config=True, include_resourcepacks=False
            )
            self.assertEqual(len(first), 64)
            self.assertNotIn(str(server), first)
            self.assertNotIn(str(source), first)

            client_config.write_text("value = 2\n", encoding="utf-8")
            changed_payload = client_export.compute_input_fingerprint(
                instance, client_source=str(source), include_config=True, include_resourcepacks=False
            )
            self.assertNotEqual(first, changed_payload)

            changed_option = client_export.compute_input_fingerprint(
                instance, client_source=str(source), include_config=True, include_resourcepacks=True
            )
            self.assertNotEqual(changed_payload, changed_option)

            with patch.object(distribution_catalog, "catalog_revision", return_value="a" * 64):
                old_catalog_fingerprint = client_export.compute_input_fingerprint(
                    instance, client_source=str(source), include_config=True, include_resourcepacks=False
                )
            with patch.object(distribution_catalog, "catalog_revision", return_value="b" * 64):
                new_catalog_fingerprint = client_export.compute_input_fingerprint(
                    instance, client_source=str(source), include_config=True, include_resourcepacks=False
                )
            self.assertNotEqual(old_catalog_fingerprint, new_catalog_fingerprint)

    def test_export_refuses_a_source_that_changed_after_fingerprint_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "mods").mkdir(parents=True)
            (server / "config").mkdir()
            source = root / "profile"
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
            client_config.write_text("value = 1\n", encoding="utf-8")
            instance = self._instance(server)
            baseline = client_export.compute_input_fingerprint(
                instance, client_source=str(source), include_config=True, include_resourcepacks=False
            )
            client_config.write_text("value = 2\n", encoding="utf-8")

            output = root / "changed-during-build.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source),
                expected_input_fingerprint=baseline,
            )

            self.assertFalse(result.ok)
            self.assertIn("inputs changed", result.error.lower())
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".zip.partial").exists())

    def _catalog_test_jar(self, root: Path):
        server = root / "server"
        (server / "mods").mkdir(parents=True)
        jar = server / "mods" / "examplemod-1.2.3.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({
                "id": "examplemod", "name": "Example Mod", "version": "1.2.3", "environment": "*"
            }))
        return self._instance(server), jar

    def _distribution_record(self, jar: Path, *, decision: str, alternative: str = ""):
        return {
            "sha256": client_export._sha256_file(jar),
            "modId": "examplemod",
            "modVersion": "1.2.3",
            "decision": decision,
            "projectUrl": "https://example.org/examplemod",
            "evidenceUrl": "https://example.org/redistribution-policy",
            "verifiedOn": "2026-10-01",
            "basis": "The cited publisher policy was checked for this exact artifact.",
            "license": "Example-License-1.0" if decision == "permitted" else "",
            "licenseUrl": "https://example.org/license" if decision == "permitted" else "",
            "conditions": ["Preserve the included attribution notice."] if decision == "conditional" else [],
            "alternativeDownloadUrl": alternative,
            "notes": "Synthetic test fixture only; not a real mod license.",
        }

    def _with_catalog(self, catalog_path: Path):
        original_loader = distribution_catalog.load_catalog
        return patch.object(
            distribution_catalog,
            "load_catalog",
            side_effect=lambda path=None: original_loader(catalog_path if path is None else path),
        )

    def test_unknown_jar_remains_unverified_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, jar = self._catalog_test_jar(root)
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schemaVersion": 1, "catalogVersion": "test-unknown", "records": []
            }), encoding="utf-8")
            output = root / "player.zip"
            with self._with_catalog(catalog_path):
                result = client_export.export(instance, output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read(result.manifest_path))
                row = next(item for item in manifest["distributionReview"] if item["filename"] == jar.name)
                self.assertIn(f"CreatePlus/.minecraft/mods/{jar.name}", archive.namelist())
            self.assertEqual(row["decision"], "unverified")
            self.assertTrue(row["bundled"])

    def test_prohibited_jar_without_alternative_blocks_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, jar = self._catalog_test_jar(root)
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schemaVersion": 1, "catalogVersion": "test-prohibited",
                "records": [self._distribution_record(jar, decision="prohibited")],
            }), encoding="utf-8")
            output = root / "player.zip"
            with self._with_catalog(catalog_path):
                result = client_export.export(instance, output, fmt="prism")
            self.assertFalse(result.ok)
            self.assertIn("prohibited for redistribution", result.error)
            self.assertFalse(output.exists())

    def test_prohibited_jar_with_reviewed_alternative_is_omitted_with_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, jar = self._catalog_test_jar(root)
            alternative = "https://downloads.example.org/examplemod"
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schemaVersion": 1, "catalogVersion": "test-alternative",
                "records": [self._distribution_record(jar, decision="prohibited", alternative=alternative)],
            }), encoding="utf-8")
            output = root / "player.zip"
            with self._with_catalog(catalog_path):
                result = client_export.export(instance, output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                members = set(archive.namelist())
                guide_path = "CreatePlus/CRUCIBLE-MOD-DOWNLOADS.txt"
                self.assertIn(guide_path, members)
                self.assertNotIn(f"CreatePlus/.minecraft/mods/{jar.name}", members)
                guide = archive.read(guide_path).decode("utf-8")
                manifest = json.loads(archive.read(result.manifest_path))
            self.assertIn(alternative, guide)
            self.assertIn("redistribution.\nThis instance may not start", guide)
            self.assertEqual(len(manifest["requiredDownloads"]), 1)
            review = manifest["distributionReview"][0]
            self.assertEqual(review["decision"], "prohibited")
            self.assertFalse(review["bundled"])
            self.assertEqual(review["sha256"], client_export._sha256_file(jar))

    def test_permitted_catalog_entry_is_bound_to_exact_jar_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance, jar = self._catalog_test_jar(root)
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schemaVersion": 1, "catalogVersion": "test-permitted",
                "records": [self._distribution_record(jar, decision="permitted")],
            }), encoding="utf-8")
            output = root / "player.zip"
            with self._with_catalog(catalog_path):
                result = client_export.export(instance, output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read(result.manifest_path))
            review = manifest["distributionReview"][0]
            self.assertEqual(review["decision"], "permitted")
            self.assertTrue(review["bundled"])
            self.assertEqual(review["sha256"], client_export._sha256_file(jar))
            self.assertEqual(manifest["distributionCatalog"]["revision"], distribution_catalog.load_catalog(catalog_path).revision)

    def test_config_paths_reject_backslash_based_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "config").mkdir(parents=True)
            # Backslashes are legal filename characters on Linux but can act as
            # separators on Windows archive extraction.
            (server / "config" / r"bad\..\server.properties").write_text(
                "server-port=25565\n", encoding="utf-8"
            )
            output = root / "unsafe.zip"
            result = client_export.export(self._instance(server), output, fmt="prism")
            self.assertFalse(result.ok)
            self.assertIn("Unsafe archive path", result.error)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".zip.partial").exists())

    def test_symlinked_config_file_is_excluded_and_warned_about(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            config = server / "config"
            config.mkdir(parents=True)
            outside = root / "outside.toml"
            secret = "sk_live_DO_NOT_PACKAGE_THIS_VALUE"
            outside.write_text(f"api_key = '{secret}'\n", encoding="utf-8")
            (config / "linked.toml").symlink_to(outside)
            output = root / "symlink.zip"
            result = client_export.export(self._instance(server), output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            self.assertTrue(output.exists())
            self.assertTrue(
                any("Excluded symlink from client configuration: linked.toml" in w for w in result.warnings),
                result.warnings,
            )
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(any(name.endswith("/linked.toml") for name in archive.namelist()))
                manifest = json.loads(archive.read("CreatePlus/crucible-manifest.json"))
                self.assertFalse(any("linked.toml" in row["path"] for row in manifest["files"]))
            self.assertNotIn(secret, "\n".join(result.warnings))


    def test_symlinked_client_source_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "real-prism"
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(
                json.dumps({"formatVersion": 1, "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.233"},
                ]}),
                encoding="utf-8",
            )
            linked = root / "linked-prism"
            linked.symlink_to(source, target_is_directory=True)
            output = root / "pack.zip"
            result = client_export.export(
                self._instance(server), output, fmt="prism", client_source=str(linked)
            )
            self.assertFalse(result.ok)
            self.assertIn("selected Prism source is a symbolic link", result.error)
            self.assertFalse(output.exists())


    def test_manifest_records_privacy_review_and_warns_on_binary_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            config = server / "config"
            config.mkdir(parents=True)
            (config / "odd.toml").write_bytes(b"setting = true\x00\x01binary")
            (config / "unrecognized.dat").write_bytes(b"opaque config data")
            output = root / "binary-config.zip"
            result = client_export.export(self._instance(server), output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read(result.manifest_path))
            self.assertEqual(manifest["manifestPath"], result.manifest_path)
            review = manifest["privacyReview"]
            self.assertFalse(review["coverageComplete"])
            self.assertIn("heuristic", review["scanner"])
            self.assertIn("JARs, resource packs", review["scope"])
            self.assertTrue(
                any("Privacy scan skipped binary-looking config file" in warning
                    for warning in review["warnings"]),
                review["warnings"],
            )
            self.assertTrue(
                any("Privacy scan skipped binary-looking config file" in warning
                    for warning in result.warnings),
                result.warnings,
            )
            self.assertTrue(
                any("unrecognized file types" in warning and "unrecognized.dat" in warning
                    for warning in result.warnings),
                result.warnings,
            )
            self.assertTrue(
                any("unrecognized.dat" in warning
                    for warning in review["warnings"]),
                review["warnings"],
            )

    def test_manifest_warns_and_names_config_files_over_scan_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            config = server / "config"
            config.mkdir(parents=True)
            oversized = config / "large.toml"
            oversized.write_bytes(b"safe_key = true\n" + b"# padding\n" * 120000)
            output = root / "large-config.zip"
            result = client_export.export(self._instance(server), output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            self.assertTrue(
                any("Privacy scan incomplete" in warning and "config/large.toml" in warning
                    for warning in result.warnings),
                result.warnings,
            )
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read(result.manifest_path))
            self.assertFalse(manifest["privacyReview"]["coverageComplete"])
            self.assertTrue(
                any("config/large.toml" in warning
                    for warning in manifest["privacyReview"]["warnings"]),
                manifest["privacyReview"],
            )


if __name__ == "__main__":
    unittest.main()
