from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]


def test_main_window_import_only_loads_console_tab_module() -> None:
    """MainWindow import must not eagerly pull in every secondary page."""
    script = r"""
import sys
from crucible.ui.main_window import MainWindow
loaded = {
    name.rsplit(".", 1)[-1]
    for name in sys.modules
    if name.startswith("crucible.ui.tabs.")
}
assert loaded == {"console_tab"}, sorted(loaded)

# User-invoked dialogs and optional tray telemetry are also on demand.
for name in (
    "crucible.ui.add_dialog",
    "crucible.ui.shortcuts_dialog",
    "crucible.ui.system_tray",
    "crucible.process.machine_resources",
):
    assert name not in sys.modules, name

# Public package exports still resolve on demand without importing unrelated
# pages alongside the one requested.
from crucible.ui import tabs
assert "crucible.ui.tabs.mods_tab" not in sys.modules
mods_tab = tabs.ModsTab
assert mods_tab.__name__ == "ModsTab"
assert "crucible.ui.tabs.mods_tab" in sys.modules
assert "crucible.ui.tabs.world_tab" not in sys.modules
assert "crucible.ui.tabs.backup_tab" not in sys.modules
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_instance_panel_defers_seven_pages_without_changing_tab_contract(
    tmp_path,
) -> None:
    """Build seven secondary pages on demand while retaining fixed tab slots."""
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget

    from crucible.data.instance_manager import InstanceManager
    from crucible.data.instance_model import ServerInstance
    from crucible.process.log_watcher import LogWatcher
    from crucible.ui.instance_panel import InstancePanel

    app = QApplication.instance() or QApplication([])

    def wait_for_history_load(players) -> None:
        deadline = time.monotonic() + 5.0
        while getattr(players, "_seen_loading", False) and time.monotonic() < deadline:
            QTest.qWait(5)
        assert not getattr(players, "_seen_loading", False), (
            "Players history worker did not finish"
        )

    manager = InstanceManager(tmp_path / "registry")
    manager.load()
    panel = InstancePanel(manager)

    try:
        assert panel._tabs.count() == 8
        assert [panel._tabs.tabText(index) for index in range(8)] == [
            "Console",
            "Mods",
            "Worlds",
            "Setup",
            "Diagnostics",
            "Config",
            "Players",
            "Notes",
        ]
        assert panel._mods is None
        assert panel._world is None
        assert panel._setup is None
        assert panel._info is None
        assert panel._config is None
        assert panel._notes is None
        assert panel._players is None
        assert panel._tabs.widget(6).objectName() == "LazyWorkspacePage"
        assert set(panel._tab_placeholders) == {1, 2, 3, 4, 5, 6, 7}

        assert panel._has_mutating_operations() is False
        assert panel._confirm_config_changes() is True

        # Selecting a placeholder must construct that page synchronously,
        # without moving it to a different index or replacing the active page.
        first_placeholder = panel._tabs.widget(1)
        assert first_placeholder.objectName() == "LazyWorkspacePage"
        panel._tabs.setCurrentIndex(1)
        app.processEvents()
        assert panel._mods is not None
        assert panel._tabs.widget(1) is panel._mods
        panel._tabs.setCurrentIndex(0)
        app.processEvents()

        names = {
            2: "_world",
            3: "_setup",
            4: "_info",
            5: "_config",
            7: "_notes",
        }
        for index, attribute in names.items():
            placeholder = panel._tabs.widget(index)
            assert placeholder.objectName() == "LazyWorkspacePage"
            page = panel._ensure_tab_created(index)
            assert page is getattr(panel, attribute)
            assert isinstance(page, QWidget)
            assert panel._tabs.widget(index) is page
            assert panel._tabs.currentIndex() == 0

        assert set(panel._tab_placeholders) == {6}
        assert panel._tabs.count() == 8
        assert panel._players is None
        assert panel._tabs.widget(6).objectName() == "LazyWorkspacePage"
        assert [panel._tabs.tabText(index) for index in range(8)] == [
            "Console",
            "Mods",
            "Worlds",
            "Setup",
            "Diagnostics",
            "Config",
            "Players",
            "Notes",
        ]
        assert not panel._tabs.tabBar().isTabVisible(5)
        assert not panel._tabs.tabBar().isTabVisible(6)
        assert not panel._tabs.tabBar().isTabVisible(7)

        # Console owns the live watcher from server selection. Events received
        # before Players is opened are reflected in its first online snapshot.
        server_root = tmp_path / "server"
        server_root.mkdir()
        instance = ServerInstance(str(server_root), "Synthetic player history")
        watcher = LogWatcher(instance)
        panel._instance = instance
        panel._watcher = watcher
        panel._attach_player_history_tracker(instance)
        panel._console.attach(instance, watcher)
        assert panel._players is None
        watcher.player_joined.emit("AlreadyOnline")
        assert "AlreadyOnline" in panel._console._active_players
        watcher.player_joined.emit("FinishedBeforeOpen")
        watcher.player_left.emit("FinishedBeforeOpen")
        watcher.player_joined.emit("StoppedBeforeOpen")
        watcher.server_stopping.emit()
        assert not panel._console._active_players
        watcher.player_joined.emit("AlreadyOnline")
        history_path = server_root / ".crucible" / "players_seen.json"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if history_path.exists():
                try:
                    persisted_before_open = json.loads(
                        history_path.read_text(encoding="utf-8")
                    )
                    if persisted_before_open.get("AlreadyOnline", {}).get(
                        "sessions"
                    ) == 2:
                        break
                except (OSError, json.JSONDecodeError):
                    pass
            QTest.qWait(10)
        else:
            raise AssertionError("coalesced player history was not persisted")
        assert persisted_before_open["AlreadyOnline"]["sessions"] == 2
        assert persisted_before_open["FinishedBeforeOpen"]["sessions"] == 1
        assert persisted_before_open["StoppedBeforeOpen"]["sessions"] == 1
        assert set(panel._player_history_tracker.join_times) == {"AlreadyOnline"}

        players_placeholder = panel._tabs.widget(6)
        assert players_placeholder.objectName() == "LazyWorkspacePage"
        players = panel._ensure_tab_created(6)
        assert players is panel._players
        assert panel._tab_placeholders == {}
        assert panel._tabs.currentIndex() == 0
        assert not panel._tabs.tabBar().isTabVisible(6)
        players._fetch_avatar = lambda _name: None
        panel._tabs.setCurrentIndex(6)
        app.processEvents()
        wait_for_history_load(players)
        assert players._instance is instance
        assert players._watcher is None, (
            "Players receives Console's GUI-thread relay, not raw watcher events"
        )
        assert panel._player_history_tracker is None
        assert "AlreadyOnline" in players._online
        assert players._seen["AlreadyOnline"]["sessions"] == 2
        assert players._seen["FinishedBeforeOpen"]["sessions"] == 1
        assert players._seen["StoppedBeforeOpen"]["sessions"] == 1
        assert "AlreadyOnline" in players._join_times
        assert "FinishedBeforeOpen" not in players._join_times

        # After the page is open, joins/leaves are relayed and history is
        # persisted against the currently selected instance.
        watcher.player_joined.emit("NewJoin")
        assert "NewJoin" in players._online
        assert players._seen["NewJoin"]["sessions"] == 1
        watcher.player_left.emit("NewJoin")
        assert "NewJoin" not in players._online
        assert (
            players._seen["NewJoin"]["last_seen"]
            >= players._seen["NewJoin"]["first_seen"]
        )

        # Switch the selected server with Players already open. The old
        # relay must disconnect before the new Console watcher starts.
        second_root = tmp_path / "second-server"
        second_root.mkdir()
        second_instance = ServerInstance(str(second_root), "Second synthetic server")
        old_player_relay = panel._console._player_event_relay
        reused_watcher = watcher  # InstancePanel intentionally reuses this QObject.

        def attach_second_server(selected_instance):
            assert selected_instance is second_instance
            panel._watcher = reused_watcher
            panel._console.attach(selected_instance, reused_watcher)

        with (
            patch.object(panel, "_ensure_watchdog", lambda: None),
            patch.object(panel, "_request_world_start_summary", lambda *a, **k: None),
            patch.object(panel, "_start_watcher", new=attach_second_server),
        ):
            assert panel.load(second_instance, status_hint="stopped")

        # Simulate an event that had already been queued to the prior relay
        # before detach. The same LogWatcher object is now attached to server 2,
        # so sender identity alone cannot distinguish these two generations.
        assert old_player_relay is not None
        old_player_relay.player_joined("LateOldServerEvent")
        assert "LateOldServerEvent" not in panel._console._active_players
        assert "LateOldServerEvent" not in panel._player_history_tracker.join_times
        reused_watcher.player_joined.emit("SecondServerPlayer")
        assert "SecondServerPlayer" in panel._console._active_players
        assert "SecondServerPlayer" not in players._seen, (
            "Players must remain detached while it still represents the old instance"
        )

        panel._tabs.setCurrentIndex(6)
        app.processEvents()
        wait_for_history_load(players)
        assert players._instance is second_instance
        assert "SecondServerPlayer" in players._online
        assert players._seen["SecondServerPlayer"]["sessions"] == 1
        assert "SecondServerPlayer" in players._join_times, (
            "the background history tracker must transfer the real join time"
        )

        # World operations must retain the Config editor's save/discard guard
        # even though the Config page can be constructed later.
        config = panel._config
        assert panel._world._config_guard is not None
        assert panel._world._config_guard() is True
        decisions: list[str] = []
        config.confirm_discard_or_save = lambda: decisions.append("guard") or False
        config.reload_from_disk = lambda: decisions.append("reload")
        assert panel._world._config_guard() is False
        panel._world._config_reload()
        assert decisions == ["guard", "reload"]

    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_deferred_player_history_preserves_oversized_source_and_batches_stop(
    tmp_path,
) -> None:
    from crucible.data.player_history import (
        PlayerHistoryTracker,
        _MAX_PLAYER_JSON_BYTES,
    )

    server_root = tmp_path / "server"
    history_path = server_root / ".crucible" / "players_seen.json"
    history_path.parent.mkdir(parents=True)
    oversized_bytes = b" " * (_MAX_PLAYER_JSON_BYTES + 1)
    history_path.write_bytes(oversized_bytes)

    tracker = PlayerHistoryTracker(server_root)
    tracker.record_joined("FirstPlayer")
    assert tracker._seen is not None
    assert tracker._seen["FirstPlayer"]["sessions"] == 1
    assert tracker.load_error
    assert tracker.dirty
    tracker.record_server_stopped()
    assert history_path.read_bytes() == oversized_bytes
    assert tracker.dirty
    assert not tracker.flush()
    assert history_path.read_bytes() == oversized_bytes
    assert not tracker.close()
    tracker.record_joined("SecondPlayer")
    assert "SecondPlayer" in tracker.join_times
    assert history_path.read_bytes() == oversized_bytes

    # A valid source is updated once when multiple active sessions stop,
    # rather than rewriting the same JSON once per player.
    valid_root = tmp_path / "valid-server"
    valid_path = valid_root / ".crucible" / "players_seen.json"
    valid_path.parent.mkdir(parents=True)
    valid_path.write_text('{"PriorPlayer": {"sessions": 3}}', encoding="utf-8")
    valid = PlayerHistoryTracker(valid_root)
    valid.record_joined("FirstPlayer")
    valid.record_joined("SecondPlayer")
    saves = 0
    original_save = valid._save

    def count_save() -> bool:
        nonlocal saves
        saves += 1
        return original_save()

    valid._save = count_save
    valid.record_server_stopped()
    assert saves == 1
    assert valid.join_times == {}
    assert valid.seen_snapshot["FirstPlayer"]["seconds_played"] >= 0
    assert valid.seen_snapshot["SecondPlayer"]["seconds_played"] >= 0
    assert valid.flush()
    assert valid.close()
    valid.record_joined("AfterClose")
    assert "AfterClose" not in valid.join_times
    assert not valid.dirty

def test_player_history_retry_merges_unpersisted_session_delta(tmp_path) -> None:
    from crucible.data.player_history import (
        PlayerHistoryTracker,
        _MAX_PLAYER_JSON_BYTES,
    )

    server_root = tmp_path / "server"
    history_path = server_root / ".crucible" / "players_seen.json"
    history_path.parent.mkdir(parents=True)
    history_path.write_bytes(b" " * (_MAX_PLAYER_JSON_BYTES + 1))

    tracker = PlayerHistoryTracker(server_root)
    tracker.record_joined("KnownPlayer")
    assert tracker.load_error
    assert tracker.dirty

    # Simulate the file becoming readable after the original bounded-read
    # failure. The session observed in memory must be added to disk's old total.
    history_path.write_text(
        '{"KnownPlayer": {"sessions": 4, "seconds_played": 12.5}}',
        encoding="utf-8",
    )
    assert tracker.flush()
    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    assert persisted["KnownPlayer"]["sessions"] == 5
    assert persisted["KnownPlayer"]["seconds_played"] == 12.5
    assert not tracker.dirty
    assert tracker.close()


def test_history_tracker_disconnect_requires_successful_flush() -> None:
    from types import SimpleNamespace

    from crucible.ui.instance_panel import InstancePanel

    class Signal:
        def __init__(self) -> None:
            self.connected = []
            self.disconnected = []

        def connect(self, slot) -> None:
            self.connected.append(slot)

        def disconnect(self, slot) -> None:
            self.disconnected.append(slot)

    class Tracker:
        def __init__(self) -> None:
            self.closed = False
            self.allow_close = False

        def record_joined(self, _name: str) -> None:
            pass

        def record_left(self, _name: str) -> None:
            pass

        def record_server_stopped(self) -> None:
            pass

        def close(self) -> bool:
            self.closed = True
            return self.allow_close

    signals = [Signal(), Signal(), Signal()]
    tracker = Tracker()
    panel = SimpleNamespace(
        _console=SimpleNamespace(
            online_player_joined=signals[0],
            online_player_left=signals[1],
            online_players_cleared=signals[2],
        ),
        _player_history_tracker=tracker,
    )

    # A failed flush must leave the tracker attached and restore its signals.
    assert InstancePanel._disconnect_player_history_tracker(panel) is False
    assert panel._player_history_tracker is tracker
    assert signals[0].connected == [tracker.record_joined]
    assert signals[1].connected == [tracker.record_left]
    assert signals[2].connected == [tracker.record_server_stopped]

    # Once publication succeeds, retire the tracker and detach every signal.
    tracker.allow_close = True
    assert InstancePanel._disconnect_player_history_tracker(panel) is True
    assert panel._player_history_tracker is None
    assert signals[0].disconnected == [tracker.record_joined, tracker.record_joined]
    assert signals[1].disconnected == [tracker.record_left, tracker.record_left]
    assert signals[2].disconnected == [
        tracker.record_server_stopped,
        tracker.record_server_stopped,
    ]


def test_instance_selection_never_runs_synchronous_tmux_status_probe(tmp_path) -> None:
    """Unknown or absent health hints must not block selection on tmux/procfs."""
    from PyQt6.QtWidgets import QApplication

    from crucible.data.instance_manager import InstanceManager
    from crucible.data.instance_model import ServerInstance
    from crucible.ui.instance_panel import InstancePanel

    app = QApplication.instance() or QApplication([])
    manager = InstanceManager(tmp_path / "registry")
    manager.load()
    root = tmp_path / "server"
    root.mkdir()
    instance = ServerInstance(str(root), "Synthetic server")
    panel = InstancePanel(manager)

    try:
        with (
            patch.object(
                panel._tmux,
                "get_status",
                side_effect=AssertionError("GUI selection must not run tmux/procfs"),
            ),
            patch.object(panel, "_start_watcher", lambda _instance: None),
            patch.object(panel, "_set_identity_icon", lambda _instance: None),
        ):
            assert panel.load(instance, status_hint="unknown")
            assert panel._current_status == "unknown"

            # An absent hint is not permission to synchronously probe either.
            second_root = tmp_path / "second-server"
            second_root.mkdir()
            second = ServerInstance(str(second_root), "Second synthetic server")
            assert panel.load(second, status_hint=None)
            assert panel._current_status == "checking"
    finally:
        panel._disconnect_player_history_tracker()
        panel.shutdown()
        panel.deleteLater()
        app.processEvents()


def test_registered_instance_uses_cached_status_and_health_worker() -> None:
    """Post-import UI registration must not synchronously probe tmux."""
    from types import SimpleNamespace

    from crucible.data.instance_model import ServerInstance
    from crucible.ui.main_window import MainWindow

    class Sidebar:
        def __init__(self):
            self._items = {}
            self.added = []
            self.updated = []
            self.selected = []

        def add_instance(self, instance, status):
            self.added.append((instance, status))
            self._items[instance.id] = object()

        def update_status(self, instance_id, status):
            self.updated.append((instance_id, status))

        def select_by_id(self, instance_id):
            self.selected.append(instance_id)

    sidebar = Sidebar()
    health_checks = []
    fake_window = SimpleNamespace(
        _sidebar=sidebar,
        _status_map={},
        _health_check=lambda: health_checks.append("queued"),
    )
    instance = ServerInstance("/tmp/synthetic-registered", "Imported")

    already_listed = MainWindow._show_registered_instance(fake_window, instance)
    assert not already_listed
    assert sidebar.added == [(instance, "checking")]
    assert sidebar.selected == [instance.id]
    assert health_checks == ["queued"]

    fake_window._status_map[instance.id] = "unknown"
    already_listed = MainWindow._show_registered_instance(fake_window, instance)
    assert already_listed
    assert sidebar.updated == [(instance.id, "unknown")]
    assert sidebar.added == [(instance, "checking")]
    assert health_checks == ["queued", "queued"]
