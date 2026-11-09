from __future__ import annotations

import threading
from pathlib import Path

from crucible.data.instance_model import ServerInstance
from crucible.ui.new_server_dialog import _CreateWorker


def test_create_worker_returns_validation_result_for_registry_commit(tmp_path, monkeypatch):
    target = tmp_path / "new-server"
    target.mkdir()
    result = object()
    validation_calls = []

    def create_fresh_server(*_args, **_kwargs):
        return result

    def validate(candidate):
        validation_calls.append(candidate.path)
        return ["synthetic validation warning"]

    monkeypatch.setattr(
        "crucible.importers.serverloader.create_fresh_server",
        create_fresh_server,
    )
    monkeypatch.setattr(ServerInstance, "validate", validate)

    worker = _CreateWorker(
        str(target), "1.21.1", "fabric", "0.16.10", True, threading.Event()
    )
    emitted = []
    worker.finished.connect(lambda created, problems: emitted.append((created, problems)))
    worker.run()

    assert validation_calls == [str(target)]
    assert emitted == [(result, ["synthetic validation warning"])]
