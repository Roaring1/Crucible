from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel

from crucible.mods.mod_manager import ModEntry
from crucible.ui.tabs.mods_tab import ModsTab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])





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
            assert name.data(Qt.ItemDataRole.UserRole) is bundled[row]
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


def test_mod_identity_record_is_read_once_per_refresh_for_all_rows(
    tmp_path, monkeypatch
):
    app = _app()
    tab = ModsTab()
    first = ModEntry("first.jar", tmp_path / "first.jar", True, 10)
    second = ModEntry("second.jar", tmp_path / "second.jar", True, 10)
    tab._instance = SimpleNamespace(path=str(tmp_path))

    class FakeManager:
        def list_mods(self):
            return [first, second]

    tab._manager = FakeManager()
    tab._start_inspect_pass = lambda: None
    calls = []

    def read_identity(path):
        calls.append(path)
        return {"first.jar": {"project_id": "project-123"}}

    monkeypatch.setattr("crucible.mods.modrinth.added_mods", read_identity)

    try:
        tab.refresh()
        assert calls == [str(tmp_path)]
        first.name = "First"
        first.version = "1.0"
        tab._on_inspect_result(tab._inspect_generation, 0, first)
        assert "Exact online match" in tab._mod_name_items[id(first)].toolTip()

        # Selection changes and separate worker completions reuse the same
        # snapshot rather than reopening and reparsing added-mods.json.
        tab._table.selectRow(1)
        first.name = "First"
        first.version = "1.0"
        second.name = "Second"
        second.version = "2.0"
        monkeypatch.setattr(
            "crucible.mods.modrinth.added_mods",
            lambda _path: (_ for _ in ()).throw(
                AssertionError("selection/worker callback reread added-mods.json")
            ),
        )
        tab._on_inspect_result(tab._inspect_generation, 0, first)
        tab._on_inspect_result(tab._inspect_generation, 1, second)
        assert calls == [str(tmp_path)]

        # A new refresh intentionally takes a new disk snapshot.
        monkeypatch.setattr("crucible.mods.modrinth.added_mods", read_identity)
        tab.refresh()
        assert calls == [str(tmp_path), str(tmp_path)]
    finally:
        tab.close()
        app.processEvents()
