from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from crucible.ui.tabs import players_tab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _server(root: Path, *, whitelist: list[dict] | None = None) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    for filename, entries in (
        ("whitelist.json", whitelist if whitelist is not None else []),
        ("ops.json", [{"name": "Operator", "uuid": "op-uuid"}]),
        ("banned-players.json", [{"name": "Banned", "uuid": "ban-uuid"}]),
    ):
        (root / filename).write_text(json.dumps(entries), encoding="utf-8")
    seen = root / ".crucible" / "players_seen.json"
    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.write_text(
        json.dumps({"Prior": {"first_seen": 1, "last_seen": 2, "sessions": 4}}),
        encoding="utf-8",
    )
    return SimpleNamespace(path=str(root), id=root.name)


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


def test_players_tab_reads_all_player_json_on_worker_thread(tmp_path, monkeypatch):
    app = _app()
    instance = _server(
        tmp_path / "server",
        whitelist=[{"name": f"Player-{index}", "uuid": f"uuid-{index}"} for index in range(125)],
    )
    tab = players_tab.PlayersTab()
    main_thread_id = threading.get_ident()
    read_threads: list[int] = []
    original = players_tab._read_json_file

    def tracked_read(path: Path):
        read_threads.append(threading.get_ident())
        return original(path)

    monkeypatch.setattr(players_tab, "_read_json_file", tracked_read)

    try:
        tab.load(instance)

        assert tab._whitelist_w._loading
        assert not tab._whitelist_w._table.isEnabled()
        assert not tab._whitelist_w._add_btn.isEnabled()
        assert _wait(
            lambda: (
                not tab._whitelist_w._loading
                and not tab._ops_w._loading
                and not tab._banned_w._loading
                and not tab._player_data_threads
            )
        )

        assert len(read_threads) == 4
        assert all(thread_id != main_thread_id for thread_id in read_threads)
        assert tab._whitelist_w._table.rowCount() == 125
        assert tab._whitelist_w._table.item(124, 0).text() == "Player-124"
        assert tab._whitelist_w._table.item(124, 1).text() == "uuid-124"
        assert tab._ops_w._table.item(0, 0).text() == "Operator"
        assert tab._banned_w._table.item(0, 0).text() == "Banned"
        assert tab._seen["Prior"]["sessions"] == 4
        assert tab._whitelist_w._add_btn.isEnabled()
        assert tab._whitelist_w._table.isEnabled()
    finally:
        tab.close()
        app.processEvents()


def test_stale_player_json_result_cannot_replace_new_selected_server(tmp_path, monkeypatch):
    app = _app()
    first = _server(tmp_path / "server-a", whitelist=[{"name": "Old", "uuid": "old"}])
    second = _server(tmp_path / "server-b", whitelist=[{"name": "New", "uuid": "new"}])
    tab = players_tab.PlayersTab()
    original = players_tab._read_json_file
    first_read_started = threading.Event()
    release_first_read = threading.Event()

    def delayed_read(path: Path):
        if "server-a" in str(path):
            first_read_started.set()
            release_first_read.wait(2.0)
        return original(path)

    monkeypatch.setattr(players_tab, "_read_json_file", delayed_read)

    try:
        tab.load(first)
        assert first_read_started.wait(1.0)
        tab.load(second)
        assert _wait(
            lambda: (
                not tab._whitelist_w._loading
                and tab._whitelist_w._table.rowCount() == 1
                and tab._whitelist_w._table.item(0, 0) is not None
            )
        )
        assert tab._whitelist_w._table.item(0, 0).text() == "New"
        assert tab._instance is second
    finally:
        release_first_read.set()
        _wait(lambda: not tab._player_data_threads)
        tab.close()
        app.processEvents()


def test_live_seen_event_during_load_is_merged_with_disk_snapshot(tmp_path, monkeypatch):
    app = _app()
    instance = _server(tmp_path / "server")
    tab = players_tab.PlayersTab()
    original = players_tab._read_json_file
    read_started = threading.Event()
    release_read = threading.Event()

    def delayed_seen_read(path: Path):
        data = original(path)
        if path.name == "players_seen.json":
            read_started.set()
            release_read.wait(2.0)
        return data

    monkeypatch.setattr(players_tab, "_read_json_file", delayed_seen_read)

    try:
        tab.load(instance)
        assert read_started.wait(1.0)

        tab._record_seen("Prior", joined=True)
        tab._join_times["Prior"] = time.time() - 12.0
        tab._record_seen("Prior", joined=False)
        tab._record_seen("JoinedWhileLoading", joined=True)
        assert tab._seen_loading

        release_read.set()
        assert _wait(
            lambda: not tab._seen_loading and not tab._player_data_threads
        )

        assert tab._seen["Prior"]["sessions"] == 5
        assert tab._seen["Prior"]["seconds_played"] >= 10.0
        assert tab._seen["JoinedWhileLoading"]["sessions"] == 1
        persisted = json.loads(
            (Path(instance.path) / ".crucible" / "players_seen.json").read_text(
                encoding="utf-8"
            )
        )
        assert persisted["Prior"]["sessions"] == 5
        assert persisted["JoinedWhileLoading"]["sessions"] == 1
    finally:
        release_read.set()
        _wait(lambda: not tab._player_data_threads)
        tab.close()
        app.processEvents()


def test_player_list_renders_in_bounded_chunks_and_ignores_stale_batches(tmp_path):
    app = _app()
    widget = players_tab._PlayerListWidget("whitelist.json", allow_add=True)
    instance = SimpleNamespace(path=str(tmp_path))
    entries = [
        {"name": f"Player-{index}", "uuid": f"uuid-{index}"}
        for index in range(200)
    ]

    try:
        widget.begin_async_load(instance, "whitelist.json", generation=10)
        widget.apply_async_data(
            10, {"exists": True, "data": entries, "error": ""}
        )
        render_generation = widget._render_generation

        # One chunk has at most 40 rows; remaining rows are scheduled on later
        # event-loop turns so Qt can paint and process input between batches.
        widget._render_async_chunk(render_generation)
        assert widget._table.rowCount() == 40
        assert widget._loading
        assert not widget._add_btn.isEnabled()

        # A replacement load invalidates the outstanding 1 ms chunk callback.
        widget.begin_async_load(instance, "whitelist.json", generation=11)
        widget.apply_async_data(
            10, {"exists": True, "data": [{"name": "Stale", "uuid": "x"}], "error": ""}
        )
        assert widget._table.rowCount() == 0

        widget.apply_async_data(
            11,
            {"exists": True, "data": entries[:3], "error": ""},
        )
        assert _wait(lambda: not widget._loading)
        assert widget._table.rowCount() == 3
        assert widget._table.item(2, 0).text() == "Player-2"
        assert widget._add_btn.isEnabled()
    finally:
        widget.close()
        app.processEvents()





def test_forget_during_seen_load_wins_over_late_disk_snapshot(tmp_path, monkeypatch):
    app = _app()
    instance = _server(tmp_path / "server")
    tab = players_tab.PlayersTab()
    original = players_tab._read_json_file
    read_started = threading.Event()
    release_read = threading.Event()

    def delayed_seen_read(path: Path):
        data = original(path)
        if path.name == "players_seen.json":
            read_started.set()
            release_read.wait(2.0)
        return data

    monkeypatch.setattr(players_tab, "_read_json_file", delayed_seen_read)

    try:
        tab.load(instance)
        assert read_started.wait(1.0)
        tab._forget_player("Prior")
        release_read.set()
        assert _wait(
            lambda: not tab._seen_loading and not tab._player_data_threads
        )

        assert "Prior" not in tab._seen
        persisted = json.loads(
            (Path(instance.path) / ".crucible" / "players_seen.json").read_text(
                encoding="utf-8"
            )
        )
        assert "Prior" not in persisted
    finally:
        release_read.set()
        _wait(lambda: not tab._player_data_threads)
        tab.close()
        app.processEvents()





def test_missing_player_file_keeps_not_found_state_and_does_not_enable_early(tmp_path):
    app = _app()
    instance = SimpleNamespace(path=str(tmp_path))
    widget = players_tab._PlayerListWidget("whitelist.json", allow_add=True)

    try:
        widget.begin_async_load(instance, "whitelist.json", generation=3)
        assert not widget._add_btn.isEnabled()
        widget.apply_async_data(
            3, {"exists": False, "data": None, "error": ""}
        )
        assert not widget._loading
        assert widget._status.text() == "whitelist.json not found"
        assert widget._add_btn.isEnabled()
    finally:
        widget.close()
        app.processEvents()
