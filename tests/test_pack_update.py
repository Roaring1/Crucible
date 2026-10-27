"""Tests for crucible.importers.pack_update.

No network. The GTNH index is exercised through a fixture that reproduces the
real versions.json shape, including the two traps that broke the first design:

  * the index is NOT sorted (2.4.0 precedes 2.4.1, betas trail stables), and
  * server-pack URLs cannot be pattern-constructed -- 2.8.4 ships
    '..._Server_Java_17-25.zip' while 2.9.0-beta-3 ships
    '.../betas/..._Server_Java_17-26.zip'.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from dataclasses import dataclass
from pathlib import Path

from crucible.importers.pack_update import (
    PRESERVE_DIRS,
    VARIANT_JAVA8,
    VARIANT_JAVA17,
    PackRelease,
    PackUpdateError,
    _staged_root,
    _world_names,
    apply_update,
    channel_of,
    check_for_update,
    compute_carry_mods,
    find_release,
    identify_pack,
    newest_release,
    parse_gtnh_index,
    parse_pack_version,
    plan_update,
    read_receipt,
)

_BASE = "https://downloads.gtnewhorizons.com"

# Mirrors the live index shape, deliberately in the wrong order.
INDEX = json.dumps({
    "2.9.0-beta-3": {
        "title": "Beta release",
        "description": '<p>Changelog: <a href="https://github.com/GTNewHorizons/DreamAssemblerXXL/blob/master/releases/changelogs/changelog%20from%202.8.4%20to%202.9.0-beta-3.md">here</a></p>',
        "releaseDate": "2026/09/06",
        "maxJavaVersion": 26,
        "client": {"java8Url": f"{_BASE}/ClientPacks/c.zip"},
        "mmc": {"java8Url": f"{_BASE}/Multi_mc_downloads/a.zip",
                "java17_2XUrl": f"{_BASE}/Multi_mc_downloads/b.zip"},
        "server": {
            "java8Url": f"{_BASE}/ServerPacks/betas/GT_New_Horizons_2.9.0-beta-3_Server_Java_8.zip",
            "java17_2XUrl": f"{_BASE}/ServerPacks/betas/GT_New_Horizons_2.9.0-beta-3_Server_Java_17-26.zip",
        },
    },
    "2.8.4": {
        "title": "Stable release",
        "description": '<a href="https://example.invalid/changelog-284.md">changelog</a>',
        "releaseDate": "2025/12/23",
        "maxJavaVersion": 25,
        "server": {
            "java8Url": f"{_BASE}/ServerPacks/GT_New_Horizons_2.8.4_Server_Java_8.zip",
            "java17_2XUrl": f"{_BASE}/ServerPacks/GT_New_Horizons_2.8.4_Server_Java_17-25.zip",
        },
    },
    "2.4.0": {
        "title": "Stable release", "releaseDate": "2023/01/01", "maxJavaVersion": 8,
        "server": {"java8Url": f"{_BASE}/ServerPacks/GT_New_Horizons_2.4.0_Server_Java_8.zip"},
    },
    "2.4.1": {
        "title": "Stable release", "releaseDate": "2023/02/01", "maxJavaVersion": 8,
        "server": {"java8Url": f"{_BASE}/ServerPacks/GT_New_Horizons_2.4.1_Server_Java_8.zip"},
    },
    "2.8.3": {
        "title": "Stable release", "releaseDate": "2025/08/01", "maxJavaVersion": 21,
        "server": {
            "java8Url": f"{_BASE}/ServerPacks/GT_New_Horizons_2.8.3_Server_Java_8.zip",
            "java17_2XUrl": f"{_BASE}/ServerPacks/GT_New_Horizons_2.8.3_Server_Java_17-21.zip",
        },
    },
    "April fools 2025": {
        "title": "Joke release", "releaseDate": "2025/04/01", "maxJavaVersion": 21,
        "server": {"java8Url": f"{_BASE}/ServerPacks/GT_New_Horizons_Aprils_Fool_2025_Edition_Server_Java_8.zip"},
    },
    "2.8.0-rc-1": {
        "title": "Release candidate", "releaseDate": "2025/05/01", "maxJavaVersion": 21,
        "server": {"java17_2XUrl": f"{_BASE}/ServerPacks/betas/GT_New_Horizons_2.8.0-rc-1_Server_Java_17-21.zip"},
    },
    "2.7.0-client-only": {
        "title": "Client only", "releaseDate": "2024/01/01",
        "client": {"java8Url": f"{_BASE}/ClientPacks/d.zip"},
    },
})


@dataclass
class FakeInstance:
    """Minimal stand-in: pack_update only reads these three attributes."""

    path: str = ""
    version: str = ""
    prism_source: str = ""


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crucible-packupdate-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------


class VersionParsingTests(unittest.TestCase):
    def test_ordering_across_channels(self):
        v = parse_pack_version
        self.assertLess(v("2.8.3"), v("2.8.4"))
        self.assertLess(v("2.8.4"), v("2.9.0-beta-1"))
        self.assertLess(v("2.9.0-beta-1"), v("2.9.0-beta-3"))
        self.assertLess(v("2.9.0-beta-3"), v("2.9.0-rc-1"))
        self.assertLess(v("2.9.0-rc-1"), v("2.9.0"))
        self.assertLess(v("2.4.0"), v("2.4.1"))
        self.assertLess(v("2.9.0"), v("2.10.0"))

    def test_prerelease_spelling_variants_are_equivalent(self):
        self.assertEqual(parse_pack_version("2.5.0-RC1"), parse_pack_version("2.5.0-rc-1"))
        self.assertEqual(parse_pack_version("2.7.0-RC-2"), parse_pack_version("2.7.0-rc2"))
        self.assertEqual(parse_pack_version("2.9.0-beta-3"), parse_pack_version("2.9.0 beta 3"))

    def test_two_component_versions(self):
        self.assertEqual(parse_pack_version("2.8"), parse_pack_version("2.8.0"))

    def test_unparseable_returns_none(self):
        for bad in ("April fools 2025", "", "latest", "nightly-2026-09-07", "2.7.0-client-only"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_pack_version(bad))

    def test_channel_detection(self):
        self.assertEqual(channel_of("2.8.4"), "stable")
        self.assertEqual(channel_of("2.9.0-beta-3"), "beta")
        self.assertEqual(channel_of("2.8.0-rc-1"), "rc")
        self.assertEqual(channel_of("April fools 2025"), "unknown")


class IndexParsingTests(unittest.TestCase):
    def setUp(self):
        self.releases = parse_gtnh_index(INDEX)

    def test_urls_are_read_verbatim_never_constructed(self):
        beta = find_release(self.releases, "2.9.0-beta-3")
        url = beta.server_url(VARIANT_JAVA17)
        self.assertIn("/ServerPacks/betas/", url)
        self.assertIn("Java_17-26", url)

        stable = find_release(self.releases, "2.8.4")
        stable_url = stable.server_url(VARIANT_JAVA17)
        self.assertIn("Java_17-25", stable_url)
        self.assertNotIn("/betas/", stable_url)

    def test_sorted_newest_first_despite_index_order(self):
        self.assertEqual(self.releases[0].version, "2.9.0-beta-3")
        keys = [parse_pack_version(r.version) for r in self.releases if parse_pack_version(r.version)]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_unparseable_version_kept_but_never_newest(self):
        self.assertIsNotNone(find_release(self.releases, "April fools 2025"))
        self.assertNotEqual(self.releases[0].version, "April fools 2025")
        self.assertEqual(self.releases[-1].version, "April fools 2025")

    def test_client_only_release_is_skipped(self):
        self.assertIsNone(find_release(self.releases, "2.7.0-client-only"))

    def test_newest_release_excludes_prereleases_by_default(self):
        self.assertEqual(newest_release(self.releases).version, "2.8.4")
        self.assertEqual(
            newest_release(self.releases, include_prerelease=True).version, "2.9.0-beta-3")

    def test_metadata_and_changelog(self):
        beta = find_release(self.releases, "2.9.0-beta-3")
        self.assertEqual(beta.channel, "beta")
        self.assertEqual(beta.max_java_version, 26)
        self.assertEqual(beta.release_date, "2026/09/06")
        self.assertIn("changelog", beta.changelog_url)
        self.assertIn("2.9.0-beta-3", beta.label())

    def test_variant_specific_availability(self):
        rc = find_release(self.releases, "2.8.0-rc-1")
        self.assertEqual(rc.server_url(VARIANT_JAVA8), "")
        self.assertTrue(rc.server_url(VARIANT_JAVA17))

    def test_non_https_urls_are_dropped(self):
        payload = json.dumps({"9.9.9": {"server": {"java8Url": "http://insecure.invalid/x.zip"}}})
        self.assertEqual(parse_gtnh_index(payload), [])

    def test_malformed_payloads_raise(self):
        for bad in ("not json", "[]", '"a string"'):
            with self.subTest(bad=bad), self.assertRaises(PackUpdateError):
                parse_gtnh_index(bad)


class IdentityTests(TempDirCase):
    def _server(self, name: str) -> Path:
        d = self.tmp / name
        (d / ".crucible").mkdir(parents=True)
        return d

    def test_folder_name_high_confidence(self):
        d = self._server("GT_New_Horizons_2.8.4_Server_Java_17-25")
        ident = identify_pack(FakeInstance(path=str(d)))
        self.assertEqual(ident.provider, "gtnh")
        self.assertEqual(ident.version, "2.8.4")
        self.assertEqual(ident.variant, VARIANT_JAVA17)
        self.assertEqual(ident.confidence, "high")
        self.assertTrue(ident.known)

    def test_prefixed_folder_still_matches(self):
        # This is the real folder name on the reporting machine.
        d = self._server("Main_GT_New_Horizons_2.8.4_Server_Java_17-25")
        self.assertEqual(identify_pack(FakeInstance(path=str(d))).version, "2.8.4")

    def test_java8_variant_detected(self):
        d = self._server("GT_New_Horizons_2.8.4_Server_Java_8")
        self.assertEqual(identify_pack(FakeInstance(path=str(d))).variant, VARIANT_JAVA8)

    def test_prism_source_used_when_folder_is_generic(self):
        d = self._server("Main")
        inst = FakeInstance(
            path=str(d),
            prism_source="/home/roaring/Downloads/GT_New_Horizons_2.8.4_Server_Java_17-25.zip",
        )
        ident = identify_pack(inst)
        self.assertEqual(ident.version, "2.8.4")
        self.assertEqual(ident.confidence, "high")

    def test_import_summary_source_used(self):
        d = self._server("plain")
        (d / ".crucible" / "import-summary.json").write_text(
            json.dumps({"source": "/x/GT_New_Horizons_2.8.4_Server_Java_17-25.zip"}),
            encoding="utf-8")
        self.assertEqual(identify_pack(FakeInstance(path=str(d))).version, "2.8.4")

    def test_receipt_overrides_stale_folder_name(self):
        d = self._server("GT_New_Horizons_2.8.4_Server_Java_17-25")
        (d / ".crucible" / "pack-provenance.json").write_text(
            json.dumps({"provider": "gtnh", "version": "2.9.0-beta-3", "variant": VARIANT_JAVA17}),
            encoding="utf-8")
        ident = identify_pack(FakeInstance(path=str(d)))
        self.assertEqual(ident.version, "2.9.0-beta-3")
        self.assertEqual(ident.confidence, "receipt")

    def test_shipped_changelog_filename_is_a_fallback(self):
        d = self._server("someserver")
        (d / "changelog from 2.8.3 to 2.8.4.md").write_text("x", encoding="utf-8")
        (d / "java9args.txt").write_text("x", encoding="utf-8")
        ident = identify_pack(FakeInstance(path=str(d)))
        self.assertEqual(ident.version, "2.8.4")
        self.assertEqual(ident.variant, VARIANT_JAVA17)
        self.assertEqual(ident.confidence, "guess")

    def test_registry_version_requires_gtnh_evidence_on_disk(self):
        d = self._server("Midtech")
        inst = FakeInstance(path=str(d), version="2.8.4")
        self.assertFalse(identify_pack(inst).known)
        (d / "lwjgl3ify-forgePatches.jar").write_text("x", encoding="utf-8")
        self.assertEqual(identify_pack(inst).version, "2.8.4")

    def test_unknown_pack_records_what_it_tried(self):
        d = self._server("Create")
        ident = identify_pack(FakeInstance(path=str(d)))
        self.assertFalse(ident.known)
        self.assertEqual(ident.label(), "unknown pack")
        self.assertTrue(ident.evidence)

    def test_corrupt_receipt_is_ignored(self):
        d = self._server("GT_New_Horizons_2.8.4_Server_Java_17-25")
        (d / ".crucible" / "pack-provenance.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(read_receipt(d), {})
        self.assertEqual(identify_pack(FakeInstance(path=str(d))).version, "2.8.4")


class WorldNameTests(TempDirCase):
    def test_level_name_with_space_and_dimensions(self):
        d = self.tmp / "srv"
        d.mkdir()
        for name in ("evil world", "evil world_nether"):
            (d / name).mkdir()
        (d / "server.properties").write_text(
            "#comment\nlevel-name=evil world\nmax-players=20\n", encoding="utf-8")
        self.assertEqual(_world_names(FakeInstance(path=str(d))),
                         ["evil world", "evil world_nether"])

    def test_defaults_to_world_and_skips_missing(self):
        d = self.tmp / "srv2"
        (d / "world").mkdir(parents=True)
        self.assertEqual(_world_names(FakeInstance(path=str(d))), ["world"])


class PreserveListTests(unittest.TestCase):
    def test_gtnh_progression_dirs_are_preserved(self):
        for name in ("visualprospecting", "journeymap", "serverutilities", "backups", ".crucible"):
            self.assertIn(name, PRESERVE_DIRS)

    def test_pack_owned_dirs_are_not_preserved(self):
        for name in ("mods", "config", "libraries", "scripts"):
            self.assertNotIn(name, PRESERVE_DIRS)


class PlanTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.d = self.tmp / "GT_New_Horizons_2.8.4_Server_Java_17-25"
        for sub in (".crucible", "mods", "config", "evil world", "journeymap", "visualprospecting"):
            (self.d / sub).mkdir(parents=True)
        (self.d / "server.properties").write_text("level-name=evil world\n", encoding="utf-8")
        (self.d / "ops.json").write_text("[]", encoding="utf-8")
        (self.d / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        self.inst = FakeInstance(path=str(self.d))
        self.releases = parse_gtnh_index(INDEX)

    def _plan(self, version, **kw):
        kw.setdefault("min_free_bytes", 1024)
        kw.setdefault("running", False)
        return plan_update(self.inst, find_release(self.releases, version), **kw)

    def test_happy_path_is_runnable(self):
        plan = self._plan("2.9.0-beta-3")
        self.assertTrue(plan.ok, plan.blockers)
        self.assertIn("Java_17-26", plan.download_url)
        self.assertIn("2.9.0-beta-3", plan.summary())

    def test_running_server_blocks(self):
        plan = self._plan("2.9.0-beta-3", running=True)
        self.assertFalse(plan.ok)
        self.assertTrue(any("running" in b for b in plan.blockers))

    def test_unknown_running_state_fails_closed(self):
        plan = self._plan("2.9.0-beta-3", running=None)
        self.assertFalse(plan.ok)
        self.assertTrue(any("Could not verify" in b for b in plan.blockers))

    def test_downgrade_blocked_unless_explicitly_allowed(self):
        self.assertFalse(self._plan("2.8.3").ok)
        self.assertTrue(self._plan("2.8.3", allow_downgrade=True).ok)

    def test_reinstall_is_allowed_with_a_warning(self):
        plan = self._plan("2.8.4")
        self.assertTrue(plan.ok)
        self.assertTrue(any("already installed" in w for w in plan.warnings))

    def test_major_jump_and_beta_warnings(self):
        plan = self._plan("2.9.0-beta-3")
        self.assertTrue(any("major version" in w for w in plan.warnings))
        self.assertTrue(any("beta release" in w for w in plan.warnings))

    def test_fml_confirm_guidance_always_present(self):
        plan = self._plan("2.9.0-beta-3")
        self.assertTrue(any("/fml confirm" in w for w in plan.warnings))

    def test_preserve_list_contents(self):
        plan = self._plan("2.9.0-beta-3")
        for expected in ("evil world", "journeymap", "visualprospecting",
                         "server.properties", "ops.json", "eula.txt", ".crucible"):
            self.assertIn(expected, plan.preserve)
        self.assertNotIn("mods", plan.preserve)
        self.assertNotIn("config", plan.preserve)

    def test_variant_without_a_pack_blocks_instead_of_silently_switching(self):
        release = PackRelease(
            version="3.0.0", channel="stable",
            server_urls={VARIANT_JAVA8: f"{_BASE}/ServerPacks/x_Server_Java_8.zip"},
        )
        plan = plan_update(self.inst, release, running=False, min_free_bytes=1024)
        self.assertFalse(plan.ok)
        self.assertTrue(any("java17_2X" in b for b in plan.blockers))

    def test_java_ceiling_warning(self):
        plan = self._plan("2.8.3", allow_downgrade=True, installed_java=26)
        self.assertTrue(any("at most Java 21" in w for w in plan.warnings))

    def test_no_java_warning_when_within_ceiling(self):
        plan = self._plan("2.9.0-beta-3", installed_java=25)
        self.assertFalse(any("at most Java" in w for w in plan.warnings))

    def test_insufficient_disk_blocks(self):
        plan = self._plan("2.9.0-beta-3", min_free_bytes=10 ** 16)
        self.assertFalse(plan.ok)
        self.assertTrue(any("disk space" in b for b in plan.blockers))

    def test_missing_world_warns(self):
        shutil.rmtree(self.d / "evil world")
        plan = self._plan("2.9.0-beta-3")
        self.assertTrue(any("No world folder" in w for w in plan.warnings))

    def test_unknown_current_version_warns_but_still_plans(self):
        plain = self.tmp / "mystery"
        (plain / "mods").mkdir(parents=True)
        plan = plan_update(FakeInstance(path=str(plain)),
                           find_release(self.releases, "2.9.0-beta-3"),
                           running=False, min_free_bytes=1024)
        self.assertTrue(any("could not confirm" in w for w in plan.warnings))


class CarryModTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.d = self.tmp / "srv"
        (self.d / "mods").mkdir(parents=True)
        (self.d / ".crucible").mkdir(parents=True)
        for jar in ("gregtech.jar", "OreExcavation-1.1.134.jar",
                    "worldedit-forge-mc1.7.10-6.1.1-dist.jar"):
            (self.d / "mods" / jar).write_text("x", encoding="utf-8")
        (self.d / "mods" / "ic2").mkdir()
        self.inst = FakeInstance(path=str(self.d))

    def _record_added(self, *names):
        (self.d / ".crucible" / "added-mods.json").write_text(
            json.dumps({n: {"title": n} for n in names}), encoding="utf-8")

    def test_without_baseline_only_crucible_installed_mods_are_carried(self):
        self._record_added("OreExcavation-1.1.134.jar")
        carry, notes = compute_carry_mods(self.inst)
        self.assertEqual(carry, ["OreExcavation-1.1.134.jar"])
        self.assertTrue(any("No pack baseline" in n for n in notes))

    def test_baseline_detects_hand_copied_jars(self):
        (self.d / ".crucible" / "pack-provenance.json").write_text(
            json.dumps({"provider": "gtnh", "version": "2.8.4",
                        "variant": VARIANT_JAVA17, "pack_mods": ["gregtech.jar"]}),
            encoding="utf-8")
        carry, notes = compute_carry_mods(self.inst)
        self.assertEqual(carry, ["OreExcavation-1.1.134.jar",
                                 "worldedit-forge-mc1.7.10-6.1.1-dist.jar"])
        self.assertTrue(any("recorded pack baseline" in n for n in notes))

    def test_nested_mod_dirs_are_never_carried(self):
        (self.d / ".crucible" / "pack-provenance.json").write_text(
            json.dumps({"provider": "gtnh", "version": "2.8.4", "pack_mods": []}),
            encoding="utf-8")
        carry, _ = compute_carry_mods(self.inst)
        self.assertNotIn("ic2", carry)

    def test_new_pack_diff_is_reported_when_no_baseline(self):
        self._record_added("OreExcavation-1.1.134.jar")
        _, notes = compute_carry_mods(self.inst, new_pack_mods={"gregtech.jar"})
        joined = " ".join(notes)
        self.assertIn("worldedit-forge-mc1.7.10-6.1.1-dist.jar", joined)

    def test_recorded_mod_that_is_gone_is_not_carried(self):
        # The live machine records a NeoForge jar that does not exist on disk.
        self._record_added("OreExcavation-NeoForge-1.21-1.16.14.jar")
        carry, _ = compute_carry_mods(self.inst)
        self.assertEqual(carry, [])


class StagedRootTests(TempDirCase):
    def test_flat_archive(self):
        root = self.tmp / "flat"
        (root / "mods").mkdir(parents=True)
        self.assertEqual(_staged_root(root), root)

    def test_single_wrapper_folder_is_unwrapped(self):
        root = self.tmp / "wrapped"
        (root / "inner" / "mods").mkdir(parents=True)
        self.assertEqual(_staged_root(root), root / "inner")


class ApplyUpdateTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.live = self.tmp / "GT_New_Horizons_2.8.4_Server_Java_17-25"
        (self.live / "mods").mkdir(parents=True)
        (self.live / "config").mkdir()
        (self.live / ".crucible").mkdir()
        (self.live / "evil world").mkdir()
        (self.live / "journeymap").mkdir()
        (self.live / "mods" / "gregtech-old.jar").write_text("old", encoding="utf-8")
        (self.live / "mods" / "OreExcavation-1.1.134.jar").write_text("owner", encoding="utf-8")
        (self.live / "config" / "gt.cfg").write_text("old-config", encoding="utf-8")
        (self.live / "evil world" / "level.dat").write_text("world-data", encoding="utf-8")
        (self.live / "journeymap" / "map.dat").write_text("map", encoding="utf-8")
        (self.live / "server.properties").write_text("level-name=evil world\n", encoding="utf-8")
        (self.live / "ops.json").write_text('["roaring"]', encoding="utf-8")
        (self.live / ".crucible" / "added-mods.json").write_text(
            json.dumps({"OreExcavation-1.1.134.jar": {"title": "Ore Excavation"}}),
            encoding="utf-8")

        self.pack = self.tmp / "newpack.zip"
        with zipfile.ZipFile(self.pack, "w") as z:
            z.writestr("mods/gregtech-new.jar", "new")
            z.writestr("mods/newmod.jar", "new2")
            z.writestr("config/gt.cfg", "new-config")
            z.writestr("startserver-java9.sh", "#!/bin/sh\necho hi\n")
            z.writestr("java9args.txt", "-Xmx4G\n")

        self.releases = parse_gtnh_index(INDEX)
        self.backups = []

    def _inst(self):
        return FakeInstance(path=str(self.live))

    def _plan(self, **kw):
        kw.setdefault("running", False)
        kw.setdefault("min_free_bytes", 1024)
        return plan_update(self._inst(), find_release(self.releases, "2.9.0-beta-3"), **kw)

    def _backup(self):
        self.backups.append("called")
        return str(self.tmp / "backup.zip")

    def _apply(self, **kw):
        kw.setdefault("archive", self.pack)
        kw.setdefault("backup_cb", self._backup)
        return apply_update(self._inst(), kw.pop("plan", None) or self._plan(), **kw)

    def _assert_live_untouched(self):
        self.assertTrue((self.live / "mods" / "gregtech-old.jar").is_file())
        self.assertEqual((self.live / "evil world" / "level.dat").read_text(encoding="utf-8"),
                         "world-data")
        self.assertEqual((self.live / "config" / "gt.cfg").read_text(encoding="utf-8"),
                         "old-config")

    def test_successful_stage_and_swap(self):
        res = self._apply()
        self.assertTrue(res.ok, res.failed_reason)
        self.assertEqual(self.backups, ["called"], "backup must run before anything else")

        # New pack content is live.
        self.assertTrue((self.live / "mods" / "gregtech-new.jar").is_file())
        self.assertTrue((self.live / "mods" / "newmod.jar").is_file())
        self.assertFalse((self.live / "mods" / "gregtech-old.jar").exists())
        self.assertEqual((self.live / "config" / "gt.cfg").read_text(encoding="utf-8"),
                         "new-config")

        # Owner data survived.
        self.assertEqual((self.live / "evil world" / "level.dat").read_text(encoding="utf-8"),
                         "world-data")
        self.assertEqual((self.live / "journeymap" / "map.dat").read_text(encoding="utf-8"), "map")
        self.assertIn("evil world", (self.live / "server.properties").read_text(encoding="utf-8"))
        self.assertIn("roaring", (self.live / "ops.json").read_text(encoding="utf-8"))

        # Owner mod carried forward.
        self.assertTrue((self.live / "mods" / "OreExcavation-1.1.134.jar").is_file())
        self.assertEqual(res.carried_mods, ["OreExcavation-1.1.134.jar"])

        # Start script stayed runnable.
        self.assertTrue(os.access(self.live / "startserver-java9.sh", os.X_OK))

        # Old tree kept for a manual rollback.
        self.assertTrue(Path(res.previous_dir).is_dir())
        self.assertTrue((Path(res.previous_dir) / "mods" / "gregtech-old.jar").is_file())

    def test_receipt_records_baseline_excluding_carried_mods(self):
        res = self._apply()
        self.assertTrue(res.ok, res.failed_reason)
        receipt = read_receipt(self.live)
        self.assertEqual(receipt["provider"], "gtnh")
        self.assertEqual(receipt["version"], "2.9.0-beta-3")
        self.assertEqual(receipt["previous_version"], "2.8.4")
        self.assertEqual(receipt["variant"], VARIANT_JAVA17)
        self.assertIn("gregtech-new.jar", receipt["pack_mods"])
        self.assertIn("newmod.jar", receipt["pack_mods"])
        self.assertNotIn("OreExcavation-1.1.134.jar", receipt["pack_mods"])
        self.assertEqual(receipt["carried_mods"], ["OreExcavation-1.1.134.jar"])

    def test_receipt_makes_the_next_update_identify_exactly(self):
        self.assertTrue(self._apply().ok)
        ident = identify_pack(self._inst())
        self.assertEqual(ident.version, "2.9.0-beta-3")
        self.assertEqual(ident.confidence, "receipt")
        # A hand-copied jar is now detectable because a baseline exists.
        (self.live / "mods" / "handcopied.jar").write_text("x", encoding="utf-8")
        carry, _ = compute_carry_mods(self._inst())
        self.assertIn("handcopied.jar", carry)

    def test_keep_previous_false_removes_old_tree(self):
        res = self._apply(keep_previous=False)
        self.assertTrue(res.ok, res.failed_reason)
        self.assertEqual(res.previous_dir, "")
        self.assertFalse(list(self.tmp.glob("*.pre-update-*")))

    def test_blocked_plan_is_refused_without_side_effects(self):
        res = self._apply(plan=self._plan(running=True))
        self.assertFalse(res.ok)
        self.assertEqual(self.backups, [], "a blocked plan must not take a backup")
        self._assert_live_untouched()

    def test_backup_failure_aborts_before_download(self):
        def boom():
            raise OSError("no space left on device")

        res = apply_update(self._inst(), self._plan(), backup_cb=boom, archive=self.pack)
        self.assertFalse(res.ok)
        self.assertIn("no space left", res.failed_reason)
        self._assert_live_untouched()

    def test_corrupt_archive_leaves_live_untouched(self):
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"this is not a zip file")
        res = self._apply(archive=bad)
        self.assertFalse(res.ok)
        self._assert_live_untouched()
        self.assertFalse(list(self.tmp.glob("*crucible-staging*")))

    def test_archive_without_mods_is_rejected(self):
        notpack = self.tmp / "notpack.zip"
        with zipfile.ZipFile(notpack, "w") as z:
            z.writestr("readme.txt", "hello")
        res = self._apply(archive=notpack)
        self.assertFalse(res.ok)
        self.assertIn("server pack", res.failed_reason)
        self._assert_live_untouched()

    def test_missing_archive_is_reported(self):
        res = self._apply(archive=self.tmp / "nope.zip")
        self.assertFalse(res.ok)
        self.assertIn("not found", res.failed_reason)
        self._assert_live_untouched()

    def test_zip_slip_member_never_escapes(self):
        evil = self.tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("mods/ok.jar", "ok")
            z.writestr("../escaped.txt", "pwned")
            z.writestr("../../escaped2.txt", "pwned")
        res = self._apply(archive=evil)
        self.assertTrue(res.ok, res.failed_reason)
        self.assertFalse((self.tmp / "escaped.txt").exists())
        self.assertFalse((self.tmp.parent / "escaped2.txt").exists())
        self.assertTrue(any("unsafe archive path" in m for m in res.messages))

    def test_cancellation_before_work_changes_nothing(self):
        cancel = threading.Event()
        cancel.set()
        res = self._apply(cancel=cancel)
        self.assertTrue(res.cancelled)
        self.assertFalse(res.ok)
        self.assertEqual(self.backups, [])
        self._assert_live_untouched()
        self.assertIn("cancelled", res.summary())

    def test_no_backup_callback_is_recorded_in_messages(self):
        res = apply_update(self._inst(), self._plan(), backup_cb=None, archive=self.pack)
        self.assertTrue(res.ok, res.failed_reason)
        self.assertTrue(any("no world backup" in m for m in res.messages))

    def test_log_callback_receives_progress(self):
        lines = []
        res = self._apply(log_cb=lines.append)
        self.assertTrue(res.ok, res.failed_reason)
        joined = " ".join(lines)
        self.assertIn("backup", joined.lower())
        self.assertIn("Extracting", joined)
        self.assertIn("Swapping", joined)


class CheckForUpdateTests(TempDirCase):
    def test_unknown_pack_short_circuits_without_network(self):
        d = self.tmp / "Create"
        d.mkdir()
        ident, newest, releases, note = check_for_update(FakeInstance(path=str(d)))
        self.assertFalse(ident.known)
        self.assertIsNone(newest)
        self.assertEqual(releases, [])
        self.assertIn("could not tell", note)


if __name__ == "__main__":
    unittest.main()
