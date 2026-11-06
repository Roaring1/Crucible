from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from crucible.ui.system_tray import SystemTrayController


def _server(name: str, ident: str):
    return SimpleNamespace(
        id=ident,
        name=name,
        tmux_session=ident,
        path=f"/tmp/nonexistent-crucible-server-{ident}",
    )


def _server_submenu(controller: SystemTrayController, name: str):
    for action in controller._servers_menu.actions():
        menu = action.menu()
        if menu is not None and menu.title().startswith(name + " · "):
            return menu
    raise AssertionError(f"No server submenu for {name!r}")


def test_tray_lists_only_registered_servers_and_renders_dynamic_lifecycle_action():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    first = _server("Alpha", "alpha-id")
    second = _server("Beta", "beta-id")
    statuses = {"alpha-id": "running", "beta-id": "stopped"}
    lifecycle = []
    opened = []
    controller = SystemTrayController(
        parent,
        instances=lambda: [first, second],
        statuses=lambda: statuses.copy(),
        open_ui=lambda: opened.append("ui"),
        open_server=lambda item: opened.append(item.id),
        lifecycle=lambda item, action: lifecycle.append((item.id, action)),
        quit_app=lambda: opened.append("quit"),
        gui_pid=os.getpid(),
    )

    try:
        actions = controller._servers_menu.actions()
        assert len(actions) == 2
        alpha_before = _server_submenu(controller, "Alpha")
        controller.refresh_server_menu()
        assert _server_submenu(controller, "Alpha") is alpha_before
        assert [action.menu().title().split(" · ", 1)[0] for action in actions] == [
            "Alpha",
            "Beta",
        ]

        alpha = _server_submenu(controller, "Alpha")
        alpha_text = [action.text() for action in alpha.actions()]
        assert "Status: Running" in alpha_text
        assert "Stop server" in alpha_text
        assert "Start server" not in alpha_text
        assert "Open in Crucible" in alpha_text
        assert "Open folder" in alpha_text

        beta = _server_submenu(controller, "Beta")
        beta_text = [action.text() for action in beta.actions()]
        assert "Status: Stopped" in beta_text
        assert "Start server" in beta_text
        assert "Stop server" not in beta_text

        next(action for action in alpha.actions() if action.text() == "Stop server").trigger()
        next(action for action in beta.actions() if action.text() == "Start server").trigger()
        assert lifecycle == [("alpha-id", "stop"), ("beta-id", "start")]

        next(action for action in alpha.actions() if action.text() == "Open in Crucible").trigger()
        assert opened == ["alpha-id"]

        # State is refreshed from the shared status provider, not cached locally.
        statuses["alpha-id"] = "stopped"
        controller.refresh_server_menu(force=True)
        alpha = _server_submenu(controller, "Alpha")
        alpha_text = [action.text() for action in alpha.actions()]
        assert "Status: Stopped" in alpha_text
        assert "Start server" in alpha_text
        assert "Stop server" not in alpha_text
    finally:
        controller.shutdown()
        parent.close()
        parent.deleteLater()
        app.processEvents()


def test_tray_disables_lifecycle_actions_for_uncertain_and_transitional_states():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    names = ["Checking", "Starting", "Stopping", "Unknown", "Unmanaged"]
    instances = [_server(name, name.lower()) for name in names]
    statuses = {
        "checking": "checking",
        "starting": "starting",
        "stopping": "stopping",
        "unknown": "unknown",
        "unmanaged": "unmanaged",
    }
    controller = SystemTrayController(
        parent,
        instances=lambda: instances,
        statuses=lambda: statuses,
        open_ui=lambda: None,
        open_server=lambda _item: None,
        lifecycle=lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")),
        quit_app=lambda: None,
        gui_pid=os.getpid(),
    )
    try:
        for name in names:
            menu = _server_submenu(controller, name)
            texts = [action.text() for action in menu.actions()]
            assert "Start server" not in texts
            assert "Stop server" not in texts
            pending = [
                action for action in menu.actions()
                if action.text() in {
                    "Checking…",
                    "Starting…",
                    "Stopping…",
                    "Lifecycle action unavailable",
                }
            ]
            assert pending and not pending[0].isEnabled()
    finally:
        controller.shutdown()
        parent.close()
        parent.deleteLater()
        app.processEvents()
