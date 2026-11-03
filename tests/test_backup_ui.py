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
