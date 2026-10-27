"""Regression tests for bugs found while adding modpack updating.

Each test here corresponds to a defect observed on a real machine, not a
hypothetical one. None of them touch the network.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from crucible.data.instance_model import ServerInstance
from crucible.importers import prism
from crucible.importers.prism import PrismPackInfo, _apply_gtnh_hints
from crucible.mods import modrinth

# An entry copied verbatim from a live ~/.config/crucible/instances.json. Note
# fml_query_result, which older builds of ServerInstance did not know about.
LIVE_ENTRY = {
    "id": "c679d8cc-19ec-409c-a281-60ee5cd53fe7",
    "path": "/home/roaring/CrucibleServers/Main_GT_New_Horizons_2.8.4_Server_Java_17-25",
    "name": "GT_New_Horizons_2.8.4_Server_Java_17-25",
    "version": "",
    "pack_source": "directory",
    "minecraft_version": "",
    "loader": "",
    "loader_version": "",
    "prism_source": "/home/roaring/Downloads/GT_New_Horizons_2.8.4_Server_Java_17-25.zip",
    "notes": "added: downloads/worldedit, oreexcavation.\n\n",
    "java_args": "-Xms16384M -Xmx18432M",
    "fml_query_result": "ask",
    "color": "#c8903a",
    "tmux_session": "gt-new-horizons-284-server-java-17-25",
    "auto_restart": False,
    "created_at": "2026-07-22T08:51:47.992344",
    "last_started": "2026-08-18T01:20:41.887758",
}


class RegistryRoundTripTests(unittest.TestCase):
    """Bug A: a registry key the build did not know about was silently lost."""

    def test_live_entry_survives_a_round_trip(self):
        out = ServerInstance.from_dict(dict(LIVE_ENTRY)).to_dict()
        for key, value in LIVE_ENTRY.items():
            with self.subTest(key=key):
                self.assertIn(key, out)
                self.assertEqual(out[key], value)

    def test_fml_query_result_is_a_real_field(self):
        inst = ServerInstance.from_dict(dict(LIVE_ENTRY))
        self.assertEqual(inst.fml_query_result, "ask")
        self.assertEqual(inst.to_dict()["fml_query_result"], "ask")

    def test_default_is_ask_so_nothing_is_auto_confirmed(self):
        # /fml confirm permanently deletes removed content, so the default
        # must always keep a human in the loop.
        self.assertEqual(ServerInstance("/tmp/a", "a").fml_query_result, "ask")

    def test_unrecognised_value_falls_back_to_ask(self):
        entry = dict(LIVE_ENTRY, fml_query_result="yolo")
        self.assertEqual(ServerInstance.from_dict(entry).fml_query_result, "ask")

    def test_completely_unknown_keys_are_preserved(self):
        entry = dict(LIVE_ENTRY, some_future_field={"nested": [1, 2]})
        out = ServerInstance.from_dict(entry).to_dict()
        self.assertEqual(out["some_future_field"], {"nested": [1, 2]})

    def test_known_fields_win_over_stale_extras(self):
        inst = ServerInstance.from_dict(dict(LIVE_ENTRY))
        inst.extra["name"] = "should-not-appear"
        self.assertEqual(inst.to_dict()["name"], LIVE_ENTRY["name"])

    def test_whole_registry_file_round_trips(self):
        registry = {"version": 1, "instances": [LIVE_ENTRY]}
        rebuilt = [ServerInstance.from_dict(e).to_dict() for e in registry["instances"]]
        self.assertEqual(json.loads(json.dumps(rebuilt)), registry["instances"])


class ModrinthPlatformGuardTests(unittest.TestCase):
    """Bug B: empty loader/mc_version made Modrinth return any platform.

    On the live machine this recorded 'OreExcavation-NeoForge-1.21-1.16.14.jar'
    as an installed mod for a Forge 1.7.10 server.
    """

    def test_refuses_when_both_facets_are_unknown(self):
        with self.assertRaises(modrinth.ModrinthError) as ctx:
            modrinth.resolve_file("ODQeqCgz", loader="", mc_version="")
        message = str(ctx.exception)
        self.assertIn("Minecraft version", message)
        self.assertIn("mod loader", message)

    def test_refuses_when_only_loader_is_unknown(self):
        with self.assertRaises(modrinth.ModrinthError) as ctx:
            modrinth.resolve_file("ODQeqCgz", loader="", mc_version="1.7.10")
        self.assertIn("mod loader", str(ctx.exception))
        self.assertNotIn("Minecraft version", str(ctx.exception))

    def test_refuses_when_only_mc_version_is_unknown(self):
        with self.assertRaises(modrinth.ModrinthError) as ctx:
            modrinth.resolve_file("ODQeqCgz", loader="forge", mc_version="")
        self.assertIn("Minecraft version", str(ctx.exception))


class GtnhImportMetadataTests(unittest.TestCase):
    """Bug C: importing a GTNH server pack produced empty pack metadata."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crucible-gtnh-hints-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_hints_fill_blank_metadata(self):
        info = PrismPackInfo(source_type="directory")
        _apply_gtnh_hints(info, "GT_New_Horizons_2.8.4_Server_Java_17-25.zip")
        self.assertEqual(info.pack_version, "2.8.4")
        self.assertEqual(info.minecraft_version, "1.7.10")
        self.assertEqual(info.loader, "forge")

    def test_hints_never_overwrite_real_detection(self):
        info = PrismPackInfo(source_type="modrinth", pack_version="9.9.9",
                             minecraft_version="1.21.1", loader="neoforge")
        _apply_gtnh_hints(info, "GT_New_Horizons_2.8.4_Server_Java_17-25.zip")
        self.assertEqual(info.pack_version, "9.9.9")
        self.assertEqual(info.minecraft_version, "1.21.1")
        self.assertEqual(info.loader, "neoforge")

    def test_non_gtnh_names_are_left_alone(self):
        info = PrismPackInfo(source_type="directory")
        _apply_gtnh_hints(info, "Create Plus 1.21.1.zip", "some-random-folder")
        self.assertEqual(info.pack_version, "")
        self.assertEqual(info.loader, "")

    def test_detect_prism_source_on_a_gtnh_server_zip(self):
        archive = self.tmp / "GT_New_Horizons_2.8.4_Server_Java_17-25.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("mods/gregtech.jar", "x")
            z.writestr("config/gt.cfg", "x")
            z.writestr("startserver-java9.sh", "#!/bin/sh\n")
        plan = prism.detect_prism_source(archive)
        self.addCleanup(shutil.rmtree, plan.cleanup_dir, ignore_errors=True)
        self.assertEqual(plan.info.pack_version, "2.8.4")
        self.assertEqual(plan.info.minecraft_version, "1.7.10")
        self.assertEqual(plan.info.loader, "forge")

    def test_detect_prism_source_on_a_gtnh_directory(self):
        d = self.tmp / "Main_GT_New_Horizons_2.8.4_Server_Java_17-25"
        (d / "mods").mkdir(parents=True)
        plan = prism.detect_prism_source(d)
        self.assertEqual(plan.info.pack_version, "2.8.4")
        self.assertEqual(plan.info.loader, "forge")


class ExtractionLocationTests(unittest.TestCase):
    """_extract_archive can be pointed at a chosen filesystem.

    Without this, a ~420 MiB GTNH pack was always unpacked under /tmp and then
    copied across devices (and on a tmpfs /tmp, into RAM).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crucible-extract-dest-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_dest_parent_is_honoured(self):
        archive = self.tmp / "pack.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("mods/a.jar", "x")
        target_fs = self.tmp / "onthisdisk"
        target_fs.mkdir()
        extracted, warnings = prism._extract_archive(archive, target_fs)
        self.addCleanup(shutil.rmtree, extracted, ignore_errors=True)
        self.assertEqual(extracted.parent, target_fs)
        self.assertEqual(warnings, [])
        self.assertTrue((extracted / "mods" / "a.jar").is_file())

    def test_default_still_uses_the_system_temp_dir(self):
        archive = self.tmp / "pack2.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("mods/a.jar", "x")
        extracted, _ = prism._extract_archive(archive)
        self.addCleanup(shutil.rmtree, extracted, ignore_errors=True)
        self.assertTrue((extracted / "mods" / "a.jar").is_file())


if __name__ == "__main__":
    unittest.main()