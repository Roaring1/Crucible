from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]


def test_main_window_import_only_loads_console_and_players_tab_modules() -> None:
    """MainWindow import must not eagerly pull in every secondary page."""
    script = r"""
import sys
from crucible.ui.main_window import MainWindow
loaded = {
    name.rsplit(".", 1)[-1]
    for name in sys.modules
    if name.startswith("crucible.ui.tabs.")
}
assert loaded == {"console_tab", "players_tab"}, sorted(loaded)

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


def test_instance_panel_defers_six_pages_without_changing_tab_contract(
    tmp_path,
) -> None:
    """Build secondary pages on demand while retaining fixed navigation slots."""
    from PyQt6.QtWidgets import QApplication, QWidget

    from crucible.data.instance_manager import InstanceManager
    from crucible.data.instance_model import ServerInstance
    from crucible.process.log_watcher import LogWatcher
    from crucible.ui.instance_panel import InstancePanel
    from crucible.ui.tabs.players_tab import PlayersTab

    app = QApplication.instance() or QApplication([])
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
        assert isinstance(panel._players, PlayersTab)
        assert panel._tabs.widget(6) is panel._players
        assert set(panel._tab_placeholders) == {1, 2, 3, 4, 5, 7}

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

        assert panel._tab_placeholders == {}
        assert panel._tabs.count() == 8
        assert panel._tabs.widget(6) is panel._players
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

        # Players is deliberately eager: its live-online state must consume
        # join/leave signals while Console remains selected. Persistence is
        # still governed by PlayersTab.load(), a separate lifecycle.
        server_root = tmp_path / "server"
        server_root.mkdir()
        instance = ServerInstance(str(server_root), "Synthetic player history")
        watcher = LogWatcher(instance)
        panel._instance = instance
        panel._watcher = watcher
        panel._players._fetch_avatar = lambda _name: None
        panel._start_watcher(instance)
        assert panel._players._watcher is watcher
        assert panel._tabs.currentIndex() == 0
        watcher.player_joined.emit("HistoryPlayer")
        watcher.player_left.emit("HistoryPlayer")
        record = panel._players._seen["HistoryPlayer"]
        assert record["sessions"] == 1
        assert record["last_seen"] >= record["first_seen"]
        assert "HistoryPlayer" not in panel._players._online

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
