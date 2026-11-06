from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from crucible.ui.tabs import world_tab


def test_generation_world_restore_exports_before_rotation_and_uses_safe_swap(
    tmp_path, monkeypatch
):
    instance_root = tmp_path / "server"
    instance_root.mkdir()
    instance = SimpleNamespace(
        id="server-id",
        name="Example Server",
        path=str(instance_root),
    )
    calls = {"exports": [], "backups": [], "swaps": []}
    legacy_archive_filename = "archived-world.zip"

    class FakeFleetBackupManager:
        def __init__(self, instances, _tmux):
            assert instances == [instance]
            self.root = tmp_path / "fleet"

        def export_world_archive(
            self,
            generation_id,
            instance_id,
            world_name,
            archive_path,
            *,
            deep_verify,
            legacy_archive_filename=None,
        ):
            assert legacy_archive_filename == "archived-world.zip"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(b"verified selected world")
            result = {
                "path": archive_path,
                "filename": archive_path.name,
                "size_bytes": archive_path.stat().st_size,
                "sha256": "a" * 64,
                "world_name": world_name,
                "generation_id": generation_id,
                "created_at": "2026-11-05T23:50:00.000000Z",
            }
            calls["exports"].append(
                (
                    generation_id,
                    instance_id,
                    world_name,
                    archive_path,
                    deep_verify,
                    legacy_archive_filename,
                )
            )
            return result

        def run_backup(self, *, operation_lock_held, retire_legacy_sources=True):
            assert operation_lock_held is True
            calls["backups"].append(retire_legacy_sources)
            return SimpleNamespace(
                generation_id="before-restore" if len(calls["backups"]) == 1 else "after-restore"
            )

    class FakeBackupManager:
        def __init__(self, selected_instance):
            assert selected_instance is instance

        def swap_world(
            self,
            entry,
            *,
            safety_backup_verified,
            retain_pre_swap_dir,
            fleet_generation_id,
        ):
            assert entry.path.is_file()
            assert entry.path.read_bytes() == b"verified selected world"
            calls["swaps"].append(
                {
                    "filename": entry.filename,
                    "world_name": entry.slot_name,
                    "sha256": entry.sha256,
                    "generation_id": entry.fleet_generation_id,
                    "safety_backup_verified": safety_backup_verified,
                    "retain_pre_swap_dir": retain_pre_swap_dir,
                    "before_generation": fleet_generation_id,
                }
            )
            return SimpleNamespace(
                ok=True,
                message="Switched to expedition",
                pre_swap_dir=None,
                fleet_generation_id=None,
            )

    class FakeTmuxManager:
        def get_status(self, _instance):
            return "stopped"

    monkeypatch.setattr(
        world_tab, "FleetBackupManager", FakeFleetBackupManager
    )
    monkeypatch.setattr(world_tab, "BackupManager", FakeBackupManager)
    monkeypatch.setattr(world_tab, "TmuxManager", FakeTmuxManager)
    monkeypatch.setattr(
        world_tab, "fleet_operation_lock", lambda _root: nullcontext()
    )

    worker = world_tab._FleetWorldActionWorker(
        [instance],
        instance,
        "swap",
        world_recovery={
            "generation_id": "selected-old-generation",
            "world_name": "expedition",
            "legacy_archive_filename": legacy_archive_filename,
        },
    )
    finished = []
    failed = []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert len(finished) == 1
    result = finished[0]
    assert result["action"] == "swap"
    assert result["before"].generation_id == "before-restore"
    assert result["after"].generation_id == "after-restore"
    assert calls["exports"][0][0:3] == (
        "selected-old-generation",
        "server-id",
        "expedition",
    )
    assert calls["exports"][0][4] is True
    assert calls["exports"][0][5] == legacy_archive_filename
    assert calls["backups"] == [False, True]
    assert calls["swaps"] == [
        {
            "filename": "selected-world.zip",
            "world_name": "expedition",
            "sha256": "a" * 64,
            "generation_id": "selected-old-generation",
            "safety_backup_verified": True,
            "retain_pre_swap_dir": True,
            "before_generation": "before-restore",
        }
    ]
    assert result["result"].fleet_generation_id == "after-restore"
