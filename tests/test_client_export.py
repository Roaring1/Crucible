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


if __name__ == "__main__":
    unittest.main()
