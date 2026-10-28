from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from crucible.exporters import client_export


class ClientExportTests(unittest.TestCase):
    def test_prism_zip_includes_client_config_but_excludes_server_defaults_and_worlds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            for relative in (
                "config/example-client.toml",
                "kubejs/client_scripts/example.js",
                "defaultconfigs/server-default.toml",
                "world/level.dat",
                "screenshots/private.png",
            ):
                path = server / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test data", encoding="utf-8")

            instance = SimpleNamespace(
                path=str(server),
                name="CreatePlus",
                minecraft_version="1.21.1",
                loader="neoforge",
                loader_version="21.1.1",
            )
            output = root / "CreatePlus - Player Pack - v1.zip"
            result = client_export.export(instance, output, fmt="prism")

            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                self.assertIn("CreatePlus/instance.cfg", names)
                self.assertIn("CreatePlus/mmc-pack.json", names)
                self.assertIn("CreatePlus/.minecraft/config/example-client.toml", names)
                self.assertIn("CreatePlus/.minecraft/kubejs/client_scripts/example.js", names)
                self.assertFalse(any("/.minecraft/defaultconfigs/" in name for name in names))
                self.assertFalse(any("/.minecraft/world/" in name for name in names))
                self.assertFalse(any("screenshots/" in name for name in names))
                components = json.loads(archive.read("CreatePlus/mmc-pack.json"))["components"]
                self.assertIn({"uid": "net.neoforged", "version": "21.1.1"}, components)

            self.assertTrue(
                any("defaultconfigs/ was excluded" in warning for warning in result.warnings),
                result.warnings,
            )
            self.assertEqual(result.mod_count, 0)

    def test_config_checkbox_off_exports_no_config_or_defaultconfigs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "config").mkdir(parents=True)
            (server / "config" / "example.toml").write_text("config = true", encoding="utf-8")
            (server / "defaultconfigs").mkdir()
            (server / "defaultconfigs" / "server.toml").write_text("defaults = true", encoding="utf-8")
            instance = SimpleNamespace(
                path=str(server),
                name="TestPack",
                minecraft_version="1.20.1",
                loader="vanilla",
                loader_version="",
            )
            output = root / "test.zip"
            result = client_export.export(instance, output, fmt="prism", include_config=False)
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
            self.assertFalse(any("/.minecraft/config/" in name for name in names))
            self.assertFalse(any("/.minecraft/defaultconfigs/" in name for name in names))
            self.assertFalse(any("defaultconfigs/ was excluded" in warning for warning in result.warnings))


    def test_vanilla_server_rejects_modded_client_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "client"
            (source / ".minecraft" / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.fabricmc.fabric-loader", "version": "0.16.10"},
                ],
            }), encoding="utf-8")
            instance = SimpleNamespace(
                path=str(server), name="VanillaServer",
                minecraft_version="1.21.1", loader="vanilla", loader_version="",
            )
            output = root / "client.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source)
            )
            self.assertFalse(result.ok)
            self.assertIn("server is vanilla", result.error)
            self.assertFalse(output.exists())

    def test_symlinked_config_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            external = root / "external-config"
            external.mkdir()
            (external / "private.toml").write_text("token = 'do-not-share'", encoding="utf-8")
            try:
                (server / "config").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            instance = SimpleNamespace(
                path=str(server), name="TestPack",
                minecraft_version="1.21.1", loader="vanilla", loader_version="",
            )
            output = root / "client.zip"
            result = client_export.export(instance, output, fmt="prism")
            self.assertFalse(result.ok)
            self.assertIn("symbolic-link directory", result.error)
            self.assertFalse(output.exists())

    def test_export_destination_inside_server_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "server"
            server.mkdir()
            protected = server / "server.properties"
            protected.write_text("level-name=h", encoding="utf-8")
            instance = SimpleNamespace(
                path=str(server), name="TestPack",
                minecraft_version="1.21.1", loader="vanilla", loader_version="",
            )
            result = client_export.export(instance, protected, fmt="prism")
            self.assertFalse(result.ok)
            self.assertIn("outside the server folder", result.error)
            self.assertEqual(protected.read_text(encoding="utf-8"), "level-name=h")

    def test_mod_jar_is_stored_without_recompression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            mods = server / "mods"
            mods.mkdir(parents=True)
            jar = mods / "example.jar"
            with zipfile.ZipFile(jar, "w") as jar_zip:
                jar_zip.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
                jar_zip.writestr("payload.dat", "x" * 5000)
            instance = SimpleNamespace(
                path=str(server), name="TestPack",
                minecraft_version="1.21.1", loader="vanilla", loader_version="",
            )
            output = root / "client.zip"
            result = client_export.export(instance, output, fmt="prism", include_config=False)
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                member = archive.getinfo("TestPack/.minecraft/mods/example.jar")
                self.assertEqual(member.compress_type, zipfile.ZIP_STORED)

    def test_unknown_mods_from_client_source_are_reported_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "client"
            mods = source / ".minecraft" / "mods"
            mods.mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            # A valid JAR without explicit environment metadata is intentionally unknown.
            with zipfile.ZipFile(mods / "mystery.jar", "w") as jar_zip:
                jar_zip.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            instance = SimpleNamespace(
                path=str(server), name="TestPack",
                minecraft_version="1.21.1", loader="neoforge", loader_version="21.1.1",
            )
            output = root / "client.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source), include_config=False
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.mod_count, 0)
            self.assertTrue(
                any("mystery.jar" in warning and "unknown client compatibility" in warning
                    for warning in result.warnings),
                result.warnings,
            )


    def test_export_destination_inside_client_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "client"
            source_mods = source / ".minecraft" / "mods"
            source_mods.mkdir(parents=True)
            metadata = source / "mmc-pack.json"
            metadata.write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            output = source / "player-pack.zip"
            instance = SimpleNamespace(
                path=str(server), name="TestPack",
                minecraft_version="1.21.1", loader="neoforge", loader_version="21.1.1",
            )
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source), include_config=False
            )
            self.assertFalse(result.ok)
            self.assertIn("outside the selected Prism source folder", result.error)
            self.assertFalse(output.exists())
            self.assertTrue(metadata.exists())

    def test_client_source_mods_are_deduplicated_by_hash_not_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server_mods = server / "mods"
            server_mods.mkdir(parents=True)
            source = root / "client"
            client_mods = source / ".minecraft" / "mods"
            client_mods.mkdir(parents=True)
            components = [
                {"uid": "net.minecraft", "version": "1.21.1"},
                {"uid": "net.neoforged", "version": "21.1.1"},
            ]
            (source / "mmc-pack.json").write_text(
                json.dumps({"formatVersion": 1, "components": components}), encoding="utf-8"
            )
            jar_bytes = None
            server_jar = server_mods / "shared-server-name.jar"
            with zipfile.ZipFile(server_jar, "w") as archive:
                archive.writestr("fabric.mod.json", json.dumps({
                    "schemaVersion": 1, "id": "shared", "version": "1.0", "environment": "*"
                }))
            jar_bytes = server_jar.read_bytes()
            (client_mods / "renamed-copy.jar").write_bytes(jar_bytes)
            instance = SimpleNamespace(
                path=str(server), name="TestPack",
                minecraft_version="1.21.1", loader="neoforge", loader_version="21.1.1",
            )
            output = root / "out.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source), include_config=False
            )
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                jar_paths = [n for n in archive.namelist() if n.endswith(".jar")]
            self.assertEqual(jar_paths, ["TestPack/.minecraft/mods/shared-server-name.jar"])


if __name__ == "__main__":
    unittest.main()
