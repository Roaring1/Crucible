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


    def test_shared_source_mod_missing_from_server_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_mods = root / "server" / "mods"
            server_mods.mkdir(parents=True)
            source_mods = root / "client" / ".minecraft" / "mods"
            source_mods.mkdir(parents=True)
            (root / "client" / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.fabricmc.fabric-loader", "version": "0.16.10"},
                ],
            }), encoding="utf-8")

            with zipfile.ZipFile(server_mods / "shared.jar", "w") as archive:
                archive.writestr("fabric.mod.json", json.dumps({
                    "schemaVersion": 1, "id": "shared", "version": "1.0.0", "environment": "*"
                }))
            with zipfile.ZipFile(source_mods / "shared-renamed.jar", "w") as archive:
                archive.writestr("fabric.mod.json", json.dumps({
                    "schemaVersion": 1, "id": "shared", "version": "2.0.0", "environment": "*"
                }))

            instance = SimpleNamespace(
                path=str(root / "server"), name="TestPack",
                minecraft_version="1.21.1", loader="fabric", loader_version="0.16.10",
            )
            output = root / "out.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(root / "client"), include_config=False
            )
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                jar_paths = [n for n in archive.namelist() if n.endswith(".jar")]
            self.assertEqual(jar_paths, ["TestPack/.minecraft/mods/shared.jar"])
            self.assertTrue(any(
                "shared-renamed.jar" in warning and "not added" in warning
                for warning in result.warnings
            ), result.warnings)



    def test_only_client_relevant_kubejs_folders_are_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            for relative, content in (
                ("kubejs/client_scripts/ui.js", "// client script"),
                ("kubejs/startup_scripts/registry.js", "// shared registry script"),
                ("kubejs/assets/kubejs/lang/en_us.json", '{"test":"value"}'),
                ("kubejs/server_scripts/recipes.js", "// server-only logic"),
                ("kubejs/data/recipe.json", '{"type":"server-data"}'),
                ("kubejs/exported/private-dump.json", '{"debug":"dump"}'),
            ):
                target = server / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )
            output = root / "kubejs-safety.zip"
            result = client_export.export(instance, output, fmt="prism")
            self.assertTrue(result.ok, result.error)
            warning_text = "\n".join(result.warnings)
            self.assertIn("Excluded KubeJS/server_scripts/", warning_text)
            self.assertIn("Excluded KubeJS/data/", warning_text)
            self.assertIn("Excluded KubeJS/exported/", warning_text)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                prefix = "CreatePlus/.minecraft/"
                self.assertIn(prefix + "kubejs/client_scripts/ui.js", names)
                self.assertIn(prefix + "kubejs/startup_scripts/registry.js", names)
                self.assertIn(prefix + "kubejs/assets/kubejs/lang/en_us.json", names)
                self.assertFalse(any("/kubejs/server_scripts/" in name for name in names))
                self.assertFalse(any("/kubejs/data/" in name for name in names))
                self.assertFalse(any("/kubejs/exported/" in name for name in names))


    def test_resourcepacks_are_excluded_by_default_and_included_only_by_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "working-prism"
            mc_root = source / ".minecraft"
            (mc_root / "mods").mkdir(parents=True)
            (mc_root / "resourcepacks").mkdir()
            (mc_root / "shaderpacks").mkdir()
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            (mc_root / "resourcepacks" / "ServerTextures.zip").write_bytes(b"synthetic resource pack")
            (mc_root / "shaderpacks" / "PersonalShader.zip").write_bytes(b"unrelated shader pack")
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )

            default_output = root / "without-resourcepacks.zip"
            default_result = client_export.export(
                instance, default_output, fmt="prism", client_source=str(source)
            )
            self.assertTrue(default_result.ok, default_result.error)
            self.assertTrue(any("resourcepacks/ content, but it was excluded by default" in warning
                                for warning in default_result.warnings), default_result.warnings)
            self.assertTrue(any("Shaderpacks from the selected Prism profile were excluded" in warning
                                for warning in default_result.warnings), default_result.warnings)
            with zipfile.ZipFile(default_output) as archive:
                default_names = set(archive.namelist())
            self.assertFalse(any("/resourcepacks/" in name for name in default_names))
            self.assertFalse(any("/shaderpacks/" in name for name in default_names))

            opted_output = root / "with-resourcepacks.zip"
            opted_result = client_export.export(
                instance, opted_output, fmt="prism", client_source=str(source),
                include_resourcepacks=True,
            )
            self.assertTrue(opted_result.ok, opted_result.error)
            self.assertTrue(any("resource packs from the selected Prism profile" in warning for warning in opted_result.warnings))
            with zipfile.ZipFile(opted_output) as archive:
                names = set(archive.namelist())
                resourcepack_info = archive.getinfo("CreatePlus/.minecraft/resourcepacks/ServerTextures.zip")
                self.assertEqual(resourcepack_info.compress_type, zipfile.ZIP_STORED)
            self.assertIn("CreatePlus/.minecraft/resourcepacks/ServerTextures.zip", names)
            self.assertFalse(any("/shaderpacks/" in name for name in names))

    def test_resourcepack_inclusion_requires_a_client_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "server"
            server.mkdir()
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )
            result = client_export.export(
                instance, Path(tmp) / "pack.zip", fmt="prism",
                include_resourcepacks=True,
            )
            self.assertFalse(result.ok)
            self.assertIn("Choose a matching Prism client source", result.error)

    def test_resourcepack_symlink_root_is_rejected_when_opted_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "working-prism"
            mc_root = source / ".minecraft"
            (mc_root / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            external = root / "resourcepacks"
            external.mkdir()
            try:
                (mc_root / "resourcepacks").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )
            result = client_export.export(
                instance, root / "linked-resourcepacks.zip", fmt="prism",
                client_source=str(source), include_resourcepacks=True,
            )
            self.assertFalse(result.ok)
            self.assertIn("symbolic-link resourcepacks/", result.error)


    def test_client_profile_config_is_merged_and_server_config_wins_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "config").mkdir(parents=True)
            (server / "config" / "shared.toml").write_text(
                "side = 'server-authoritative'\n", encoding="utf-8"
            )
            (server / "config" / "server-only.toml").write_text(
                "server_setting = true\n", encoding="utf-8"
            )

            source = root / "working-prism"
            mc_root = source / ".minecraft"
            (mc_root / "mods").mkdir(parents=True)
            (mc_root / "config").mkdir()
            (mc_root / "kubejs" / "client_scripts").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            (mc_root / "config" / "shared.toml").write_text(
                "side = 'client-different'\n", encoding="utf-8"
            )
            (mc_root / "config" / "client-only.toml").write_text(
                "api_key = 'client_profile_fake_secret_value_12345'\n",
                encoding="utf-8",
            )
            (mc_root / "kubejs" / "client_scripts" / "ui.js").write_text(
                "// client-only tested script\n", encoding="utf-8"
            )

            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )
            output = root / "CreatePlus - Player Pack - v1.zip"
            result = client_export.export(
                instance, output, fmt="prism", include_config=True,
                client_source=str(source),
            )
            self.assertTrue(result.ok, result.error)
            warning_text = "\n".join(result.warnings)
            self.assertIn("Client profile configuration differs from the server copy", warning_text)
            self.assertIn("config/shared.toml", warning_text)
            self.assertIn("credential assignment", warning_text)
            self.assertIn("config/client-only.toml", warning_text)
            self.assertNotIn("client_profile_fake_secret_value_12345", warning_text)

            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                prefix = "CreatePlus/.minecraft/"
                self.assertIn(prefix + "config/server-only.toml", names)
                self.assertIn(prefix + "config/client-only.toml", names)
                self.assertIn(prefix + "kubejs/client_scripts/ui.js", names)
                self.assertEqual(
                    archive.read(prefix + "config/shared.toml").decode("utf-8"),
                    "side = 'server-authoritative'\n",
                )
                manifest = json.loads(archive.read("CreatePlus/crucible-manifest.json"))
                manifest_paths = {row["path"] for row in manifest["files"]}
                self.assertIn(prefix + "config/client-only.toml", manifest_paths)
                self.assertIn(prefix + "kubejs/client_scripts/ui.js", manifest_paths)
                self.assertTrue(any(
                    "config/client-only.toml" in warning
                    for warning in manifest["privacyReview"]["warnings"]
                ))

    def test_nested_symlink_in_client_profile_config_is_skipped_and_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "working-prism"
            mc_root = source / ".minecraft"
            (mc_root / "mods").mkdir(parents=True)
            config = mc_root / "config"
            config.mkdir()
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            external = root / "outside.toml"
            external.write_text("api_key = 'must-not-follow-this-link'\n", encoding="utf-8")
            try:
                (config / "linked.toml").symlink_to(external)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )
            output = root / "source-config-symlink.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source)
            )
            self.assertTrue(result.ok, result.error)
            self.assertTrue(any(
                "Excluded symlink from client configuration: linked.toml" in warning
                for warning in result.warnings
            ), result.warnings)
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(any(name.endswith("/config/linked.toml") for name in archive.namelist()))
                manifest = json.loads(archive.read(result.manifest_path))
                self.assertFalse(any("linked.toml" in row["path"] for row in manifest["files"]))
            self.assertNotIn("must-not-follow-this-link", "\n".join(result.warnings))

    def test_symlinked_client_profile_config_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "working-prism"
            mc_root = source / ".minecraft"
            (mc_root / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            external = root / "external-config"
            external.mkdir()
            (external / "private.toml").write_text("secret = true\n", encoding="utf-8")
            try:
                (mc_root / "config").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )
            output = root / "linked-config-root.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source)
            )
            self.assertFalse(result.ok)
            self.assertIn("symbolic-link config/ directory", result.error)
            self.assertFalse(output.exists())

    def test_client_profile_config_is_not_copied_when_config_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "working-prism"
            mc_root = source / ".minecraft"
            (mc_root / "mods").mkdir(parents=True)
            (mc_root / "config").mkdir()
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.1"},
                ],
            }), encoding="utf-8")
            (mc_root / "config" / "client-only.toml").write_text(
                "personal_setting = true\n", encoding="utf-8"
            )
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.1",
            )
            output = root / "no-config.zip"
            result = client_export.export(
                instance, output, fmt="prism", include_config=False,
                client_source=str(source),
            )
            self.assertTrue(result.ok, result.error)
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(any("/config/" in name for name in archive.namelist()))


    def test_prism_client_loader_version_mismatch_reports_both_exact_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            server.mkdir()
            source = root / "working-prism"
            (source / "minecraft" / "mods").mkdir(parents=True)
            (source / "mmc-pack.json").write_text(json.dumps({
                "formatVersion": 1,
                "components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": "net.neoforged", "version": "21.1.265"},
                ],
            }), encoding="utf-8")
            instance = SimpleNamespace(
                path=str(server), name="CreatePlus", minecraft_version="1.21.1",
                loader="neoforge", loader_version="21.1.233",
            )
            output = root / "mismatched-loader.zip"
            result = client_export.export(
                instance, output, fmt="prism", client_source=str(source)
            )
            self.assertFalse(result.ok)
            self.assertIn("server expects NeoForge 21.1.233", result.error)
            self.assertIn("selected Prism profile uses NeoForge 21.1.265", result.error)
            self.assertIn("no archive was written", result.error)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
