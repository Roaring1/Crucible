from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from crucible.data.instance_manager import InstanceManager
from crucible.ui.main_window import MainWindow
from crucible.ui.system_tray import SystemTrayController


def _window():
    app = QApplication.instance() or QApplication([])
    settings_root = tempfile.mkdtemp(prefix="crucible-tray-settings-")
    QSettings.setPath(
        QSettings.Format.NativeFormat,
        QSettings.Scope.UserScope,
        settings_root,
    )
    manager = InstanceManager(
        config_dir=Path(tempfile.mkdtemp(prefix="crucible-tray-registry-"))
    )
    manager.load()
    with (
        patch.object(MainWindow, "_run_crash_recovery", lambda _self: None),
        patch.object(SystemTrayController, "is_available", return_value=False),
    ):
        window = MainWindow(manager)
    app.processEvents()
    return app, window


def test_tray_stop_uses_non_selected_graceful_worker_without_showing_ui() -> None:
    app, window = _window()
    instance = SimpleNamespace(
        id="server-b", name="Server B", path="/tmp/server-b", tmux_session="server-b"
    )
    window._manager.instances = [instance]
    shown = Mock()
    selected = Mock()
    stop = Mock()
    window._show_from_tray = shown
    window._sidebar.select_by_id = selected
    window._panel.current_instance_id = lambda: "server-a"
    window._panel.has_active_operations = lambda: False
    window._panel._do_stop_for = stop
    window._status_map = {instance.id: "running"}

    try:
        window._run_tray_lifecycle(instance, "stop")

        shown.assert_not_called()
        selected.assert_not_called()
        stop.assert_called_once_with(
            instance,
            status_hint="running",
            graceful_only=True,
        )
    finally:
        window.close()
        app.processEvents()


def test_tray_start_restores_ui_and_selects_the_requested_server() -> None:
    app, window = _window()
    instance = SimpleNamespace(
        id="server-b", name="Server B", path="/tmp/server-b", tmux_session="server-b"
    )
    window._manager.instances = [instance]
    shown = Mock()
    selected = Mock()
    start = Mock()
    window._show_from_tray = shown
    window._sidebar.select_by_id = selected
    window._panel.current_instance_id = lambda: "server-a"
    window._panel.has_active_operations = lambda: False
    window._panel._do_start_for = start
    window._status_map = {instance.id: "stopped"}

    try:
        window._run_tray_lifecycle(instance, "start")

        shown.assert_called_once_with()
        selected.assert_called_once_with(instance.id)
        start.assert_called_once_with(instance, status_hint="stopped")
    finally:
        window.close()
        app.processEvents()


def test_tray_lifecycle_refuses_unknown_state_without_dispatch() -> None:
    app, window = _window()
    instance = SimpleNamespace(
        id="server-b", name="Server B", path="/tmp/server-b", tmux_session="server-b"
    )
    window._manager.instances = [instance]
    message = Mock()
    start = Mock()
    stop = Mock()
    window._tray_message = message
    window._panel.has_active_operations = lambda: False
    window._panel._do_start_for = start
    window._panel._do_stop_for = stop
    window._status_map = {instance.id: "unknown"}

    try:
        window._run_tray_lifecycle(instance, "start")
        window._run_tray_lifecycle(instance, "stop")

        assert message.call_count == 2
        start.assert_not_called()
        stop.assert_not_called()
    finally:
        window.close()
        app.processEvents()


def test_tray_lifecycle_rejects_removed_or_replaced_registry_target() -> None:
    app, window = _window()
    stale = SimpleNamespace(
        id="server-b", name="Server B", path="/tmp/old-server-b", tmux_session="old-session"
    )
    current = SimpleNamespace(
        id="server-b", name="Server B", path="/tmp/new-server-b", tmux_session="new-session"
    )
    message = Mock()
    start = Mock()
    stop = Mock()
    window._tray_message = message
    window._manager.instances = [current]
    window._panel.has_active_operations = lambda: False
    window._panel._do_start_for = start
    window._panel._do_stop_for = stop
    window._status_map = {stale.id: "running"}

    try:
        window._run_tray_lifecycle(stale, "stop")
        assert message.call_count == 1
        start.assert_not_called()
        stop.assert_not_called()
    finally:
        window.close()
        app.processEvents()
