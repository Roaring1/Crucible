from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from crucible.mods.mod_manager import ModEntry
from crucible.ui.tabs.mods_tab import ModsTab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return True
        QTest.qWait(5)
    app.processEvents()
    return bool(predicate())


def _inventory_idle(tab: ModsTab) -> bool:
    return (
        tab._inventory_thread is None
        and not tab._inventory_refresh_pending
        and not tab._table_rendering
    )


def test_bundled_rows_use_items_not_child_widgets(tmp_path):
    app = _app()
    tab = ModsTab()
    bundled = [
        ModEntry(
            f"nested-{index}.jar",
            tmp_path / f"parent-{index}" / f"nested-{index}.jar",
            False,
            50,
            bundled=True,
        )
        for index in range(100)
    ]

    try:
        # Populate a manageable row first so the replacement proves an old
        # Enabled/Disabled QPushButton is not retained in the same row slot.
        managed = ModEntry("managed.jar", tmp_path / "managed.jar", True, 25)
        tab._populate_table([managed])
        labels_before = len(tab.findChildren(QLabel))
        tab._populate_table(bundled)
        labels_after = len(tab.findChildren(QLabel))

        assert tab._table.rowCount() == 100
        assert labels_after == labels_before
        for row in range(100):
            state = tab._table.item(row, tab._COL_ENABLED)
            assert tab._table.cellWidget(row, tab._COL_ENABLED) is None
            assert state.text() == "Bundled"
            assert state.textAlignment() & Qt.AlignmentFlag.AlignHCenter
            assert "cannot be toggled or removed" in state.toolTip()
            assert state.flags() & Qt.ItemFlag.ItemIsSelectable
            name = tab._table.item(row, tab._COL_NAME)
            mod = name.data(Qt.ItemDataRole.UserRole)
            assert mod in bundled
        rendered_mods = {
            id(
                tab._table.item(row, tab._COL_NAME).data(
                    Qt.ItemDataRole.UserRole
                )
            )
            for row in range(100)
        }
        assert rendered_mods == {id(mod) for mod in bundled}
    finally:
        tab.close()
        app.processEvents()


def test_inspection_queue_skips_bundled_and_already_inspected_entries(tmp_path):
    manageable = ModEntry("main.jar", tmp_path / "main.jar", True, 100)
    bundled = ModEntry(
        "nested-lib.jar",
        tmp_path / "parent" / "nested-lib.jar",
        False,
        50,
        bundled=True,
    )
    already_inspected = ModEntry(
        "known.jar", tmp_path / "known.jar", True, 25, name="Known", version="1.0"
    )

    jobs = ModsTab._inspection_jobs([manageable, bundled, already_inspected])

    assert jobs == [(0, manageable)]


def test_async_metadata_result_uses_identity_map_after_table_sorting(tmp_path, monkeypatch):
    app = _app()
    tab = ModsTab()
    first = ModEntry("z.jar", tmp_path / "z.jar", True, 10)
    second = ModEntry("a.jar", tmp_path / "a.jar", True, 10)
    tab._instance = SimpleNamespace(path=str(tmp_path))

    try:
        tab._populate_table([first, second])
        tab._table.sortItems(tab._COL_NAME, Qt.SortOrder.AscendingOrder)

        # The second mod now occupies an earlier table row than the first.
        assert tab._table.item(0, tab._COL_NAME).data(
            Qt.ItemDataRole.UserRole
        ) is second

        first.name = "Zeta"
        first.version = "2.0"
        first.side = "client"
        tab._added_mod_snapshot = {}
        monkeypatch.setattr(
            "crucible.mods.modrinth.added_mods",
            lambda _path: (_ for _ in ()).throw(
                AssertionError("worker result reread added-mods.json")
            ),
        )

        # The worker's original row argument is stale after sorting. The
        # identity map must find and update the right item anyway.
        tab._on_inspect_result(tab._inspect_generation, 0, first)

        first_item = tab._mod_name_items[id(first)]
        row = first_item.row()
        assert row >= 0
        assert first_item.data(Qt.ItemDataRole.UserRole) is first
        assert first_item.text() == "Zeta"
        assert tab._table.item(row, tab._COL_VERSION).text() == "2.0"
        assert tab._table.item(row, tab._COL_SIDE).text() == "Client only"
    finally:
        tab.close()
        app.processEvents()


def test_inspection_result_ignores_replaced_or_stale_mod_items(tmp_path, monkeypatch):
    app = _app()
    tab = ModsTab()
    original = ModEntry("same.jar", tmp_path / "same.jar", True, 10)
    replacement = ModEntry("same.jar", tmp_path / "same.jar", True, 10)
    tab._instance = SimpleNamespace(path=str(tmp_path))

    try:
        tab._populate_table([original])
        tab._populate_table([replacement])
        replacement_item = tab._mod_name_items[id(replacement)]
        original.name = "Must Not Appear"
        original.version = "99"
        tab._added_mod_snapshot = {}
        monkeypatch.setattr(
            "crucible.mods.modrinth.added_mods",
            lambda _path: (_ for _ in ()).throw(
                AssertionError("stale worker result reread added-mods.json")
            ),
        )

        tab._on_inspect_result(tab._inspect_generation, 0, original)

        assert replacement_item.text() == "same"
        assert replacement_item.data(Qt.ItemDataRole.UserRole) is replacement
    finally:
        tab.close()
        app.processEvents()





def test_refresh_scans_inventory_off_thread_and_latest_server_wins(
    tmp_path, monkeypatch
):
    app = _app()
    tab = ModsTab()
    old_mod = ModEntry(
        "old.jar", tmp_path / "server-a" / "mods" / "old.jar", True, 10
    )
    new_mod = ModEntry(
        "new.jar", tmp_path / "server-b" / "mods" / "new.jar", True, 10
    )
    old_instance = SimpleNamespace(path=str(tmp_path / "server-a"))
    new_instance = SimpleNamespace(path=str(tmp_path / "server-b"))
    old_started = threading.Event()
    release_old = threading.Event()
    identity_paths: list[tuple[str, int]] = []
    main_thread = threading.get_ident()

    class SlowOldManager:
        def list_mods(self):
            old_started.set()
            release_old.wait(3.0)
            return [old_mod]

    class NewManager:
        def list_mods(self):
            return [new_mod]

    def read_identity(path):
        identity_paths.append((path, threading.get_ident()))
        return {Path(path).name: {"project_id": f"project-{Path(path).name}"}}

    tab._start_inspect_pass = lambda: None
    monkeypatch.setattr("crucible.mods.modrinth.added_mods", read_identity)

    try:
        tab._instance = old_instance
        tab._manager = SlowOldManager()
        tab.refresh()
        assert old_started.wait(1.0)
        assert not tab._table.isEnabled()
        assert not tab.has_active_operation()

        # Replace the in-flight request. The old result must be discarded,
        # then the one pending worker must read the newly selected server.
        tab._instance = new_instance
        tab._manager = NewManager()
        tab.refresh()
        assert tab._inventory_refresh_pending
        assert not tab.has_active_operation()
        release_old.set()

        assert _wait_until(lambda: _inventory_idle(tab))
        assert tab._table.rowCount() == 1
        assert tab._table.item(0, tab._COL_NAME).data(
            Qt.ItemDataRole.UserRole
        ) is new_mod
        assert tab._table.item(0, tab._COL_NAME).text() == "new"
        assert tab._instance is new_instance
        assert len(identity_paths) == 2
        assert all(thread != main_thread for _path, thread in identity_paths)
        assert any(path == str(old_instance.path) for path, _thread in identity_paths)
        assert any(path == str(new_instance.path) for path, _thread in identity_paths)
    finally:
        release_old.set()
        _wait_until(lambda: _inventory_idle(tab))
        tab.close()
        app.processEvents()


def test_chunked_mod_table_yields_in_hundred_row_batches_and_replaces_stale_render(
    tmp_path
):
    app = _app()
    tab = ModsTab()
    old_mods = [
        ModEntry(
            f"old-{index}.jar",
            tmp_path / f"old-{index}.jar",
            False,
            5,
            bundled=True,
        )
        for index in range(250)
    ]
    new_mods = [
        ModEntry(
            f"new-{index}.jar",
            tmp_path / f"new-{index}.jar",
            False,
            5,
            bundled=True,
        )
        for index in range(3)
    ]

    try:
        tab._begin_chunked_table_population(old_mods)
        tab._table_render_timer.stop()
        tab._render_table_chunk()
        assert tab._table.rowCount() == 100
        assert tab._table_rendering
        assert not tab.has_active_operation()
        assert not tab._table.isEnabled()
        assert tab._table.item(99, tab._COL_NAME).text() == "old-99"

        # A new refresh invalidates the remaining 150-row batch schedule.
        tab._begin_chunked_table_population(new_mods)
        tab._table_render_timer.stop()
        tab._render_table_chunk()
        assert not tab._table_rendering
        assert tab._table.isEnabled()
        assert tab._table.rowCount() == 3
        assert tab._table.item(0, tab._COL_NAME).text() == "new-0"
        assert tab._table.item(2, tab._COL_NAME).text() == "new-2"
        assert not tab.has_active_operation()

        # A stale timer event after completion is inert.
        tab._render_table_chunk()
        assert tab._table.rowCount() == 3
        assert tab._table.item(0, tab._COL_NAME).text() == "new-0"
    finally:
        tab.close()
        app.processEvents()


def test_inventory_worker_failure_reenables_table_and_reports_read_error(
    tmp_path
):
    app = _app()
    tab = ModsTab()
    tab._instance = SimpleNamespace(path=str(tmp_path))

    class BrokenManager:
        def list_mods(self):
            raise OSError("mods directory became unavailable")

    tab._manager = BrokenManager()

    try:
        tab.refresh()
        assert _wait_until(lambda: _inventory_idle(tab))
        assert tab._table.isEnabled()
        assert tab._table.rowCount() == 0
        assert "mods directory became unavailable" in tab._count_label.text()
    finally:
        _wait_until(lambda: _inventory_idle(tab))
        tab.close()
        app.processEvents()





def test_destroyed_tab_does_not_start_queued_inventory_refresh(tmp_path):
    app = _app()
    from crucible.ui.tabs import mods_tab

    tab = ModsTab()
    started = threading.Event()
    release = threading.Event()
    calls = []

    class SlowManager:
        def list_mods(self):
            calls.append(threading.get_ident())
            started.set()
            release.wait(3.0)
            return []

    tab._instance = SimpleNamespace(path=str(tmp_path))
    tab._manager = SlowManager()
    lifecycle_state = tab._inventory_lifecycle_state

    try:
        tab.refresh()
        assert started.wait(1.0)
        tab.refresh()
        assert tab._inventory_refresh_pending
        tab.deleteLater()
        QCoreApplication.sendPostedEvents(tab, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert lifecycle_state["destroyed"]

        release.set()
        assert _wait_until(lambda: not mods_tab._ACTIVE_MOD_INVENTORY_WORKERS)
        assert len(calls) == 1
    finally:
        release.set()
        app.processEvents()





def test_toggle_refresh_restores_selected_mod_after_async_population(
    tmp_path, monkeypatch
):
    app = _app()
    tab = ModsTab()
    original = ModEntry("z-mod.jar", tmp_path / "z-mod.jar", True, 10)
    other = ModEntry("a-mod.jar", tmp_path / "a-mod.jar", False, 10)
    replacement = ModEntry("z-mod.jar", tmp_path / "z-mod.jar", False, 10)
    instance = SimpleNamespace(path=str(tmp_path), id="server-a")

    class FakeManager:
        def disable(self, mod):
            assert mod is original
            mod.enabled = False

        def list_mods(self):
            return [other, replacement]

    tab._instance = instance
    tab._manager = FakeManager()
    tab._require_stopped = lambda: True
    tab._start_inspect_pass = lambda: None
    monkeypatch.setattr("crucible.mods.modrinth.added_mods", lambda _path: {})

    try:
        tab._populate_table([original])
        tab._set_enabled(original, False)

        # The inventory worker may finish quickly; assert the stable result
        # rather than assuming an intermediate pending state is observable.
        assert _wait_until(lambda: _inventory_idle(tab))

        selected = tab._selected_mod()
        assert selected is replacement
        assert tab._table.currentRow() == 1
        assert tab._table.item(1, tab._COL_NAME).data(
            Qt.ItemDataRole.UserRole
        ) is replacement
        assert tab._detail_title.text() == replacement.display_name
        assert tab._select_after_refresh_filename is None
    finally:
        _wait_until(lambda: _inventory_idle(tab))
        tab.close()
        app.processEvents()


def test_mod_identity_record_is_read_once_per_refresh_on_inventory_worker(
    tmp_path, monkeypatch
):
    app = _app()
    tab = ModsTab()
    first = ModEntry("first.jar", tmp_path / "first.jar", True, 10)
    second = ModEntry("second.jar", tmp_path / "second.jar", True, 10)
    tab._instance = SimpleNamespace(path=str(tmp_path))
    main_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []
    list_threads: list[int] = []

    class FakeManager:
        def list_mods(self):
            list_threads.append(threading.get_ident())
            return [first, second]

    tab._manager = FakeManager()
    tab._start_inspect_pass = lambda: None

    def read_identity(path):
        calls.append((path, threading.get_ident()))
        return {"first.jar": {"project_id": "project-123"}}

    monkeypatch.setattr("crucible.mods.modrinth.added_mods", read_identity)

    try:
        tab.refresh()
        assert calls == []
        assert not tab._table.isEnabled()
        assert _wait_until(lambda: _inventory_idle(tab))

        assert len(calls) == 1
        assert calls[0][0] == str(tmp_path)
        assert list_threads and list_threads[0] != main_thread
        assert calls[0][1] != main_thread
        assert tab._table.rowCount() == 2
        first.name = "First"
        first.version = "1.0"
        tab._on_inspect_result(tab._inspect_generation, 0, first)
        assert "Exact online match" in tab._mod_name_items[id(first)].toolTip()

        # Selection changes and worker callbacks reuse the same in-memory
        # snapshot rather than reopening added-mods.json.
        tab._table.selectRow(1)
        first.name = "First"
        first.version = "1.0"
        second.name = "Second"
        second.version = "2.0"
        monkeypatch.setattr(
            "crucible.mods.modrinth.added_mods",
            lambda _path: (_ for _ in ()).throw(
                AssertionError("selection/inspection reread added-mods.json")
            ),
        )
        tab._on_inspect_result(tab._inspect_generation, 0, first)
        tab._on_inspect_result(tab._inspect_generation, 1, second)
        assert len(calls) == 1

        # A new refresh intentionally takes one new background snapshot.
        monkeypatch.setattr("crucible.mods.modrinth.added_mods", read_identity)
        tab.refresh()
        assert _wait_until(lambda: _inventory_idle(tab))
        assert len(calls) == 2
        assert all(path == str(tmp_path) for path, _thread in calls)
        assert all(thread != main_thread for _path, thread in calls)
    finally:
        _wait_until(lambda: _inventory_idle(tab))
        tab.close()
        app.processEvents()
