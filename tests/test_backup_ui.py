from types import SimpleNamespace

from crucible.ui.tabs import backup_tab


def test_restore_worker_deep_verifies_and_returns_isolated_destination(
    tmp_path, monkeypatch
):
    instance = SimpleNamespace(id="server-id", name="Example Server")
    target = tmp_path / "restore-output"
    calls = []

    class FakeFleetBackupManager:
        def __init__(self, instances, _tmux):
            assert instances == [instance]
            self.root = tmp_path / "fleet-root"

        def restore_server(self, generation_id, instance_id, destination, *, deep_verify):
            calls.append((generation_id, instance_id, destination, deep_verify))
            return destination

    monkeypatch.setattr(
        backup_tab, "FleetBackupManager", FakeFleetBackupManager
    )

    worker = backup_tab._FleetBackupWorker(
        [instance],
        "restore",
        restore_args=("generation-123", instance.id, target, instance.name),
    )
    finished = []
    failed = []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert calls == [("generation-123", "server-id", target, True)]
    assert finished == [
        {
            "generation_id": "generation-123",
            "instance_id": "server-id",
            "server_name": "Example Server",
            "target": str(target),
        }
    ]


def test_list_worker_reuses_one_generation_and_legacy_inventory_for_all_servers(
    tmp_path, monkeypatch
):
    instances = [
        SimpleNamespace(id="server-a", name="Server A"),
        SimpleNamespace(id="server-b", name="Server B"),
    ]
    generation_inventory = SimpleNamespace(
        generations=("generation-summary",),
        issues=("generation-issue",),
    )
    legacy_sources = [{"path": "legacy/manual", "kind": "manual_server_snapshot"}]
    calls = {
        "generation_inventory": 0,
        "legacy_source_inventory": 0,
        "world_points": [],
        "blocker_inventory": None,
    }

    class FakeFleetBackupManager:
        root = tmp_path / "fleet"

        def __init__(self, received_instances, _tmux):
            assert received_instances == instances

        def generation_inventory(self):
            calls["generation_inventory"] += 1
            return generation_inventory

        def legacy_source_inventory(self):
            calls["legacy_source_inventory"] += 1
            return legacy_sources

        def list_world_recovery_points(self, instance_id, *, inventory):
            assert inventory is not None
            calls["world_points"].append((instance_id, inventory))
            return {"world": [instance_id]}

        def legacy_backup_blockers(self, *, source_inventory):
            calls["blocker_inventory"] = source_inventory
            return source_inventory

    monkeypatch.setattr(
        backup_tab, "FleetBackupManager", FakeFleetBackupManager
    )
    monkeypatch.setattr(
        backup_tab.backup_scheduler, "scheduler_status", lambda: {"active": "false"}
    )

    worker = backup_tab._FleetBackupWorker(instances, "list")
    finished = []
    failed = []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert calls["generation_inventory"] == 1
    assert calls["legacy_source_inventory"] == 1
    assert calls["world_points"] == [
        ("server-a", generation_inventory),
        ("server-b", generation_inventory),
    ]
    assert calls["blocker_inventory"] is legacy_sources
    assert finished == [
        {
            "generations": ["generation-summary"],
            "issues": ["generation-issue"],
            "state": {},
            "scheduler": {"active": "false"},
            "world_recovery_points": {
                "server-a": {"world": ["server-a"]},
                "server-b": {"world": ["server-b"]},
            },
            "legacy_sources": legacy_sources,
            "legacy_backup_blockers": legacy_sources,
        }
    ]
