from __future__ import annotations

from types import SimpleNamespace

from crucible.ui.main_window import MainWindow


class _Sidebar:
    def __init__(self):
        self.statuses = {}

    def update_status(self, instance_id, status):
        self.statuses[instance_id] = status

    def update_all_statuses(self, statuses):
        self.statuses = dict(statuses)

    def selected_instance(self):
        return None


class _Panel:
    _current_status = "stopped"

    def current_instance_id(self):
        return None

    def update_status(self, _status):
        pass


def _window():
    return SimpleNamespace(
        _panel=_Panel(),
        _sidebar=_Sidebar(),
        _tray_controller=None,
        _status_map={},
        _transient_status={},
        _health_busy=True,
        _update_status_bar=lambda: None,
    )


def test_non_selected_stopping_state_survives_periodic_health_poll():
    window = _window()

    MainWindow._on_status_changed(window, "server-b", "stopping")
    assert window._status_map["server-b"] == "stopping"
    assert window._transient_status["server-b"] == "stopping"

    # The tmux session still exists, so the next coarse health sample says
    # running. The in-flight Stop operation remains the authoritative intent.
    MainWindow._apply_health_status(window, {"server-b": "running"})

    assert window._status_map["server-b"] == "stopping"
    assert window._sidebar.statuses["server-b"] == "stopping"


def test_terminal_status_clears_transient_override():
    window = _window()
    MainWindow._on_status_changed(window, "server-b", "stopping")

    MainWindow._on_status_changed(window, "server-b", "stopped")
    MainWindow._apply_health_status(window, {"server-b": "stopped"})

    assert "server-b" not in window._transient_status
    assert window._status_map["server-b"] == "stopped"
    assert window._sidebar.statuses["server-b"] == "stopped"
