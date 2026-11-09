from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from crucible.ui.tabs import world_tab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_world_archive_projection_is_reused_until_refresh_or_instance_change(
    monkeypatch,
):
    app = _app()
    tab = world_tab.WorldTab()
    instance = SimpleNamespace(id="server-a")
    tab._instance = instance

    first_entry = SimpleNamespace(
        filename="old-world.zip",
        display_name="Old World",
        created_at=datetime(2026, 11, 7, 12, 0, 0),
    )
    fleet_entry = SimpleNamespace(
        filename="migrated-world.zip",
        display_name="Migrated World",
        created_at=datetime(2026, 11, 7, 11, 0, 0),
    )

    class FakeBackupManager:
        def __init__(self):
            self.list_calls = 0
            self.world_name_calls = []

        def list_backups(self):
            self.list_calls += 1
            return [first_entry]

        def _backup_level_name(self, entry):
            self.world_name_calls.append(entry.filename)
            return {
                "old-world.zip": "OldWorld",
                "migrated-world.zip": "MigratedWorld",
            }[entry.filename]

    manager = FakeBackupManager()
    tab._manager = manager

    class FakeFleetBackupManager:
        def __init__(self, instances, _tmux):
            assert len(instances) == 1

        def list_legacy_world_archives(self, instance_id):
            assert instance_id in {"server-a", "server-b"}
            return [fleet_entry]

    monkeypatch.setattr(
        world_tab, "FleetBackupManager", FakeFleetBackupManager
    )

    try:
        first = tab._archive_entries_for(refresh=True)
        second = tab._archive_entries_for(refresh=False)

        assert first == [
            (first_entry, "OldWorld"),
            (fleet_entry, "MigratedWorld"),
        ]
        assert second == first
        assert manager.list_calls == 1
        assert manager.world_name_calls == [
            "old-world.zip",
            "migrated-world.zip",
        ]

        # An explicit refresh observes new files and reparses archive roots.
        tab._archive_entries_for(refresh=True)
        assert manager.list_calls == 2
        assert manager.world_name_calls == [
            "old-world.zip",
            "migrated-world.zip",
            "old-world.zip",
            "migrated-world.zip",
        ]

        # A different selected server never reuses the previous server's list.
        second_instance = SimpleNamespace(id="server-b")
        tab._instance = second_instance
        tab._archive_entries_for(refresh=False)
        assert manager.list_calls == 3
    finally:
        tab.close()
        app.processEvents()


def test_recovery_list_render_reuses_existing_archive_projection(monkeypatch):
    app = _app()
    tab = world_tab.WorldTab()
    tab._instance = SimpleNamespace(id="server-a")
    tab._recovery_job_mode = "list"
    tab._render_recovery_status = Mock()
    tab._refresh = Mock()

    try:
        tab._on_recovery_job_finished(
            {
                "generations": [],
                "scheduler": {},
                "state": {},
                "world_recovery_points": {"server-a": {"World": []}},
            }
        )

        assert tab._world_recovery_points == {"World": []}
        tab._refresh.assert_called_once_with(refresh_archives=False)
    finally:
        tab.close()
        app.processEvents()
