"""Tests for the source-ingestion, archive-probing and rollback layer of
crucible.importers.pack_update.

No network. Everything is driven by real zip files built in a temp dir,
because the whole point of the probe is that it reads an actual archive index
rather than trusting a filename.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import dataclass
from pathlib import Path

from crucible.importers.pack_update import (
    ARCHIVE_CLIENT_PACK,
    ARCHIVE_MODPACK_INDEX,
    ARCHIVE_SERVER_PACK,
    SOURCE_DIRECT_URL,
    SOURCE_INDEX_URL,
    SOURCE_INDEX_VERSION,
    SOURCE_LOCAL_FILE,
    VARIANT_JAVA8,
    VARIANT_JAVA17,
    PackRelease,
    list_previous_installs,
    plan_update,
    probe_archive,
    prune_previous_installs,
    release_from_source,
    resolve_pack_source,
    rollback_update,
)

# The exact link the user pasted.
LIVE_URL = ("https://downloads.gtnewhorizons.com/ServerPacks/"
            "GT_New_Horizons_2.8.4_Server_Java_17-25.zip")
INDEX_URL = "https://downloads.gtnewhorizons.com/versions.json"
CLIENT_URL = ("https://downloads.gtnewhorizons.com/ClientPacks/"
              "GT_New_Horizons_2.8.4_Client_Java_8.zip")
PRISM_URL = ("https://downloads.gtnewhorizons.com/Multi_mc_downloads/"
             "GT_New_Horizons_2.8.4_Java_17-25.zip")


@dataclass
class FakeInstance:
    path: str = ""
    version: str = ""
    prism_source: str = ""


def server_pack_entries(extra=None):
    entries = {
        "mods/gregtech.jar": "a",
        "mods/nei.jar": "b",
        "config/gt.cfg": "cfg",
        "startserver-java9.sh": "#!/bin/sh\n",
        "java9args.txt": "-Xmx4G\n",
    }
    if extra:
        entries.update(extra)
    return entries


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crucible-source-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def zip_at(self, name, entries):
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p, "w") as z:
            for member, body in entries.items():
                z.writestr(member, body)
        return p


# Cleaning up whatever a human actually pastes

class InputCleanupTests(unittest.TestCase):
    def test_markdown_link_is_unwrapped(self):
        src = resolve_pack_source(f"[GTNH 2.8.4]({LIVE_URL})")
        self.assertEqual(src.kind, SOURCE_DIRECT_URL)
        self.assertEqual(src.url, LIVE_URL)
        self.assertEqual(src.version, "2.8.4")

    def test_angle_brackets_and_quotes_are_stripped(self):
        for wrapped in (f"<{LIVE_URL}>", f'"{LIVE_URL}"', f"'{LIVE_URL}'"):
            self.assertEqual(resolve_pack_source(wrapped).url, LIVE_URL, wrapped)

    def test_trailing_sentence_punctuation_is_stripped(self):
        self.assertEqual(resolve_pack_source(f"{LIVE_URL}.").url, LIVE_URL)
        self.assertEqual(resolve_pack_source(f"  {LIVE_URL},  ").url, LIVE_URL)

    def test_empty_input_asks_rather_than_guessing(self):
        src = resolve_pack_source("   ")
        self.assertFalse(src.usable)
        self.assertTrue(src.questions)


# URLs

class SourceUrlTests(unittest.TestCase):
    def test_the_users_actual_link_is_understood_completely(self):
        src = resolve_pack_source(LIVE_URL)
        self.assertEqual(src.kind, SOURCE_DIRECT_URL)
        self.assertEqual(src.provider, "gtnh")
        self.assertEqual(src.version, "2.8.4")
        self.assertEqual(src.variant, VARIANT_JAVA17)
        self.assertEqual(src.confidence, "high")
        self.assertEqual(src.questions, [])
        self.assertFalse(src.needs_confirmation)
        self.assertIn("2.8.4", src.label())

    def test_plain_http_is_refused(self):
        src = resolve_pack_source(LIVE_URL.replace("https://", "http://"))
        self.assertFalse(src.usable)
        self.assertTrue(any("HTTPS" in w for w in src.warnings))
        self.assertTrue(src.questions)

    def test_versions_index_is_recognised(self):
        src = resolve_pack_source(INDEX_URL)
        self.assertEqual(src.kind, SOURCE_INDEX_URL)
        self.assertEqual(src.provider, "gtnh")
        self.assertEqual(src.confidence, "high")

    def test_non_archive_link_asks_what_it_is(self):
        src = resolve_pack_source("https://example.com/downloads/page.html")
        self.assertFalse(src.usable)
        self.assertTrue(any("modpack" in q for q in src.questions))

    def test_client_pack_link_is_flagged_not_silently_installed(self):
        src = resolve_pack_source(CLIENT_URL)
        self.assertTrue(any("CLIENT" in w for w in src.warnings))
        self.assertTrue(src.questions)
        self.assertTrue(src.needs_confirmation)
        self.assertEqual(src.confidence, "low")

    def test_prism_launcher_pack_link_is_flagged(self):
        src = resolve_pack_source(PRISM_URL)
        self.assertTrue(src.warnings)
        self.assertTrue(src.needs_confirmation)

    def test_mrpack_link_is_flagged(self):
        src = resolve_pack_source("https://example.com/packs/cool_pack.mrpack")
        self.assertTrue(src.questions)
        self.assertTrue(src.needs_confirmation)

    def test_unknown_zip_name_still_usable_but_asks(self):
        src = resolve_pack_source("https://example.com/packs/some_pack.zip")
        self.assertEqual(src.kind, SOURCE_DIRECT_URL)
        self.assertTrue(src.questions)
        self.assertTrue(src.needs_confirmation)

    def test_java_variant_mismatch_is_called_out(self):
        src = resolve_pack_source(LIVE_URL, expected_variant=VARIANT_JAVA8)
        self.assertTrue(any("variant" in w for w in src.warnings))
        self.assertTrue(src.questions)
        # The pasted link is still what would be installed.
        self.assertEqual(src.url, LIVE_URL)


# Local paths and bare versions

class SourceLocalTests(TempDirCase):
    def test_recognised_local_zip(self):
        p = self.zip_at("GT_New_Horizons_2.8.4_Server_Java_17-25.zip",
                        server_pack_entries())
        src = resolve_pack_source(str(p))
        self.assertEqual(src.kind, SOURCE_LOCAL_FILE)
        self.assertEqual(src.provider, "gtnh")
        self.assertEqual(src.version, "2.8.4")
        self.assertEqual(src.confidence, "high")
        self.assertEqual(src.questions, [])

    def test_unrecognised_local_zip_asks(self):
        p = self.zip_at("mystery.zip", server_pack_entries())
        src = resolve_pack_source(str(p))
        self.assertEqual(src.kind, SOURCE_LOCAL_FILE)
        self.assertEqual(src.confidence, "low")
        self.assertTrue(src.questions)

    def test_directory_is_refused_with_a_pointer(self):
        d = self.tmp / "somefolder"
        d.mkdir()
        src = resolve_pack_source(str(d))
        self.assertFalse(src.usable)
        self.assertTrue(any("import" in q for q in src.questions))

    def test_non_archive_file_is_refused(self):
        p = self.tmp / "notes.txt"
        p.write_text("hello", encoding="utf-8")
        src = resolve_pack_source(str(p))
        self.assertFalse(src.usable)
        self.assertTrue(src.questions)

    def test_nonexistent_path_is_refused(self):
        src = resolve_pack_source(str(self.tmp / "nope" / "whatever.zip"))
        self.assertFalse(src.usable)
        self.assertTrue(src.questions)

    def test_bare_version_uses_the_index(self):
        src = resolve_pack_source("2.8.4")
        self.assertEqual(src.kind, SOURCE_INDEX_VERSION)
        self.assertEqual(src.version, "2.8.4")
        self.assertEqual(src.confidence, "high")

    def test_bare_prerelease_version_uses_the_index(self):
        src = resolve_pack_source("2.9.0-beta-3")
        self.assertEqual(src.kind, SOURCE_INDEX_VERSION)
        self.assertEqual(src.version, "2.9.0-beta-3")


# Looking inside the archive

class ArchiveProbeTests(TempDirCase):
    def test_server_pack_is_accepted(self):
        p = self.zip_at("pack.zip", server_pack_entries())
        probe = probe_archive(p)
        self.assertEqual(probe.kind, ARCHIVE_SERVER_PACK)
        self.assertEqual(probe.mod_count, 2)
        self.assertEqual(probe.variant, VARIANT_JAVA17)
        self.assertTrue(probe.start_scripts)
        self.assertTrue(probe.ok, probe.problems)
        self.assertIn("2 mods", probe.label())

    def test_pack_wrapped_in_a_folder_is_unwrapped(self):
        top = "GT_New_Horizons_2.8.4_Server_Java_17-25"
        entries = {f"{top}/{k}": v for k, v in server_pack_entries().items()}
        probe = probe_archive(self.zip_at("wrapped.zip", entries))
        self.assertEqual(probe.root_prefix, top)
        self.assertEqual(probe.kind, ARCHIVE_SERVER_PACK)
        self.assertEqual(probe.mod_count, 2)
        self.assertEqual(probe.version, "2.8.4")

    def test_mods_only_archive_is_not_mistaken_for_a_wrapper(self):
        # Regression: a lean pack whose only top-level entry is mods/ must not
        # have mods/ stripped as a wrapper folder.
        probe = probe_archive(self.zip_at(
            "lean.zip", {"mods/a.jar": "a", "mods/b.jar": "b"}))
        self.assertEqual(probe.root_prefix, "")
        self.assertEqual(probe.mod_count, 2)
        self.assertEqual(probe.kind, ARCHIVE_SERVER_PACK)

    def test_client_pack_is_rejected(self):
        probe = probe_archive(self.zip_at("client.zip", {
            "mods/gregtech.jar": "a",
            "resourcepacks/x.zip": "r",
            "options.txt": "fov:70",
            "saves/world/level.dat": "w",
        }))
        self.assertEqual(probe.kind, ARCHIVE_CLIENT_PACK)
        self.assertTrue(probe.problems)
        self.assertFalse(probe.ok)
        self.assertIn("client pack", probe.label())

    def test_modpack_index_without_mods_is_rejected(self):
        probe = probe_archive(self.zip_at("index.mrpack", {
            "modrinth.index.json": json.dumps({"files": []}),
            "overrides/config/x.cfg": "c",
        }))
        self.assertEqual(probe.kind, ARCHIVE_MODPACK_INDEX)
        self.assertTrue(any("lists mods" in p for p in probe.problems))

    def test_archive_with_no_mods_is_rejected(self):
        probe = probe_archive(self.zip_at("empty.zip", {
            "readme.txt": "nothing here", "docs/x.md": "y"}))
        self.assertTrue(any("server pack" in p for p in probe.problems))

    def test_non_zip_is_rejected(self):
        p = self.tmp / "broken.zip"
        p.write_text("this is not a zip at all", encoding="utf-8")
        probe = probe_archive(p)
        self.assertTrue(any("readable zip" in x for x in probe.problems))

    def test_missing_file_is_rejected(self):
        probe = probe_archive(self.tmp / "absent.zip")
        self.assertTrue(any("Not a file" in p for p in probe.problems))

    def test_version_read_from_a_shipped_changelog(self):
        probe = probe_archive(self.zip_at("anonymous.zip", server_pack_entries({
            "changelog from 2.8.3 to 2.8.4.md": "stuff",
        })))
        self.assertEqual(probe.version, "2.8.4")

    def test_client_folders_inside_a_server_pack_are_only_a_note(self):
        probe = probe_archive(self.zip_at("mixed.zip", server_pack_entries({
            "resourcepacks/x.zip": "r",
        })))
        self.assertEqual(probe.kind, ARCHIVE_SERVER_PACK)
        self.assertFalse(probe.problems)
        self.assertTrue(probe.notes)


# Turning a source into something the planner accepts

class ReleaseFromSourceTests(TempDirCase):
    def test_pasted_url_resolves_for_either_variant(self):
        rel = release_from_source(resolve_pack_source(LIVE_URL))
        self.assertEqual(rel.version, "2.8.4")
        self.assertEqual(rel.channel, "stable")
        self.assertEqual(rel.server_url(VARIANT_JAVA17), LIVE_URL)
        self.assertEqual(rel.server_url(VARIANT_JAVA8), LIVE_URL)

    def test_local_file_has_no_url_but_keeps_its_version(self):
        p = self.zip_at("anonymous.zip", server_pack_entries({
            "changelog from 2.8.3 to 2.8.4.md": "stuff"}))
        src = resolve_pack_source(str(p))
        rel = release_from_source(src, probe=probe_archive(p))
        self.assertEqual(rel.version, "2.8.4")
        self.assertEqual(rel.server_url(VARIANT_JAVA17), "")

    def test_unknown_pack_is_labelled_custom_not_invented(self):
        p = self.zip_at("mystery.zip", server_pack_entries())
        rel = release_from_source(resolve_pack_source(str(p)))
        self.assertEqual(rel.version, "custom")


class LocalArchivePlanTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.d = self.tmp / "GT_New_Horizons_2.8.4_Server_Java_17-25"
        for sub in (".crucible", "mods", "config", "evil world"):
            (self.d / sub).mkdir(parents=True)
        (self.d / "server.properties").write_text(
            "level-name=evil world\n", encoding="utf-8")
        self.inst = FakeInstance(path=str(self.d))
        self.arc = self.zip_at("GT_New_Horizons_2.9.0_Server_Java_17-25.zip",
                               server_pack_entries())

    def test_local_archive_is_a_runnable_plan_with_no_download(self):
        plan = plan_update(
            self.inst, PackRelease(version="2.9.0", channel="stable"),
            running=False, min_free_bytes=1024, local_archive=self.arc)
        self.assertTrue(plan.ok, plan.blockers)
        self.assertEqual(plan.download_url, "")
        self.assertEqual(plan.local_archive, str(self.arc))

    def test_missing_local_archive_blocks(self):
        plan = plan_update(
            self.inst, PackRelease(version="2.9.0", channel="stable"),
            running=False, min_free_bytes=1024,
            local_archive=self.tmp / "gone.zip")
        self.assertFalse(plan.ok)
        self.assertTrue(plan.blockers)


# Undo

class RollbackTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.live = self.tmp / "GTNH"
        self._tree(self.live, "new")
        self.prev = self.tmp / "GTNH.pre-update-20260101_120000"
        self._tree(self.prev, "old")
        (self.prev / ".crucible").mkdir()
        (self.prev / ".crucible" / "pack-provenance.json").write_text(
            json.dumps({"provider": "gtnh", "version": "2.8.4"}),
            encoding="utf-8")
        self.inst = FakeInstance(path=str(self.live))

    def _tree(self, root, tag):
        (root / "mods").mkdir(parents=True)
        (root / "evil world").mkdir()
        (root / "mods" / f"{tag}.jar").write_text(tag, encoding="utf-8")
        (root / "evil world" / "level.dat").write_text(
            f"{tag}-world", encoding="utf-8")
        (root / "server.properties").write_text(
            "level-name=evil world\n", encoding="utf-8")

    def _world(self):
        return (self.live / "evil world" / "level.dat").read_text(encoding="utf-8")

    def test_kept_installs_are_listed_newest_first(self):
        later = self.tmp / "GTNH.pre-update-20260201_090000"
        self._tree(later, "mid")
        found = list_previous_installs(self.inst)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].stamp, "20260201_090000")
        self.assertIn("2026-01-01", found[1].label())
        self.assertEqual(found[1].version, "2.8.4")

    def test_rollback_restores_the_old_pack_and_its_world(self):
        res = rollback_update(self.inst, self.prev, running=False)
        self.assertTrue(res.ok, res.failed_reason)
        self.assertTrue((self.live / "mods" / "old.jar").is_file())
        self.assertFalse((self.live / "mods" / "new.jar").exists())
        self.assertEqual(self._world(), "old-world")
        self.assertEqual(res.to_version, "2.8.4")

    def test_rollback_never_deletes_the_install_it_replaces(self):
        res = rollback_update(self.inst, self.prev, running=False)
        aside = Path(res.previous_dir)
        self.assertTrue(aside.is_dir())
        self.assertTrue((aside / "mods" / "new.jar").is_file())
        self.assertEqual(
            (aside / "evil world" / "level.dat").read_text(encoding="utf-8"),
            "new-world")

    def test_rollback_can_carry_the_current_world_back(self):
        res = rollback_update(self.inst, self.prev, running=False,
                              keep_current_world=True)
        self.assertTrue(res.ok, res.failed_reason)
        self.assertTrue((self.live / "mods" / "old.jar").is_file())
        self.assertEqual(self._world(), "new-world")

    def test_rollback_refuses_while_running(self):
        res = rollback_update(self.inst, self.prev, running=True)
        self.assertFalse(res.ok)
        self.assertIn("running", res.failed_reason)
        self.assertTrue((self.live / "mods" / "new.jar").is_file())

    def test_rollback_refuses_when_state_is_unknown(self):
        res = rollback_update(self.inst, self.prev, running=None)
        self.assertFalse(res.ok)
        self.assertTrue((self.live / "mods" / "new.jar").is_file())

    def test_rollback_refuses_a_foreign_directory(self):
        other = self.tmp / "unrelated"
        self._tree(other, "other")
        res = rollback_update(self.inst, other, running=False)
        self.assertFalse(res.ok)
        self.assertTrue((self.live / "mods" / "new.jar").is_file())

    def test_rollback_refuses_a_tree_with_no_mods(self):
        hollow = self.tmp / "GTNH.pre-update-19990101_000000"
        hollow.mkdir()
        res = rollback_update(self.inst, hollow, running=False)
        self.assertFalse(res.ok)
        self.assertIn("mods", res.failed_reason)

    def test_rollback_with_no_argument_picks_the_newest(self):
        later = self.tmp / "GTNH.pre-update-20260201_090000"
        self._tree(later, "mid")
        res = rollback_update(self.inst, running=False)
        self.assertTrue(res.ok, res.failed_reason)
        self.assertTrue((self.live / "mods" / "mid.jar").is_file())

    def test_rollback_reports_when_there_is_nothing_to_undo(self):
        shutil.rmtree(self.prev)
        res = rollback_update(self.inst, running=False)
        self.assertFalse(res.ok)
        self.assertIn("No kept pre-update install", res.failed_reason)

    def test_prune_keeps_the_requested_number(self):
        for stamp in ("20260201_090000", "20260301_090000"):
            self._tree(self.tmp / f"GTNH.pre-update-{stamp}", "x")
        self.assertEqual(len(list_previous_installs(self.inst)), 3)
        _freed, removed = prune_previous_installs(self.inst, keep=1)
        self.assertEqual(len(removed), 2)
        remaining = list_previous_installs(self.inst)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].stamp, "20260301_090000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
