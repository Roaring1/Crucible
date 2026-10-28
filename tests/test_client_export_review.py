from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from crucible.exporters import client_export


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

    def test_symlinked_config_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            config = server / "config"
            config.mkdir(parents=True)
            outside = root / "outside.toml"
            outside.write_text("api_key = 'do not ship'\n", encoding="utf-8")
            (config / "linked.toml").symlink_to(outside)
            output = root / "symlink.zip"
            result = client_export.export(self._instance(server), output, fmt="prism")
            self.assertFalse(result.ok)
            self.assertIn("Symlinks are not exported", result.error)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
