from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from crucible.ui.tabs.players_tab import _PlayerListWidget
from crucible.ui.tabs.system_tab import SystemTab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_system_tab_does_not_show_offline_when_procfs_scan_is_unknown():
    app = _app()
    tab = SystemTab()
    tab._sampler = SimpleNamespace(
        find_instances_pids=lambda _instances, **_kwargs: None,
        gui_pid=lambda: 101,
        sample_pids=lambda _pids: {},
    )
    instance = SimpleNamespace(id="server-a", path="/srv/server-a")

    with patch(
        "crucible.ui.tabs.system_tab.system_memory_mb",
        return_value=(100.0, 1000.0),
    ):
        tab.load(instance)

    assert tab._server_card["cpu"].text() == "Unavailable"
    assert tab._server_card["mem"].text() == "Unavailable"
    assert tab._server_card["pid"].text() == "Process scan unavailable"

    tab.close()
    app.processEvents()


def test_player_list_does_not_send_console_command_when_procfs_is_unknown():
    app = _app()
    widget = _PlayerListWidget("whitelist.json", allow_add=True)
    instance = SimpleNamespace(
        id="server-a",
        path="/srv/server-a",
        tmux_session="server-a",
    )
    # The production UI enables mutations only after a valid permission-list
    # load (a missing file is a valid empty list). Keep this fixture compatible
    # with the pre-async branch on which PR #69's own CI runs.
    if hasattr(widget, "begin_async_load"):
        widget.begin_async_load(instance, "whitelist.json", generation=1)
        widget.apply_async_data(
            1, {"exists": False, "data": None, "error": ""}
        )
    else:
        widget._instance = instance
    widget._name_input.setText("ExamplePlayer")
    widget._tmux = Mock()
    widget._tmux.probe_running.return_value = False
    widget._tmux._unmanaged_pids.return_value = None

    with (
        patch(
            "crucible.ui.tabs.players_tab.QMessageBox.warning"
        ) as warning,
        patch.object(widget._tmux, "send_command_result") as send,
    ):
        widget._add_player()

    warning.assert_called_once()
    assert warning.call_args.args[1] == "Server process status unavailable"
    assert "No command was sent" in warning.call_args.args[2]
    send.assert_not_called()

    widget.close()
    app.processEvents()
