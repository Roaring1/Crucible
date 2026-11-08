from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from crucible.ui.tabs import world_tab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait(predicate, timeout: float = 4.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


def test_world_archive_projection_loads_off_thread_and_caches_until_refresh(
    monkeypatch,
):
    app = _app()
    tab = world_tab.WorldTab()
    instance = SimpleNamespace(id="server-a", path="/tmp/server-a")
    tab._instance = instance
    tab._refresh = Mock()

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
            self.thread_ids = []

        def list_backups(self):
            self.list_calls += 1
            self.thread_ids.append(threading.get_ident())
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

    monkeypatch.setattr(world_tab, "FleetBackupManager", FakeFleetBackupManager)
    main_thread_id = threading.get_ident()

    try:
        # The first call only schedules work; it does not enumerate ZIPs.
        assert tab._archive_entries_for(refresh=True) == []
        assert _wait(lambda: tab._archive_thread is None)
        expected = [
            (first_entry, "OldWorld"),
            (fleet_entry, "MigratedWorld"),
        ]
        assert tab._archive_entries_for(refresh=False) == expected
        assert manager.list_calls == 1
        assert len(manager.thread_ids) == 1
        assert manager.thread_ids[0] != main_thread_id
        assert manager.world_name_calls == [
            "old-world.zip",
            "migrated-world.zip",
        ]

        # Explicit refresh takes a new snapshot; an ordinary render reuses it.
        tab._archive_entries_for(refresh=True)
        assert _wait(lambda: tab._archive_thread is None and manager.list_calls == 2)
        assert tab._archive_entries_for(refresh=False) == expected

        # A new selected instance never reuses the prior instance's cache.
        tab._instance = SimpleNamespace(id="server-b", path="/tmp/server-b")
        assert tab._archive_entries_for(refresh=False) == []
        assert _wait(
            lambda: tab._archive_thread is None
            and tab._archive_inventory_instance_id == "server-b"
            and manager.list_calls == 3
        )
    finally:
        if tab._archive_thread is not None:
            _wait(lambda: tab._archive_thread is None)
        tab.close()
        app.processEvents()


def test_archive_refreshes_coalesce_and_stale_server_results_are_discarded(
    monkeypatch,
):
    app = _app()
    tab = world_tab.WorldTab()
    first_instance = SimpleNamespace(id="server-a", path="/tmp/server-a")
    second_instance = SimpleNamespace(id="server-b", path="/tmp/server-b")
    tab._instance = first_instance
    tab._refresh = Mock()
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    entries = {
        "server-a": SimpleNamespace(
            filename="old.zip",
            display_name="Old",
            created_at=datetime(2026, 11, 7, 12, 0, 0),
        ),
        "server-b": SimpleNamespace(
            filename="new.zip",
            display_name="New",
            created_at=datetime(2026, 11, 7, 13, 0, 0),
        ),
    }

    class FakeBackupManager:
        def list_backups(self):
            instance_id = tab._instance.id
            calls.append(instance_id)
            if len(calls) == 1:
                first_started.set()
                assert release_first.wait(3.0)
            return [entries[instance_id]]

        def _backup_level_name(self, entry):
            return entry.display_name

    tab._manager = FakeBackupManager()

    class EmptyFleetBackupManager:
        def __init__(self, _instances, _tmux):
            pass

        def list_legacy_world_archives(self, _instance_id):
            return []

    monkeypatch.setattr(world_tab, "FleetBackupManager", EmptyFleetBackupManager)

    try:
        tab._archive_entries_for(refresh=True)
        assert first_started.wait(1.0)

        # A new server selection invalidates the in-flight generation and
        # queues exactly one replacement scan after the old QThread exits.
        tab._instance = second_instance
        assert tab._archive_entries_for(refresh=True) == []
        assert tab._archive_pending

        release_first.set()
        assert _wait(
            lambda: tab._archive_thread is None
            and tab._archive_inventory_instance_id == "server-b"
            and len(calls) == 2
        )
        assert calls == ["server-a", "server-b"]
        assert [entry.filename for entry, _name in tab._archive_inventory_entries] == [
            "new.zip"
        ]
    finally:
        release_first.set()
        if tab._archive_thread is not None:
            _wait(lambda: tab._archive_thread is None)
        tab.close()
        app.processEvents()


def test_world_tab_load_returns_while_archive_scan_is_still_running(
    tmp_path, monkeypatch
):
    app = _app()
    world_root = tmp_path / "world"
    world_root.mkdir()
    instance = SimpleNamespace(
        id="server-load",
        path=str(tmp_path),
        world_root_path=lambda: world_root,
        world_dimension_dirs=lambda: [],
    )
    started = threading.Event()
    release = threading.Event()
    entry = SimpleNamespace(
        filename="saved.zip",
        display_name="Saved world",
        created_at=datetime(2026, 11, 7, 12, 0, 0),
        slot_name=None,
        fleet_generation_id=None,
        size_display="1 MiB",
    )

    class SlowBackupManager:
        def __init__(self, _instance):
            pass

        def list_backups(self):
            started.set()
            assert release.wait(3.0)
            return [entry]

        def _backup_level_name(self, _entry):
            return "SavedWorld"

        def list_pre_swap_dirs(self):
            return []

    class EmptyFleetBackupManager:
        def __init__(self, _instances, _tmux):
            pass

        def list_legacy_world_archives(self, _instance_id):
            return []

    monkeypatch.setattr(world_tab, "BackupManager", SlowBackupManager)
    monkeypatch.setattr(world_tab, "FleetBackupManager", EmptyFleetBackupManager)
    tab = world_tab.WorldTab()
    tab._run_recovery_job = lambda _mode: None
    tab._start_stats_worker = lambda _world_root: None

    try:
        tab.load(instance)
        assert started.wait(1.0)
        assert tab._archive_thread is not None
        assert tab._archive_thread.isRunning()
        assert not tab._archive_status.isHidden()
        # The active-world row renders immediately while ZIP enumeration waits.
        assert tab._table.rowCount() == 1
        assert tab._table.item(0, 0).text() == "world"

        release.set()
        assert _wait(
            lambda: tab._archive_thread is None
            and tab._archive_inventory_instance_id == "server-load"
        )
        assert tab._table.rowCount() == 2
        assert tab._table.item(1, 0).text() == "Saved world"
        assert tab._archive_status.isHidden()
    finally:
        release.set()
        if tab._archive_thread is not None:
            _wait(lambda: tab._archive_thread is None)
        tab.close()
        app.processEvents()


def test_archive_inventory_failure_is_visible_and_table_recovers(monkeypatch):
    app = _app()
    tab = world_tab.WorldTab()
    tab._instance = SimpleNamespace(id="server-a", path="/tmp/server-a")

    class BrokenBackupManager:
        def list_backups(self):
            raise PermissionError("synthetic archive read denial")

    tab._manager = BrokenBackupManager()

    try:
        tab._archive_entries_for(refresh=True)
        assert _wait(lambda: tab._archive_thread is None)
        assert not tab._archive_status.isHidden()
        assert "synthetic archive read denial" in tab._archive_status.text()
        assert tab._table.isEnabled()
        assert tab._archive_inventory_instance_id is None
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
