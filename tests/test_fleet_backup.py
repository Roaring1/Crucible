from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from crucible.data.fleet_backup import (
    BackupAlreadyRunning,
    FleetBackupError,
    FleetBackupManager,
    fleet_operation_lock,
)


class FakeTmux:
    def __init__(self, statuses: dict[str, str] | None = None):
        self.statuses = statuses or {}
        self.console: dict[str, str] = {}
        self.commands: list[tuple[str, str]] = []
        self.fail_save_all: set[str] = set()
        self.already_save_off: set[str] = set()
        self.drop_save_off_ack: set[str] = set()
        self.readiness: dict[str, bool | None] = {}

    def get_status(self, instance):
        return self.statuses.get(instance.id, "stopped")

    def is_server_ready(self, instance):
        return self.readiness.get(instance.id, True)

    def send_command_result(self, instance, command):
        self.commands.append((instance.id, command))
        if command == "save-off":
            if instance.id in self.drop_save_off_ack:
                return True, "accepted; acknowledgement intentionally dropped"
            result = (
                "Automatic saving is already turned off"
                if instance.id in self.already_save_off
                else "Automatic saving is now disabled"
            )
        elif command == "save-all":
            result = (
                "Unable to save the game"
                if instance.id in self.fail_save_all
                else "Saved the game"
            )
        elif command == "save-on":
            result = "Automatic saving is now enabled"
        else:
            result = "OK"
        self.console[instance.id] = self.console.get(instance.id, "") + "\n" + result
        return True, "accepted"

    def capture_pane_tail(self, instance, max_lines=200, max_chars=8000):
        return self.console.get(instance.id, "")[-max_chars:]


def make_server(root: Path, name: str = "Example", instance_id: str = "id-1"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.properties").write_text(
        "server-port=25565\nlevel-name=main_world\n", encoding="utf-8"
    )
    (root / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    (root / "start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / "mods").mkdir(exist_ok=True)
    (root / "mods" / "example.jar").write_bytes(b"synthetic mod file")
    world = root / "main_world"
    (world / "region").mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"synthetic level metadata")
    (world / "region" / "r.0.0.mca").write_bytes(b"synthetic region data")
    # These files are deliberately excluded from a restorable snapshot.
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "latest.log").write_text("transient log", encoding="utf-8")
    return SimpleNamespace(
        id=instance_id,
        name=name,
        path=str(root),
        version="1.0",
        pack_source="manual",
        minecraft_version="1.21.1",
        loader="fabric",
        loader_version="0.16.10",
        tmux_session=f"session-{instance_id}",
        java_args="-Xms2G -Xmx4G",
        auto_restart=False,
        notes="",
        fml_query_result="ask",
        to_dict=lambda: {
            "id": instance_id,
            "name": name,
            "path": str(root),
            "java_args": "-Xms2G -Xmx4G",
        },
    )


def manager(tmp_path: Path, instances, tmux=None):
    return FleetBackupManager(
        instances,
        tmux or FakeTmux(),
        root=tmp_path / "fleet",
        legacy_backup_base=tmp_path / "legacy-backups",
        command_timeout_s=1,
        poll_interval_s=0,
        sleep=lambda _seconds: None,
    )


def test_legacy_source_inventory_surfaces_unassigned_and_mapped_sources(tmp_path):
    import uuid

    instance_id = str(uuid.uuid4())
    retired_id = str(uuid.uuid4())
    server = make_server(
        tmp_path / "server",
        name="username admin server",
        instance_id=instance_id,
    )
    base = tmp_path / "legacy-backups"
    current_dir = base / instance_id
    retired_dir = base / retired_id
    manual = base / "manual" / "username-admin-server-before-first-start"
    failed = base / "manual" / "failed-imports" / "username-admin-server.failed-import"
    for folder in (current_dir, retired_dir, manual / "mods", failed / "mods"):
        folder.mkdir(parents=True, exist_ok=True)
    (current_dir / "current-id-world.zip").write_bytes(
        b"already covered by archive migration"
    )
    (retired_dir / "retired-id-world.zip").write_bytes(
        b"needs explicit retired-ID mapping"
    )
    (manual / "server.properties").write_text("level-name=world\n", encoding="utf-8")
    (manual / "mods" / "old.jar").write_bytes(b"old server pack")
    (failed / "mods" / "failed.jar").write_bytes(b"partial import")
    (base / "username-admin-server-world-before-reset.tar.gz").write_bytes(
        b"legacy tar source"
    )
    (base / "other-server-world-backup.tar.gz").write_bytes(b"unassigned tar")

    manager = FleetBackupManager(
        [server],
        FakeTmux(),
        root=tmp_path / "fleet",
        legacy_backup_base=base,
    )
    inventory = manager.legacy_source_inventory()
    by_path = {item["path"]: item for item in inventory}

    assert "current-id-world.zip" not in by_path
    retired = by_path[retired_id]
    assert retired["kind"] == "retired_instance_backup_store"
    assert retired["status"] == "unassigned"
    assert retired["instance_id"] is None
    assert retired["files"] == 1

    snapshot = by_path["manual/username-admin-server-before-first-start"]
    assert snapshot["kind"] == "manual_server_snapshot"
    assert snapshot["status"] == "mapped_pending"
    assert snapshot["instance_id"] == instance_id
    assert snapshot["files"] == 2

    flat = by_path["username-admin-server-world-before-reset.tar.gz"]
    assert flat["kind"] == "flat_legacy_world_archive"
    assert flat["status"] == "mapped_pending"
    assert flat["instance_id"] == instance_id

    failed_record = by_path["manual/failed-imports/username-admin-server.failed-import"]
    assert failed_record["kind"] == "failed_import_data"
    assert failed_record["status"] == "review"
    assert by_path["other-server-world-backup.tar.gz"]["status"] == "unassigned"


def test_unassigned_retired_manual_and_flat_sources_are_preserved_then_pruned_after_two_generations(
    tmp_path,
):
    import uuid

    instance = make_server(tmp_path / "server")
    base = tmp_path / "legacy-backups"
    retired = base / str(uuid.uuid4())
    retired.mkdir(parents=True)
    retired_zip = retired / "unknown-world.zip"
    retired_zip.write_bytes(b"legacy retired world archive bytes")
    (retired / "unknown-world.zip.json").write_text(
        '{"slot_name":"Do not assign this to an active server"}',
        encoding="utf-8",
    )

    manual = base / "manual" / "username-admin-server-before-update"
    (manual / "mods").mkdir(parents=True)
    (manual / "server.properties").write_text(
        "level-name=old-world\n", encoding="utf-8"
    )
    (manual / "mods" / "legacy.jar").write_bytes(b"legacy manual snapshot mod")

    flat = base / "username-admin-server-world-before-reset.tar.gz"
    flat.write_bytes(b"legacy flat world archive bytes")

    failed = base / "manual" / "failed-imports" / "partial-import"
    (failed / "mods").mkdir(parents=True)
    (failed / "mods" / "partial.jar").write_bytes(b"partial import is not a backup")

    backups = FleetBackupManager(
        [instance],
        FakeTmux(),
        root=tmp_path / "fleet",
        legacy_backup_base=base,
    )
    assert len(backups.legacy_backup_blockers()) == 3

    first = backups.run_backup()
    first_verification = backups.verify_generation(first.path, deep=True)
    assert first_verification["ok"], first_verification["errors"]
    first_sources = {
        item["source"]: item
        for item in first_verification["manifest"]["legacy_sources"]
    }
    assert set(first_sources) == {
        retired.name,
        "manual/username-admin-server-before-update",
        flat.name,
    }

    retired_snapshot = first.path / "legacy-sources" / retired.name / retired_zip.name
    metadata_snapshot = (
        first.path / "legacy-sources" / retired.name / "unknown-world.zip.json"
    )
    manual_snapshot = (
        first.path
        / "legacy-sources"
        / "manual"
        / "username-admin-server-before-update"
        / "mods"
        / "legacy.jar"
    )
    flat_snapshot = first.path / "legacy-sources" / flat.name
    assert retired_snapshot.read_bytes() == retired_zip.read_bytes()
    assert (
        metadata_snapshot.read_bytes()
        == (retired / "unknown-world.zip.json").read_bytes()
    )
    assert manual_snapshot.read_bytes() == b"legacy manual snapshot mod"
    assert flat_snapshot.read_bytes() == flat.read_bytes()
    assert all(
        not path.stat().st_mode & 0o222
        for path in (
            retired_snapshot,
            metadata_snapshot,
            manual_snapshot,
            flat_snapshot,
        )
    )

    # One generation is not sufficient to retire the only external copy.
    assert retired.exists() and manual.exists() and flat.exists()
    assert failed.exists()

    second = backups.run_backup()
    assert not retired.exists()
    assert not manual.exists()
    assert not flat.exists()
    # Partial failed-import staging is deliberately not treated as a complete
    # backup and is never deleted by fleet retention.
    assert failed.exists()
    assert backups.legacy_backup_blockers() == []

    for generation in (first, second):
        verification = backups.verify_generation(generation.path, deep=True)
        assert verification["ok"], verification["errors"]
        assert backups.restore_server(
            generation.generation_id,
            instance.id,
            tmp_path / f"restored-{generation.generation_id}",
        ).is_dir()

    third = backups.run_backup()
    third_verification = backups.verify_generation(third.path, deep=True)
    assert third_verification["ok"], third_verification["errors"]
    carried = {
        item["source"] for item in third_verification["manifest"]["legacy_sources"]
    }
    assert carried == set(first_sources)
    migration = json.loads(backups.state_path.read_text(encoding="utf-8"))[
        "legacy_source_migration"
    ]
    assert migration["sources_pending"] == 0
    assert migration["excluded_partial_import_sources"]
    assert failed.exists()


def test_manual_legacy_source_is_not_retired_when_retained_snapshot_bytes_are_corrupt(
    tmp_path,
):
    instance = make_server(tmp_path / "server")
    base = tmp_path / "legacy-backups"
    manual = base / "manual" / "unassigned-server-snapshot"
    manual.mkdir(parents=True)
    source_file = manual / "server.properties"
    source_file.write_bytes(b"level-name=legacy_world\n")

    backups = FleetBackupManager(
        [instance],
        FakeTmux(),
        root=tmp_path / "fleet",
        legacy_backup_base=base,
    )
    first = backups.run_backup()
    first_record = next(
        item
        for item in backups._load_manifest(first.path)["legacy_sources"]
        if item["source"] == "manual/unassigned-server-snapshot"
    )
    first_file_record = first_record["files"][0]
    first_stored = first.path.joinpath(*Path(first_file_record["path"]).parts)
    original_bytes = source_file.read_bytes()

    first_stored.chmod(0o600)
    first_stored.write_bytes(b"X" + original_bytes[1:])
    first_stored.chmod(0o444)

    second = backups.run_backup()
    second_record = next(
        item
        for item in backups._load_manifest(second.path)["legacy_sources"]
        if item["source"] == "manual/unassigned-server-snapshot"
    )
    second_stored = second.path.joinpath(
        *Path(second_record["files"][0]["path"]).parts
    )

    assert second_stored.read_bytes() == original_bytes
    assert manual.exists()
    state = json.loads(backups.state_path.read_text(encoding="utf-8"))
    assert any(
        first.generation_id in error
        for error in state["legacy_source_migration"]["cleanup_errors"]
    )

    third = backups.run_backup()
    assert not first.path.exists()
    assert not manual.exists()
    assert backups.verify_generation(second.path, deep=True)["ok"]
    assert backups.verify_generation(third.path, deep=True)["ok"]


def test_legacy_source_added_after_hashing_is_not_deleted(tmp_path, monkeypatch):
    from crucible.data import fleet_backup as fleet_module

    instance = make_server(tmp_path / "server")
    base = tmp_path / "legacy-backups"
    manual = base / "manual" / "late-file-server"
    manual.mkdir(parents=True)
    source_file = manual / "server.properties"
    source_file.write_bytes(b"level-name=legacy_world\n")

    backups = FleetBackupManager(
        [instance],
        FakeTmux(),
        root=tmp_path / "fleet",
        legacy_backup_base=base,
    )

    # Keep the original source for the explicit cleanup exercise while still
    # creating two completed immutable generations.
    monkeypatch.setattr(
        FleetBackupManager,
        "_cleanup_migrated_legacy_sources",
        lambda self, retained: (0, 0, len(self.legacy_backup_blockers()), 1, []),
    )
    backups.run_backup()
    backups.run_backup()
    monkeypatch.undo()

    original_hash = fleet_module._hash_file
    injected = False
    late_file = manual / "added-after-inventory.txt"

    def add_file_after_source_hash(path):
        nonlocal injected
        digest = original_hash(path)
        if Path(path) == source_file and not injected:
            late_file.write_bytes(b"this file was never present in either backup")
            injected = True
        return digest

    monkeypatch.setattr(fleet_module, "_hash_file", add_file_after_source_hash)
    cleanup = backups._cleanup_migrated_legacy_sources(backups.list_generations())

    assert injected
    assert manual.is_dir()
    assert source_file.read_bytes() == b"level-name=legacy_world\n"
    assert late_file.read_bytes() == b"this file was never present in either backup"
    assert any("changed after verification" in error for error in cleanup[4])


def test_external_serverbackup_root_and_unregistered_rollbacks_join_two_generation_policy(
    tmp_path, monkeypatch
):
    instance = make_server(tmp_path / "CrucibleServers" / "active")
    home = tmp_path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    server_backups = home / "ServerBackups"
    gtnh_archive = server_backups / "GTNH" / "pre-consolidation" / "world.zip"
    gtnh_archive.parent.mkdir(parents=True)
    gtnh_archive.write_bytes(b"historical GTNH world archive")
    (gtnh_archive.parent / "manifest.json").write_text(
        '{"restore":"keep this provenance"}', encoding="utf-8"
    )

    rollback = home / "CrucibleServers" / "GTNH-Midtech.pre-update-20261102"
    (rollback / "world").mkdir(parents=True)
    (rollback / "server.properties").write_text("level-name=world\n", encoding="utf-8")
    (rollback / "world" / "level.dat").write_bytes(b"old rollback level metadata")

    legacy_base = home / "legacy-backups"
    legacy_base.mkdir()
    backups = FleetBackupManager(
        [instance],
        FakeTmux(),
        root=home / "fleet",
        legacy_backup_base=legacy_base,
    )
    external_sources = [
        (
            "external/ServerBackups",
            server_backups,
            "external_backup_root",
            "Historical backup root",
        ),
        (
            "external/CrucibleServers/GTNH-Midtech.pre-update-20261102",
            rollback,
            "unregistered_server_rollback",
            "Previous server installation",
        ),
    ]
    monkeypatch.setattr(
        backups, "_additional_legacy_backup_roots", lambda: external_sources
    )

    inventory = {item["path"]: item for item in backups.legacy_source_inventory()}
    assert set(inventory) == {
        "external/ServerBackups",
        "external/CrucibleServers/GTNH-Midtech.pre-update-20261102",
    }
    assert len(backups.legacy_backup_blockers()) == 2

    first = backups.run_backup()
    verified = backups.verify_generation(first.path, deep=True)
    assert verified["ok"], verified["errors"]
    records = {item["source"]: item for item in verified["manifest"]["legacy_sources"]}
    assert set(records) == set(inventory)
    assert (
        first.path
        / "legacy-sources"
        / "external"
        / "ServerBackups"
        / "GTNH"
        / "pre-consolidation"
        / "world.zip"
    ).read_bytes() == gtnh_archive.read_bytes()
    assert (
        first.path
        / "legacy-sources"
        / "external"
        / "CrucibleServers"
        / "GTNH-Midtech.pre-update-20261102"
        / "world"
        / "level.dat"
    ).read_bytes() == b"old rollback level metadata"
    assert server_backups.exists() and rollback.exists()

    restored_backups = backups.restore_legacy_source(
        first.generation_id,
        "external/ServerBackups",
        home / "restored-ServerBackups",
    )
    assert (
        restored_backups / "GTNH" / "pre-consolidation" / "world.zip"
    ).read_bytes() == gtnh_archive.read_bytes()
    restored_rollback = backups.restore_legacy_source(
        first.generation_id,
        "external/CrucibleServers/GTNH-Midtech.pre-update-20261102",
        home / "restored-GTNH-rollback",
    )
    assert (
        restored_rollback / "world" / "level.dat"
    ).read_bytes() == b"old rollback level metadata"

    with pytest.raises(FleetBackupError, match="registered server folder"):
        backups.restore_legacy_source(
            first.generation_id,
            "external/CrucibleServers/GTNH-Midtech.pre-update-20261102",
            Path(instance.path) / "restore-inside-live",
        )

    second = backups.run_backup()
    assert not server_backups.exists()
    assert not rollback.exists()
    assert backups.legacy_backup_blockers() == []
    for generation in (first, second):
        result = backups.verify_generation(generation.path, deep=True)
        assert result["ok"], result["errors"]

    third = backups.run_backup()
    carried = backups.verify_generation(third.path, deep=True)
    assert carried["ok"], carried["errors"]
    assert {item["source"] for item in carried["manifest"]["legacy_sources"]} == set(
        inventory
    )


def test_stopped_server_snapshot_deep_verify_and_restore(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    result = backups.run_backup()

    assert result.server_count == 1
    assert result.file_count > 0
    assert result.path.is_dir()
    verification = backups.verify_generation(result.path, deep=True)
    assert verification["ok"], verification["errors"]

    manifest_server = verification["manifest"]["instances"][0]
    assert manifest_server["capture_status"] == "stopped"
    assert manifest_server["registry"]["java_args"] == "-Xms2G -Xmx4G"
    assert {row["name"] for row in manifest_server["world_saves"]} == {"main_world"}

    restored = backups.restore_server(
        result.generation_id, instance.id, tmp_path / "restored"
    )
    assert (
        (restored / "server.properties")
        .read_text(encoding="utf-8")
        .startswith("server-port=")
    )
    assert (
        restored / "main_world" / "level.dat"
    ).read_bytes() == b"synthetic level metadata"
    assert (restored / "mods" / "example.jar").read_bytes() == b"synthetic mod file"
    assert not (restored / "logs").exists()
    assert not (tmp_path / "server" / "main_world" / "level.dat").samefile(
        restored / "main_world" / "level.dat"
    )


def test_unchanged_files_are_reused_only_from_prior_immutable_snapshot(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    second = backups.run_backup()

    first_file = (
        first.path
        / "servers"
        / backupsafe(instance.id)
        / "files"
        / "main_world"
        / "level.dat"
    )
    second_file = (
        second.path
        / "servers"
        / backupsafe(instance.id)
        / "files"
        / "main_world"
        / "level.dat"
    )
    source_file = Path(instance.path) / "main_world" / "level.dat"

    assert second_file.stat().st_ino == first_file.stat().st_ino
    assert second_file.stat().st_ino != source_file.stat().st_ino
    assert not (second_file.stat().st_mode & 0o222)
    assert second.bytes_reused > 0
    assert backups.verify_generation(second.path, deep=True)["ok"]


def backupsafe(instance_id: str) -> str:
    import hashlib

    return hashlib.sha256(instance_id.encode("utf-8")).hexdigest()


def test_retention_is_exactly_two_generations_after_successful_rotation(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])

    first = backups.run_backup()
    (Path(instance.path) / "main_world" / "region" / "r.0.0.mca").write_bytes(
        b"changed once"
    )
    second = backups.run_backup()
    (Path(instance.path) / "main_world" / "region" / "r.0.0.mca").write_bytes(
        b"changed twice"
    )
    third = backups.run_backup()

    retained = backups.list_generations()
    assert [row.generation_id for row in retained] == [
        third.generation_id,
        second.generation_id,
    ]
    assert not first.path.exists()
    assert backups.verify_generation(second.path, deep=True)["ok"]
    assert backups.verify_generation(third.path, deep=True)["ok"]


def test_failed_partial_fleet_cycle_keeps_both_prior_generations(tmp_path):
    first = make_server(tmp_path / "server-a", instance_id="a")
    second = make_server(tmp_path / "server-b", instance_id="b")
    tmux = FakeTmux()
    backups = manager(tmp_path, [first, second], tmux)
    one = backups.run_backup()
    two = backups.run_backup()

    tmux.statuses["b"] = "unknown"
    with pytest.raises(FleetBackupError, match="unknown"):
        backups.run_backup()

    retained = backups.list_generations()
    assert [row.generation_id for row in retained] == [
        two.generation_id,
        one.generation_id,
    ]
    assert backups.verify_generation(one.path, deep=True)["ok"]
    assert backups.verify_generation(two.path, deep=True)["ok"]
    assert not list(backups.staging_dir.iterdir())


def test_directory_fsync_failure_keeps_prior_generations_and_cleans_staging(
    tmp_path, monkeypatch
):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    second = backups.run_backup()

    def fail_fsync(_path):
        raise FleetBackupError("simulated directory fsync failure")

    monkeypatch.setattr(
        FleetBackupManager,
        "_fsync_directory_strict",
        staticmethod(fail_fsync),
    )
    with pytest.raises(FleetBackupError, match="simulated directory fsync failure"):
        backups.run_backup()

    assert first.path.exists()
    assert second.path.exists()
    assert [
        row.generation_id for row in backups.list_generations()
    ] == [second.generation_id, first.generation_id]
    assert not list(backups.staging_dir.iterdir())


def test_post_rename_fsync_failure_rolls_back_unpublished_generation(
    tmp_path, monkeypatch
):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    second = backups.run_backup()
    original_fsync = FleetBackupManager._fsync_directory_strict

    def fail_retained_parent(path):
        if path == backups.generations_dir:
            raise FleetBackupError("simulated post-rename parent fsync failure")
        original_fsync(path)

    monkeypatch.setattr(
        FleetBackupManager,
        "_fsync_directory_strict",
        staticmethod(fail_retained_parent),
    )
    with pytest.raises(
        FleetBackupError, match="simulated post-rename parent fsync failure"
    ):
        backups.run_backup()

    assert first.path.exists()
    assert second.path.exists()
    assert sorted(path.name for path in backups.generations_dir.iterdir()) == sorted(
        [first.generation_id, second.generation_id]
    )
    assert [
        row.generation_id for row in backups.list_generations()
    ] == [second.generation_id, first.generation_id]
    assert backups.generation_issues() == []
    assert not list(backups.staging_dir.iterdir())


@pytest.mark.parametrize("readiness", [False, None])
def test_running_but_not_confirmed_ready_server_is_deferred_before_console_commands(
    tmp_path, readiness
):
    instance = make_server(tmp_path / "server")
    tmux = FakeTmux({instance.id: "running"})
    tmux.readiness[instance.id] = readiness
    backups = manager(tmp_path, [instance], tmux)

    with pytest.raises(FleetBackupError, match="not confirmed ready|readiness could not be verified"):
        backups.run_backup()

    assert tmux.commands == []
    assert backups.list_generations() == []


def test_running_server_is_flushed_quiesced_and_autosave_restored(tmp_path):
    instance = make_server(tmp_path / "server")
    tmux = FakeTmux({instance.id: "running"})
    backups = manager(tmp_path, [instance], tmux)

    result = backups.run_backup()
    assert [command for _id, command in tmux.commands] == [
        "save-off",
        "save-all",
        "save-on",
    ]
    assert backups.verify_generation(result.path, deep=True)["ok"]
    assert backups.list_generations()[0].generation_id == result.generation_id


def test_running_server_that_was_already_save_off_is_left_that_way(tmp_path):
    instance = make_server(tmp_path / "server")
    tmux = FakeTmux({instance.id: "running"})
    tmux.already_save_off.add(instance.id)
    backups = manager(tmp_path, [instance], tmux)

    backups.run_backup()
    assert [command for _id, command in tmux.commands] == ["save-off", "save-all"]


def test_missing_save_off_ack_attempts_save_on_recovery_and_commits_nothing(tmp_path):
    instance = make_server(tmp_path / "server")
    tmux = FakeTmux({instance.id: "running"})
    tmux.drop_save_off_ack.add(instance.id)
    backups = manager(tmp_path, [instance], tmux)
    backups.command_timeout_s = 0.01

    with pytest.raises(FleetBackupError, match="sent save-on as a safety recovery"):
        backups.run_backup()

    assert [command for _id, command in tmux.commands] == ["save-off", "save-on"]
    assert "Automatic saving is now enabled" in tmux.console[instance.id]
    assert backups.list_generations() == []


def test_save_failure_attempts_to_restore_autosave_and_commits_nothing(tmp_path):
    instance = make_server(tmp_path / "server")
    tmux = FakeTmux({instance.id: "running"})
    tmux.fail_save_all.add(instance.id)
    backups = manager(tmp_path, [instance], tmux)

    with pytest.raises(FleetBackupError, match="failed save"):
        backups.run_backup()
    assert [command for _id, command in tmux.commands] == [
        "save-off",
        "save-all",
        "save-on",
    ]
    assert backups.list_generations() == []


def test_pane_delta_refuses_to_scan_a_replaced_tail_for_stale_confirmation():
    before = "older console output\nAutomatic saving is now disabled"
    after = "replacement pane history\nAutomatic saving is now disabled"

    assert FleetBackupManager._pane_delta(before, after) == ""


def test_unknown_or_unmanaged_server_blocks_entire_fleet_generation(tmp_path):
    instance = make_server(tmp_path / "server")
    for state in ("unknown", "unmanaged", "missing", "tmux_missing"):
        backups = manager(tmp_path / state, [instance], FakeTmux({instance.id: state}))
        with pytest.raises(FleetBackupError, match="status"):
            backups.run_backup()
        assert backups.list_generations() == []


def test_fleet_backup_lock_prevents_overlapping_backup_publishers(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    backups.root.mkdir(parents=True, exist_ok=True)
    backups.generations_dir.mkdir(parents=True, exist_ok=True)
    backups.staging_dir.mkdir(parents=True, exist_ok=True)
    first_lock = backups._acquire_lock()
    try:
        with pytest.raises(BackupAlreadyRunning):
            backups.run_backup(operation_lock_held=True)
    finally:
        first_lock.close()


def test_global_operation_lock_prevents_backup_during_world_or_pack_mutation(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])

    with (
        fleet_operation_lock(backups.root),
        pytest.raises(BackupAlreadyRunning, match="world operation or modpack update"),
    ):
        backups.run_backup()

    # A normal cycle can proceed once the compound operation releases the lock.
    result = backups.run_backup()
    assert backups.verify_generation(result.path, deep=True)["ok"]


def test_server_without_generated_world_is_still_backed_up(tmp_path):
    root = tmp_path / "new-server"
    root.mkdir()
    (root / "server.properties").write_text(
        "server-port=25565\nlevel-name=brand-new-world\n", encoding="utf-8"
    )
    (root / "start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    instance = SimpleNamespace(
        id="new",
        name="New server",
        path=str(root),
        version="",
        minecraft_version="1.21.1",
        loader="vanilla",
        loader_version="",
        tmux_session="new",
    )
    backups = manager(tmp_path, [instance])

    result = backups.run_backup()
    verification = backups.verify_generation(result.path, deep=True)
    assert verification["ok"], verification["errors"]
    assert verification["manifest"]["instances"][0]["world_saves"] == []


def test_symbolic_links_are_reported_but_not_followed(tmp_path):
    instance = make_server(tmp_path / "server")
    outside = tmp_path / "outside"
    outside.write_text("private", encoding="utf-8")
    link = Path(instance.path) / "outside-link"
    link.symlink_to(outside)

    backups = manager(tmp_path, [instance])
    result = backups.run_backup()
    verification = backups.verify_generation(result.path, deep=True)
    assert verification["ok"], verification["errors"]
    entry = verification["manifest"]["instances"][0]
    assert not any(row["path"] == "outside-link" for row in entry["files"])
    assert any(row["path"] == "outside-link" for row in entry["excluded"])


def test_restore_refuses_target_inside_a_registered_live_server(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    result = backups.run_backup()
    marker = Path(instance.path) / "server.properties"
    before = marker.read_bytes()
    target = Path(instance.path) / "recovery-copy"

    with pytest.raises(FleetBackupError, match="registered server folder"):
        backups.restore_server(result.generation_id, instance.id, target)

    assert not target.exists()
    assert marker.read_bytes() == before


def test_restore_refuses_symlinked_target_parent(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    result = backups.run_backup()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "restore-link"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(FleetBackupError, match="symbolic link"):
        backups.restore_server(
            result.generation_id, instance.id, linked_parent / "restored"
        )

    assert list(outside.iterdir()) == []


def test_verify_generation_rejects_symlinked_server_files_root(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    result = backups.run_backup()
    server_root = (
        result.path / "servers" / backupsafe(instance.id)
    )
    files_root = server_root / "files"
    outside = tmp_path / "outside-snapshot"
    outside.mkdir()

    try:
        server_root.chmod(0o700)
        files_root.rename(server_root / "files-original")
        files_root.symlink_to(outside, target_is_directory=True)
        server_root.chmod(0o555)

        verification = backups.verify_generation(result.path, deep=False)
        assert not verification["ok"]
        assert any(
            "unsafe files snapshot directory" in error
            for error in verification["errors"]
        )
    finally:
        backups._make_generation_removable(result.path)


def test_restore_refuses_existing_target_and_does_not_overwrite(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    result = backups.run_backup()
    target = tmp_path / "restored"
    target.mkdir()
    marker = target / "preserve.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(FleetBackupError, match="already exists"):
        backups.restore_server(result.generation_id, instance.id, target)
    assert marker.read_text(encoding="utf-8") == "preserve"


def _write_legacy_world_archive(path: Path, world_name: str = "ArchivedWorld") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{world_name}/level.dat", b"synthetic archived world metadata"
        )
        archive.writestr(f"{world_name}/region/r.0.0.mca", b"synthetic archived region")
    return path


def _legacy_backup_dir(tmp_path: Path, instance_id: str) -> Path:
    return tmp_path / "legacy-backups" / instance_id


def test_legacy_world_archives_are_included_and_restored_separately(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    archive_dir = _legacy_backup_dir(tmp_path, instance.id)
    archive = _write_legacy_world_archive(archive_dir / "World_backup.zip")
    archive.with_name(archive.name + ".json").write_text(
        json.dumps({"slot_name": "Before update"}),
        encoding="utf-8",
    )

    result = backups.run_backup()
    verification = backups.verify_generation(result.path, deep=True)
    assert verification["ok"], verification["errors"]

    server_record = verification["manifest"]["instances"][0]
    assert len(server_record["legacy_world_archives"]) == 1
    archived = server_record["legacy_world_archives"][0]
    assert archived["filename"] == archive.name
    assert archived["slot_name"] == "Before update"
    assert archived["sha256"]
    stored = (
        result.path
        / "servers"
        / backupsafe(instance.id)
        / "legacy-world-archives"
        / archive.name
    )
    assert stored.read_bytes() == archive.read_bytes()
    # One generation is not enough to retire the only external source copy.
    assert archive.is_file()

    restored = backups.restore_server(
        result.generation_id, instance.id, tmp_path / "restored-with-archives"
    )
    restored_archive = restored / ".crucible-legacy-world-archives" / archive.name
    assert restored_archive.read_bytes() == archive.read_bytes()
    index = json.loads(
        (restored_archive.parent / "index.json").read_text(encoding="utf-8")
    )
    assert index[0]["slot_name"] == "Before update"


def test_world_recovery_history_includes_and_exports_migrated_legacy_zip(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    archive = _write_legacy_world_archive(
        _legacy_backup_dir(tmp_path, instance.id) / "World_backup.zip",
        world_name="ArchivedWorld",
    )
    archive.with_name(archive.name + ".json").write_text(
        json.dumps({"slot_name": "Before update"}),
        encoding="utf-8",
    )
    generation = backups.run_backup()

    points = backups.list_world_recovery_points(instance.id)
    archived = points["ArchivedWorld"][0]
    assert archived.generation_id == generation.generation_id
    assert archived.legacy_archive_filename == "World_backup.zip"
    assert archived.active is False

    restored_archive = backups.export_world_archive(
        generation.generation_id,
        instance.id,
        "ArchivedWorld",
        tmp_path / "restore" / "selected-world.zip",
        deep_verify=True,
        legacy_archive_filename=archived.legacy_archive_filename,
    )
    with zipfile.ZipFile(restored_archive["path"]) as zipped:
        names = set(zipped.namelist())
    assert "ArchivedWorld/level.dat" in names
    assert all(name.startswith("ArchivedWorld/") for name in names)
    assert restored_archive["sha256"]


def test_legacy_archive_source_is_removed_only_after_two_generations_match(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    archive = _write_legacy_world_archive(
        _legacy_backup_dir(tmp_path, instance.id) / "old-world.zip"
    )
    metadata = archive.with_name(archive.name + ".json")
    metadata.write_text('{"slot_name":"Original world"}', encoding="utf-8")

    first = backups.run_backup()
    assert archive.exists()

    second = backups.run_backup()
    assert not archive.exists()
    assert not metadata.exists()
    assert second.migrated_legacy_archives == 1
    assert second.legacy_archive_bytes_removed > 0
    assert backups.verify_generation(first.path, deep=True)["ok"]
    assert backups.verify_generation(second.path, deep=True)["ok"]

    third = backups.run_backup()
    assert archive.exists() is False
    assert backups.verify_generation(second.path, deep=True)["ok"]
    assert backups.verify_generation(third.path, deep=True)["ok"]

    latest_archives = backups.list_legacy_world_archives(instance.id)
    assert len(latest_archives) == 1
    assert latest_archives[0].filename == "old-world.zip"
    assert latest_archives[0].slot_name == "Original world"
    assert latest_archives[0].fleet_generation_id == third.generation_id


def test_legacy_archive_migration_hashes_both_retained_copies_before_source_retirement(
    tmp_path,
):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    archive = _write_legacy_world_archive(
        _legacy_backup_dir(tmp_path, instance.id) / "legacy-world.zip"
    )
    original_bytes = archive.read_bytes()

    first = backups.run_backup()
    first_stored = (
        first.path
        / "servers"
        / backupsafe(instance.id)
        / "legacy-world-archives"
        / archive.name
    )
    # Simulate same-size bit rot in an immutable retained copy while leaving
    # the original sidecar/source untouched. Shallow verification alone
    # cannot detect this corruption.
    first_stored.chmod(0o600)
    first_stored.write_bytes(b"X" + original_bytes[1:])
    first_stored.chmod(0o444)

    second = backups.run_backup()
    second_stored = (
        second.path
        / "servers"
        / backupsafe(instance.id)
        / "legacy-world-archives"
        / archive.name
    )

    # A damaged previous copy must not be propagated by hard-link reuse, and
    # the external original must not be retired while either retained copy is
    # corrupt, even when both manifests contain the original expected digest.
    assert second_stored.read_bytes() == original_bytes
    assert archive.is_file()
    assert first_stored.read_bytes() != original_bytes
    state = json.loads(backups.state_path.read_text(encoding="utf-8"))
    assert any(
        first.generation_id in error
        for error in state["legacy_archive_migration"]["cleanup_errors"]
    )

    # Once the damaged generation ages out, the newest two actual archive
    # payloads verify. Only then is the original legacy ZIP eligible for
    # retirement.
    third = backups.run_backup()
    assert not first.path.exists()
    assert not archive.exists()
    assert backups.verify_generation(second.path, deep=True)["ok"]
    assert backups.verify_generation(third.path, deep=True)["ok"]


def test_world_swap_can_defer_legacy_source_retirement_until_post_swap_generation(
    tmp_path,
):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    archive = _write_legacy_world_archive(
        _legacy_backup_dir(tmp_path, instance.id) / "selected-world.zip"
    )

    # The pre-swap checkpoint must not unlink the external path selected by
    # the World table, even when it publishes a generation.
    before_swap = backups.run_backup(retire_legacy_sources=False)
    assert archive.is_file()
    assert backups.verify_generation(before_swap.path, deep=True)["ok"]

    # After extraction/world mutation, the new generation plus the before-swap
    # generation carry the same verified archive. Only now may the old path go.
    after_swap = backups.run_backup()
    assert not archive.exists()
    assert after_swap.migrated_legacy_archives == 1
    assert backups.verify_generation(before_swap.path, deep=True)["ok"]
    assert backups.verify_generation(after_swap.path, deep=True)["ok"]
    assert first.path.exists() is False


def test_legacy_archive_added_after_first_generation_waits_for_two_verified_copies(
    tmp_path,
):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    archive = _write_legacy_world_archive(
        _legacy_backup_dir(tmp_path, instance.id) / "late-world.zip"
    )

    second = backups.run_backup()
    assert archive.exists()
    assert not second.migrated_legacy_archives

    third = backups.run_backup()
    assert not archive.exists()
    assert third.migrated_legacy_archives == 1
    assert backups.verify_generation(second.path, deep=True)["ok"]
    assert backups.verify_generation(third.path, deep=True)["ok"]
    assert first.path.exists() is False


def test_corrupt_legacy_world_archive_blocks_publication_without_losing_prior_generations(
    tmp_path,
):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    second = backups.run_backup()

    bad_archive = _legacy_backup_dir(tmp_path, instance.id) / "damaged.zip"
    bad_archive.parent.mkdir(parents=True, exist_ok=True)
    bad_archive.write_bytes(b"not a zip archive")

    with pytest.raises(
        FleetBackupError, match="Could not validate legacy world archive"
    ):
        backups.run_backup()

    assert first.path.exists()
    assert second.path.exists()
    assert backups.verify_generation(first.path, deep=True)["ok"]
    assert backups.verify_generation(second.path, deep=True)["ok"]
    assert bad_archive.exists()
    assert not list(backups.staging_dir.iterdir())


def test_legacy_archive_symlinks_are_rejected(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    outside = _write_legacy_world_archive(tmp_path / "outside.zip")
    archive_dir = _legacy_backup_dir(tmp_path, instance.id)
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "linked.zip").symlink_to(outside)

    with pytest.raises(FleetBackupError, match="symbolic link"):
        backups.run_backup()
    assert outside.exists()
    assert backups.list_generations() == []


def test_invalid_generation_manifest_is_rejected_and_never_rotated_away(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    second = backups.run_backup()
    manifest = first.path / "manifest.json"
    manifest.chmod(0o600)
    manifest.write_text("{}", encoding="utf-8")

    verification = backups.verify_generation(first.path, deep=True)
    assert not verification["ok"]
    issues = backups.generation_issues()
    assert [item["path"] for item in issues] == [first.path]

    # A corrupt remnant must be reported and preserved, not silently rotated
    # away while the scheduler continues to claim clean two-generation policy.
    with pytest.raises(FleetBackupError, match="invalid"):
        backups.run_backup()
    assert first.path.exists()
    assert second.path.exists()
    assert backups.list_generations()[0].generation_id == second.generation_id


def test_generation_root_manifest_and_directories_are_immutable(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    result = backups.run_backup()
    manifest = result.path / "manifest.json"
    server_root = result.path / "servers" / backupsafe(instance.id)
    world_dir = server_root / "files" / "main_world"

    assert result.path.stat().st_mode & 0o222 == 0
    assert manifest.stat().st_mode & 0o222 == 0
    assert server_root.stat().st_mode & 0o222 == 0
    assert world_dir.stat().st_mode & 0o222 == 0


def test_symlinked_fleet_backup_root_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "fleet-link"
    linked_root.symlink_to(outside, target_is_directory=True)
    instance = make_server(tmp_path / "server")
    backups = FleetBackupManager([instance], FakeTmux(), root=linked_root)

    with pytest.raises(FleetBackupError, match="symbolic link"):
        backups.run_backup()
    assert list(outside.iterdir()) == []


def test_symlinked_lock_file_is_refused(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    backups.root.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "external-lock-target"
    target.write_text("do not touch", encoding="utf-8")
    backups.lock_path.symlink_to(target)

    with pytest.raises(FleetBackupError, match="symbolic link"):
        backups.run_backup()
    assert target.read_text(encoding="utf-8") == "do not touch"


def test_backup_fleet_cli_parser_exposes_run_list_and_deep_verify_modes():
    from crucible.__main__ import build_parser

    parser = build_parser()
    run_args = parser.parse_args(["backup-fleet"])
    list_args = parser.parse_args(["backup-fleet", "--list"])
    verify_args = parser.parse_args(["backup-fleet", "--verify"])
    install_args = parser.parse_args(["backup-fleet", "--install-timer"])
    remove_args = parser.parse_args(["backup-fleet", "--remove-timer"])

    assert run_args.command == "backup-fleet"
    assert not run_args.list and not run_args.verify
    assert list_args.list and not list_args.verify
    assert verify_args.verify and not verify_args.list
    assert install_args.install_timer and not install_args.remove_timer
    assert remove_args.remove_timer and not remove_args.install_timer

    with pytest.raises(SystemExit):
        parser.parse_args(["backup-fleet", "--list", "--verify"])


def test_world_inventory_does_not_present_temporary_pre_swap_copies_as_named_worlds(
    tmp_path,
):
    instance = make_server(tmp_path / "server")
    rollback = Path(instance.path) / "main_world.pre-swap-20261105_123456_000001"
    rollback.mkdir()
    (rollback / "level.dat").write_bytes(b"temporary prior world")

    backups = manager(tmp_path, [instance])
    generation = backups.run_backup()
    verification = backups.verify_generation(generation.path, deep=True)

    assert verification["ok"], verification["errors"]
    server_record = verification["manifest"]["instances"][0]
    assert "main_world.pre-swap-20261105_123456_000001" not in {
        world["name"] for world in server_record["world_saves"]
    }
    assert (
        generation.path
        / "servers"
        / backupsafe(instance.id)
        / "files"
        / "main_world.pre-swap-20261105_123456_000001"
        / "level.dat"
    ).is_file()


def test_world_recovery_points_are_listed_for_each_retained_generation(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    first = backups.run_backup()
    second = backups.run_backup()

    points = backups.list_world_recovery_points(instance.id)

    assert list(points) == ["main_world"]
    assert [
        point.generation_id for point in points["main_world"]
    ] == [second.generation_id, first.generation_id]
    assert all(point.active for point in points["main_world"])
    assert all(point.world_name == "main_world" for point in points["main_world"])
    assert all(point.size_bytes > 0 for point in points["main_world"])


def test_export_world_archive_contains_only_the_selected_verified_world(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    generation = backups.run_backup()
    destination = tmp_path / "recovery" / "main-world.zip"

    exported = backups.export_world_archive(
        generation.generation_id,
        instance.id,
        "main_world",
        destination,
        deep_verify=True,
    )

    assert exported["path"] == destination
    assert exported["world_name"] == "main_world"
    assert exported["generation_id"] == generation.generation_id
    assert exported["size_bytes"] == destination.stat().st_size
    assert len(exported["sha256"]) == 64
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
    assert "main_world/level.dat" in names
    assert "main_world/region/r.0.0.mca" in names
    assert all(name.startswith("main_world/") for name in names)
    assert "server.properties" not in names
    assert "mods/example.jar" not in names


def test_export_world_archive_refuses_unsafe_or_missing_worlds(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    generation = backups.run_backup()

    with pytest.raises(FleetBackupError, match="Unsafe world name"):
        backups.export_world_archive(
            generation.generation_id,
            instance.id,
            "../main_world",
            tmp_path / "unsafe.zip",
        )
    with pytest.raises(FleetBackupError, match="does not contain world"):
        backups.export_world_archive(
            generation.generation_id,
            instance.id,
            "not-a-world",
            tmp_path / "missing.zip",
        )
    assert not (tmp_path / "unsafe.zip").exists()
    assert not (tmp_path / "missing.zip").exists()


def test_migrated_saved_world_archive_appears_once_in_world_history_and_can_be_exported(tmp_path):
    instance = make_server(tmp_path / "server")
    backups = manager(tmp_path, [instance])
    archive = _write_legacy_world_archive(
        _legacy_backup_dir(tmp_path, instance.id) / "archived-world.zip",
        world_name="ArchivedWorld",
    )

    first = backups.run_backup()
    assert archive.exists()
    second = backups.run_backup()
    assert not archive.exists()

    points = backups.list_world_recovery_points(instance.id)
    archived_points = points["ArchivedWorld"]
    assert len(archived_points) == 1
    point = archived_points[0]
    assert point.legacy_archive_filename == "archived-world.zip"
    assert point.generation_id == second.generation_id
    assert point.world_name == "ArchivedWorld"
    assert not point.active

    exported_path = tmp_path / "restore" / "archived-world.zip"
    exported = backups.export_world_archive(
        point.generation_id,
        instance.id,
        point.world_name,
        exported_path,
        legacy_archive_filename=point.legacy_archive_filename,
        deep_verify=True,
    )
    assert exported["path"] == exported_path
    assert exported["world_name"] == "ArchivedWorld"
    with zipfile.ZipFile(exported_path) as restored_archive:
        assert "ArchivedWorld/level.dat" in restored_archive.namelist()
        assert restored_archive.read("ArchivedWorld/level.dat") == b"synthetic archived world metadata"

    # The original archive is preserved by the two retained generations,
    # then represented only once in the user-facing world history.
    assert backups.verify_generation(first.path, deep=True)["ok"]
    assert backups.verify_generation(second.path, deep=True)["ok"]


def test_export_world_archive_includes_companion_dimension_folders(tmp_path):
    instance = make_server(tmp_path / "server")
    server = Path(instance.path)
    for suffix in ("_nether", "_the_end"):
        companion = server / f"main_world{suffix}"
        companion.mkdir()
        (companion / "level.dat").write_bytes(b"companion level metadata")
        (companion / "region").mkdir()
        (companion / "region" / "r.0.0.mca").write_bytes(b"companion region")
    backups = manager(tmp_path, [instance])
    generation = backups.run_backup()

    exported = backups.export_world_archive(
        generation.generation_id,
        instance.id,
        "main_world",
        tmp_path / "world-with-dimensions.zip",
    )
    with zipfile.ZipFile(exported["path"]) as archive:
        names = set(archive.namelist())
    assert "main_world/level.dat" in names
    assert "main_world_nether/level.dat" in names
    assert "main_world_the_end/level.dat" in names


def test_legacy_world_archive_root_requires_its_own_level_dat(tmp_path):
    from crucible.data.fleet_backup import FleetBackupError, FleetBackupManager

    archive_path = tmp_path / "wrong-root.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("actual-world/region/r.0.0.mca", b"region")
        archive.writestr("actual-world_nether/level.dat", b"companion metadata")

    with pytest.raises(FleetBackupError, match="level.dat at its world root"):
        FleetBackupManager._legacy_archive_world_name(archive_path)
