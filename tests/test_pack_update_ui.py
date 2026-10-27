"""Tests for the GUI layer of the modpack updater.

Two layers, both headless:

  * crucible/ui/pack_update_model.py holds every decision the Update-pack and
    Rollback dialogs make and imports no PyQt, so it is exercised directly --
    including the cases that must refuse to plan anything at all.
  * crucible/ui/pack_update_dialog.py and the two tabs are checked against
    their source, the same way the rest of Crucible's Qt code is tested. The
    contracts asserted here (blocking work only inside worker closures,
    threads retired through thread_lifecycle, typing the version to arm the
    install) are the ones whose violation would corrupt a server folder or
    freeze the app, and no display is needed to check them.

No network, no Qt, no real downloads.
"""

from __future__ import annotations

import ast
import shutil
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from crucible.importers import pack_update as pu
from crucible.importers.pack_update import (
    ARCHIVE_CLIENT_PACK,
    ARCHIVE_SERVER_PACK,
    SOURCE_LOCAL_FILE,
    SOURCE_UNKNOWN,
    VARIANT_JAVA8,
    VARIANT_JAVA17,
    ArchiveProbe,
    PackIdentity,
    PackRelease,
    PackSource,
    UpdatePlan,
)
from crucible.ui import pack_update_model as model

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeInstance:
    """Stand-in for ServerInstance with just what the updater reads."""

    path: str = ""
    version: str = ""
    prism_source: str = ""
    name: str = "Main"
    id: str = "inst-1"
    minecraft_version: str = ""
    loader: str = ""

    def world_root_path(self) -> Path:
        return Path(self.path) / "world"


@dataclass
class FakeManager:
    """Registry stand-in: records saves, or raises what it was told to."""

    raises: BaseException | None = None
    saved: list = field(default_factory=list)

    def update_instance(self, instance) -> None:
        if self.raises is not None:
            raise self.raises
        self.saved.append(instance)


def _identity(**kw) -> PackIdentity:
    base = dict(provider="gtnh", version="2.8.4", variant=VARIANT_JAVA17,
                confidence="receipt")
    base.update(kw)
    return PackIdentity(**base)


def _release(version="2.9.0", channel="stable", both=True, java_max=25) -> PackRelease:
    base = "https://" + "example.invalid"
    urls = {VARIANT_JAVA17: f"{base}/{version}-17.zip"}
    if both:
        urls[VARIANT_JAVA8] = f"{base}/{version}-8.zip"
    return PackRelease(version=version, title="Stable release", channel=channel,
                       release_date="2026/09/01", max_java_version=java_max,
                       server_urls=urls,
                       changelog_url="https://example.invalid/changelog.md")


class ServerDirCase(unittest.TestCase):
    """A plausible installed pack on disk, plus a deterministic Java."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crucible-packui-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in ("mods", "world", "config", "downloads"):
            (self.tmp / name).mkdir()
        (self.tmp / "server.properties").write_text(
            "level-name=world\n", encoding="utf-8")
        (self.tmp / "java9args.txt").write_text("-Xmx6G\n", encoding="utf-8")
        self.inst = FakeInstance(path=str(self.tmp), version="2.8.4")

        java = mock.patch.object(pu, "installed_java_major", return_value=17)
        java.start()
        self.addCleanup(java.stop)

        ident = mock.patch.object(pu, "identify_pack", return_value=_identity())
        self.identify = ident.start()
        self.addCleanup(ident.stop)

    def _pack_zip(self, name="GT_New_Horizons_2.9.0_Server_Java_17-25.zip") -> Path:
        path = self.tmp / "downloads" / name
        path.write_bytes(b"PK\x03\x04not-really-a-zip")
        return path

    def _room_to_swap(self) -> bool:
        """plan_update blocks when a swap would not fit; skip strict
        'this plan is runnable' assertions on a nearly full disk."""
        try:
            return shutil.disk_usage(self.tmp).free > 3 * 1024 ** 3
        except OSError:
            return False


class IndexPreviewTests(ServerDirCase):
    """Following the pack's own release index."""

    def test_up_to_date_is_a_note_not_an_error(self):
        note = "GT New Horizons 2.8.4 (Java 17+) is up to date."
        with mock.patch.object(pu, "check_for_update",
                               return_value=(_identity(), None, [], note)):
            preview = model.build_preview(self.inst)
        self.assertEqual(preview.note, note)
        self.assertEqual(preview.error, "")
        self.assertIsNone(preview.target)
        self.assertIsNone(preview.plan)
        self.assertFalse(preview.ok)
        self.assertIn(note, "\n".join(model.describe_preview(preview)))

    def test_offline_check_is_reported_without_blowing_up(self):
        note = "Could not reach downloads.gtnewhorizons.com (no internet?)."
        with mock.patch.object(pu, "check_for_update",
                               return_value=(_identity(), None, [], note)):
            preview = model.build_preview(self.inst)
        self.assertEqual(preview.note, note)
        self.assertEqual(preview.error, "")
        self.assertFalse(preview.ok)

    def test_unknown_pack_still_returns_a_preview(self):
        unknown = _identity(provider="", version="", confidence="none",
                            evidence=["No receipt and no recognisable name."])
        note = "Crucible could not tell which modpack this instance is running."
        with mock.patch.object(pu, "identify_pack", return_value=unknown), \
                mock.patch.object(pu, "check_for_update",
                                  return_value=(unknown, None, [], note)):
            preview = model.build_preview(self.inst)
        text = "\n".join(model.describe_preview(preview))
        self.assertIn("unknown pack", text)
        self.assertIn("No receipt", text)
        self.assertEqual(preview.error, "")

    def test_newer_release_builds_a_runnable_plan(self):
        with mock.patch.object(
                pu, "check_for_update",
                return_value=(_identity(), _release(), [], "2.9.0 is available")):
            preview = model.build_preview(self.inst, running=False)

        self.assertEqual(preview.target.version, "2.9.0")
        self.assertEqual(preview.confirm_word, "2.9.0")
        self.assertFalse(preview.needs_confirmation)
        self.assertTrue(preview.plan.download_url.endswith("2.9.0-17.zip"))
        # config/ is pack content: the new pack supplies it, so it is
        # deliberately not carried over. Settings are.
        self.assertIn("world", preview.plan.preserve)
        self.assertIn("server.properties", preview.plan.preserve)
        self.assertNotIn("config", preview.plan.preserve)
        if self._room_to_swap():
            self.assertTrue(preview.ok, preview.blockers)

        text = "\n".join(model.describe_preview(preview))
        self.assertIn("Kept from the current install", text)
        self.assertIn("https://example.invalid/changelog.md", text)

    def test_running_server_is_a_blocker(self):
        with mock.patch.object(
                pu, "check_for_update",
                return_value=(_identity(), _release(), [], "newer")):
            preview = model.build_preview(self.inst, running=True)
        self.assertFalse(preview.ok)
        self.assertTrue(any("running" in b for b in preview.blockers))
        self.assertIn("Blockers", "\n".join(model.describe_preview(preview)))

    def test_unverifiable_run_state_fails_closed(self):
        with mock.patch.object(
                pu, "check_for_update",
                return_value=(_identity(), _release(), [], "newer")):
            preview = model.build_preview(self.inst, running=None)
        self.assertFalse(preview.ok)
        self.assertTrue(any("Could not verify" in b for b in preview.blockers))

    def test_named_version_is_looked_up_in_the_index(self):
        releases = [_release("2.8.5"), _release("2.9.0")]
        with mock.patch.object(pu, "fetch_releases", return_value=releases):
            preview = model.build_preview(self.inst, to_version="2.8.5")
        self.assertEqual(preview.target.version, "2.8.5")

    def test_missing_version_is_an_error_not_a_guess(self):
        with mock.patch.object(pu, "fetch_releases", return_value=[_release()]):
            preview = model.build_preview(self.inst, to_version="9.9.9")
        self.assertIn("not in the pack index", preview.error)
        self.assertIsNone(preview.plan)

    def test_index_failure_surfaces_the_reason(self):
        boom = pu.PackUpdateError("Could not reach the index (no internet?).")
        with mock.patch.object(pu, "fetch_releases", side_effect=boom):
            preview = model.build_preview(self.inst, to_version="2.9.0")
        self.assertEqual(preview.error, str(boom))
        self.assertIsNone(preview.plan)


class SuppliedSourcePreviewTests(ServerDirCase):
    """A link, a file, or a version string the owner supplied."""

    def test_unrecognised_source_stops_before_planning(self):
        source = PackSource(kind=SOURCE_UNKNOWN, raw="what is this",
                            questions=["Crucible cannot tell what that is."])
        with mock.patch.object(pu, "resolve_pack_source", return_value=source):
            preview = model.build_preview(self.inst, source_text="what is this")
        self.assertEqual(preview.error, "Crucible cannot tell what that is.")
        self.assertIsNone(preview.plan)

    def test_client_pack_is_refused_before_extraction(self):
        archive = self._pack_zip("GT_New_Horizons_2.9.0_Client.zip")
        source = PackSource(kind=SOURCE_LOCAL_FILE, path=str(archive),
                            filename=archive.name, confidence="high")
        probe = ArchiveProbe(
            path=str(archive), kind=ARCHIVE_CLIENT_PACK, version="2.9.0",
            problems=["That is a client pack, not a server pack."])
        with mock.patch.object(pu, "resolve_pack_source", return_value=source), \
                mock.patch.object(pu, "probe_archive", return_value=probe):
            preview = model.build_preview(self.inst, source_text=str(archive))
        self.assertEqual(preview.error,
                         "That is a client pack, not a server pack.")
        self.assertIsNone(preview.plan)
        self.assertIs(preview.probe, probe)
        self.assertIn("Cannot continue",
                      "\n".join(model.describe_preview(preview)))

    def test_local_server_pack_installs_from_the_file(self):
        archive = self._pack_zip()
        source = PackSource(kind=SOURCE_LOCAL_FILE, path=str(archive),
                            provider="gtnh", version="2.9.0",
                            variant=VARIANT_JAVA17, filename=archive.name,
                            confidence="high")
        probe = ArchiveProbe(path=str(archive), kind=ARCHIVE_SERVER_PACK,
                             version="2.9.0", variant=VARIANT_JAVA17,
                             mod_count=412, uncompressed_bytes=3 * 1024 ** 3)
        with mock.patch.object(pu, "resolve_pack_source", return_value=source), \
                mock.patch.object(pu, "probe_archive", return_value=probe):
            preview = model.build_preview(self.inst, source_text=str(archive))

        self.assertEqual(preview.local_archive, str(archive))
        self.assertEqual(preview.plan.local_archive, str(archive))
        self.assertEqual(preview.plan.download_url, "")
        self.assertFalse(preview.needs_confirmation)
        text = "\n".join(model.describe_preview(preview))
        self.assertIn("412 mods", text)
        self.assertIn(str(archive), text)

    def test_uncertain_source_asks_before_installing(self):
        archive = self._pack_zip("some-random-pack.zip")
        source = PackSource(kind=SOURCE_LOCAL_FILE, path=str(archive),
                            filename=archive.name, confidence="low",
                            questions=["Is this really a modpack server pack?"],
                            warnings=["Crucible has no version for this file."])
        probe = ArchiveProbe(path=str(archive), kind=ARCHIVE_SERVER_PACK,
                             mod_count=88)
        with mock.patch.object(pu, "resolve_pack_source", return_value=source), \
                mock.patch.object(pu, "probe_archive", return_value=probe):
            preview = model.build_preview(self.inst, source_text=str(archive))

        self.assertTrue(preview.needs_confirmation)
        self.assertIn("Is this really a modpack server pack?", preview.questions)
        text = "\n".join(model.describe_preview(preview))
        self.assertIn("Please confirm", text)
        self.assertIn("Crucible has no version for this file.", text)

    def test_supplied_version_label_can_be_corrected(self):
        archive = self._pack_zip()
        source = PackSource(kind=SOURCE_LOCAL_FILE, path=str(archive),
                            filename=archive.name, confidence="medium")
        probe = ArchiveProbe(path=str(archive), kind=ARCHIVE_SERVER_PACK)
        with mock.patch.object(pu, "resolve_pack_source", return_value=source), \
                mock.patch.object(pu, "probe_archive", return_value=probe):
            preview = model.build_preview(self.inst, source_text=str(archive),
                                          as_version="2.9.0-beta-3")
        self.assertEqual(preview.target.version, "2.9.0-beta-3")
        self.assertEqual(preview.target.channel, "beta")
        self.assertEqual(preview.confirm_word, "2.9.0-beta-3")


class RegistrySyncTests(unittest.TestCase):
    """What Crucible records after a successful swap."""

    def test_version_is_recorded_and_saved(self):
        inst = FakeInstance(path="/tmp/x", version="2.8.4")
        manager = FakeManager()
        note = model.sync_registry_after_update(manager, inst,
                                                to_version="2.9.0")
        self.assertEqual(inst.version, "2.9.0")
        self.assertEqual(manager.saved, [inst])
        self.assertIn("2.9.0", note)

    def test_gtnh_defaults_fill_only_what_is_missing(self):
        inst = FakeInstance(path="/tmp/x", version="2.8.4")
        model.sync_registry_after_update(FakeManager(), inst,
                                         to_version="2.9.0", provider="gtnh")
        self.assertEqual(inst.minecraft_version, "1.7.10")
        self.assertEqual(inst.loader, "forge")

        kept = FakeInstance(path="/tmp/y", minecraft_version="1.7.10-custom",
                            loader="cleanroom")
        model.sync_registry_after_update(FakeManager(), kept,
                                         to_version="2.9.0", provider="gtnh")
        self.assertEqual(kept.minecraft_version, "1.7.10-custom")
        self.assertEqual(kept.loader, "cleanroom")

    def test_unregistered_server_is_not_fatal(self):
        inst = FakeInstance(path="/tmp/x")
        note = model.sync_registry_after_update(
            FakeManager(raises=KeyError("inst-1")), inst, to_version="2.9.0")
        self.assertIn("not in the registry", note)
        self.assertEqual(inst.version, "2.9.0")

    def test_save_failure_is_reported_not_raised(self):
        inst = FakeInstance(path="/tmp/x")
        note = model.sync_registry_after_update(
            FakeManager(raises=OSError("disk gone")), inst, to_version="2.9.0")
        self.assertIn("Could not save", note)

    def test_no_manager_still_updates_the_live_object(self):
        inst = FakeInstance(path="/tmp/x")
        note = model.sync_registry_after_update(None, inst, to_version="2.9.0")
        self.assertEqual(inst.version, "2.9.0")
        self.assertIn("2.9.0", note)

    def test_empty_version_changes_nothing(self):
        inst = FakeInstance(path="/tmp/x", version="2.8.4")
        manager = FakeManager()
        self.assertEqual(
            model.sync_registry_after_update(manager, inst, to_version=""), "")
        self.assertEqual(inst.version, "2.8.4")
        self.assertEqual(manager.saved, [])


class RollbackModelTests(ServerDirCase):
    """Listing, describing and pruning kept installs."""

    def _kept(self, stamp: str) -> Path:
        path = self.tmp.parent / f"{self.tmp.name}.pre-update-{stamp}"
        path.mkdir()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def test_entries_come_back_newest_first(self):
        self._kept("20260101_010101")
        self._kept("20260303_120000")
        entries = model.rollback_entries(self.inst)
        self.assertEqual([e.stamp for e in entries],
                         ["20260303_120000", "20260101_010101"])
        self.assertIn("2026-03-03 12:00", entries[0].label())

    def test_prune_preview_splits_kept_from_doomed(self):
        self._kept("20260101_010101")
        self._kept("20260303_120000")
        kept, doomed = model.prune_preview(self.inst, keep=1)
        self.assertEqual([e.stamp for e in kept], ["20260303_120000"])
        self.assertEqual([e.stamp for e in doomed], ["20260101_010101"])

        kept, doomed = model.prune_preview(self.inst, keep=0)
        self.assertEqual(kept, [])
        self.assertEqual(len(doomed), 2)

    def test_listing_never_raises_on_a_bad_instance(self):
        self.assertEqual(model.rollback_entries(object()), [])

    def test_describe_rollback_is_explicit_about_the_world(self):
        entry = pu.PreviousInstall(path=str(self.tmp), stamp="20260303_120000",
                                   version="2.8.4")
        carried = "\n".join(model.describe_rollback(entry,
                                                   keep_current_world=True))
        restored = "\n".join(model.describe_rollback(entry,
                                                    keep_current_world=False))
        self.assertIn("CURRENT world", carried)
        self.assertIn("carries the damage back", carried)
        self.assertIn("restored with it", restored)
        self.assertIn("moved aside, not deleted", restored)


class BackupCallbackTests(ServerDirCase):
    """The mandatory pre-update backup, injected rather than imported."""

    def _plan(self) -> UpdatePlan:
        return UpdatePlan(instance_path=str(self.tmp), current=_identity(),
                          target=_release(), variant=VARIANT_JAVA17)

    def test_a_world_gets_a_labelled_backup(self):
        seen = {}

        class FakeBackupManager:
            def __init__(self, instance):
                seen["instance"] = instance

            def create_backup(self, slot_name=""):
                seen["slot"] = slot_name
                return Path("/tmp/backups/before-update.tar.zst")

        callback = model.backup_callback(self.inst, self._plan())
        self.assertIsNotNone(callback)
        # crucible.data.backup_manager imports PyQt6, so stand in for the
        # whole module: the callback must reach it by name at call time.
        stub = types.ModuleType("crucible.data.backup_manager")
        stub.BackupManager = FakeBackupManager
        with mock.patch.dict(sys.modules,
                             {"crucible.data.backup_manager": stub}):
            path = callback()

        self.assertEqual(path, "/tmp/backups/before-update.tar.zst")
        self.assertIs(seen["instance"], self.inst)
        self.assertIn("2.8.4", seen["slot"])
        self.assertIn("2.9.0", seen["slot"])

    def test_no_world_means_no_backup_step(self):
        shutil.rmtree(self.tmp / "world")
        self.assertIsNone(model.backup_callback(self.inst, self._plan()))


class HumanBytesTests(unittest.TestCase):
    def test_reads_like_a_person_wrote_it(self):
        self.assertEqual(model.human_bytes(0), "0 B")
        self.assertEqual(model.human_bytes(512), "512 B")
        self.assertEqual(model.human_bytes(2048), "2.0 KiB")
        self.assertEqual(model.human_bytes(5 * 1024 ** 3), "5.0 GiB")


class ReadinessReportTests(unittest.TestCase):
    """The post-update "is this ready to start?" summary."""

    class Ready:
        def readiness(self):
            return [
                {"key": "folder", "label": "Server folder exists",
                 "ok": True, "detail": "/srv/x", "fix": None},
                {"key": "eula", "label": "Minecraft EULA accepted",
                 "ok": True, "detail": "Accepted", "fix": None},
            ]

    class Broken:
        def readiness(self):
            return [
                {"key": "folder", "label": "Server folder exists",
                 "ok": True, "detail": "/srv/x", "fix": None},
                {"key": "eula", "label": "Minecraft EULA accepted",
                 "ok": False,
                 "detail": "eula=false -- the server will refuse to start",
                 "fix": "accept_eula"},
                {"key": "server", "label": "Server program is installed",
                 "ok": None, "detail": "", "fix": "install_server"},
            ]

    class Angry:
        def readiness(self):
            raise OSError("permission denied")

    def test_quiet_when_every_check_passed(self):
        self.assertEqual(model.readiness_report(self.Ready()), [])

    def test_names_each_problem_and_how_to_fix_it(self):
        lines = model.readiness_report(self.Broken())
        blob = "\n".join(lines)
        self.assertEqual(lines[0], "Not ready to start yet:")
        self.assertIn("Minecraft EULA accepted", blob)
        self.assertIn("refuse to start", blob)
        self.assertIn("crucible accept-eula", blob)
        self.assertIn("crucible install-server", blob)
        # A check that passed is never read back to the owner.
        self.assertNotIn("Server folder exists", blob)

    def test_unknown_is_flagged_without_calling_it_a_failure(self):
        marks = [ln for ln in model.readiness_report(self.Broken())
                 if ln.startswith("[")]
        self.assertTrue(any(ln.startswith("[!]") for ln in marks))
        self.assertTrue(any(ln.startswith("[?]") for ln in marks))

    def test_a_check_that_raises_never_hides_the_update_result(self):
        lines = model.readiness_report(self.Angry())
        self.assertEqual(len(lines), 1)
        self.assertIn("permission denied", lines[0])

    def test_cli_reports_readiness_after_an_update(self):
        source = (ROOT / "crucible/__main__.py").read_text(encoding="utf-8")
        self.assertIn("def readiness_report_for(", source)
        self.assertIn("report = readiness_report_for(inst)", source)
        self.assertIn("pack_update_model", source)

    def test_dialog_computes_readiness_on_the_worker(self):
        source = (ROOT / "crucible/ui/pack_update_dialog.py").read_text(
            encoding="utf-8")
        self.assertIn("model.readiness_report(instance)", source)


class UiPackageStaysImportableTests(unittest.TestCase):
    """The CLI imports crucible.ui.pack_update_model on headless boxes."""

    def test_ui_package_init_pulls_in_no_pyqt(self):
        init = (ROOT / "crucible/ui/__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("PyQt6", init)


class ModelStaysHeadlessTests(unittest.TestCase):
    def test_model_imports_no_pyqt(self):
        """The decision layer must stay testable on a box with no display."""
        source = (ROOT / "crucible/ui/pack_update_model.py").read_text(
            encoding="utf-8")
        self.assertNotIn("PyQt6", source)


class DialogContractTests(unittest.TestCase):
    """Source-level guards on the Qt dialogs."""

    DIALOG_PATH = ROOT / "crucible/ui/pack_update_dialog.py"
    BLOCKING = {
        "apply_update", "rollback_update", "prune_previous_installs",
        "build_preview", "download_pack", "fetch_releases", "probe_archive",
        "readiness_report",
    }

    @classmethod
    def setUpClass(cls):
        cls.source = cls.DIALOG_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def _blocking_calls_outside_workers(self, node) -> list:
        """Blocking calls reachable without going through a `job` closure."""
        found = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef) and child.name == "job":
                continue
            if isinstance(child, ast.Call):
                name = getattr(child.func, "attr",
                               getattr(child.func, "id", ""))
                if name in self.BLOCKING:
                    found.append(name)
            found.extend(self._blocking_calls_outside_workers(child))
        return found

    def test_blocking_work_only_runs_inside_worker_closures(self):
        self.assertEqual(self._blocking_calls_outside_workers(self.tree), [])

    def test_workers_run_on_their_own_thread(self):
        self.assertIn("moveToThread(self._thread)", self.source)
        self.assertIn("self._thread.started.connect(self._worker.run)",
                      self.source)

    def test_threads_are_retired_through_the_lifecycle_helper(self):
        self.assertIn("connect_thread_cleanup(self._thread, self._thread_finished)",
                      self.source)
        self.assertIn("self._worker.finished.connect(self._thread.quit)",
                      self.source)
        self.assertIn("self._worker.finished.connect(self._worker.deleteLater)",
                      self.source)
        # Blocking waits belong to the helper, never to the dialogs.
        self.assertNotIn("thread.wait(", self.source)

    def test_installing_requires_typing_the_target_version(self):
        self.assertIn("self._confirm.text().strip() != preview.confirm_word",
                      self.source)
        self.assertIn("typed == preview.confirm_word", self.source)

    def test_a_running_job_cannot_be_abandoned(self):
        self.assertIn("def _refuse_close", self.source)
        self.assertIn("def closeEvent", self.source)
        self.assertIn("def reject", self.source)

    def test_backups_are_delegated_not_reimplemented(self):
        self.assertNotIn("BackupManager", self.source)
        self.assertIn("model.backup_callback", self.source)

    def test_cancelling_is_offered_and_honoured(self):
        self.assertIn("threading.Event()", self.source)
        self.assertIn("cancel=cancel", self.source)


class TabWiringTests(unittest.TestCase):
    """The buttons that reach the dialogs."""

    @classmethod
    def setUpClass(cls):
        cls.setup_tab = (ROOT / "crucible/ui/tabs/setup_tab.py").read_text(
            encoding="utf-8")
        cls.mods_tab = (ROOT / "crucible/ui/tabs/mods_tab.py").read_text(
            encoding="utf-8")
        cls.panel = (ROOT / "crucible/ui/instance_panel.py").read_text(
            encoding="utf-8")

    def test_setup_tab_shows_the_update_section(self):
        self.assertIn("def _maybe_add_update_section", self.setup_tab)
        self.assertIn("self._maybe_add_update_section()", self.setup_tab)
        for label in ("Check for updates", "Update from a link or file",
                      "Roll back"):
            self.assertIn(label, self.setup_tab)

    def test_setup_tab_opens_both_dialogs(self):
        self.assertIn("from ..pack_update_dialog import PackUpdateDialog",
                      self.setup_tab)
        self.assertIn("from ..pack_update_dialog import RollbackDialog",
                      self.setup_tab)

    def test_setup_tab_refreshes_itself_afterwards(self):
        body = self.setup_tab.split("def _open_pack_updater", 1)[1].split(
            "def _check_pack_updates", 1)[0]
        self.assertIn("self._rebuild()", body)

    def test_rollback_button_is_disabled_with_nothing_to_restore(self):
        self.assertIn("rb_btn.setEnabled(bool(kept))", self.setup_tab)
        self.assertIn("list_previous_installs", self.setup_tab)

    def test_mods_tab_offers_a_pack_update(self):
        self.assertIn("Update pack", self.mods_tab)
        self.assertIn("def _open_pack_update", self.mods_tab)
        self.assertIn('setAccessibleName("Update modpack")', self.mods_tab)

    def test_mods_tab_keeps_the_two_managers_apart(self):
        """self._manager is the ModManager; the registry gets its own name."""
        self.assertIn("self._instance_manager = instance_manager",
                      self.mods_tab)
        self.assertIn("self._manager = ModManager(instance)", self.mods_tab)
        self.assertIn("manager=self._instance_manager", self.mods_tab)

    def test_panel_hands_the_registry_to_the_mods_tab(self):
        self.assertIn("ModsTab(self._manager)", self.panel)


if __name__ == "__main__":
    unittest.main()
