from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from crucible import __main__ as cli


class FakeHeartbeatStore:
    def __init__(self):
        self.events = []

    def mark_running(self, instance_id, session):
        self.events.append(("running", instance_id, session))

    def mark_stop_requested(self, instance_id):
        self.events.append(("stop_requested", instance_id))

    def mark_stopped_clean(self, instance_id):
        self.events.append(("stopped_clean", instance_id))


@pytest.fixture(autouse=True)
def isolate_heartbeat_store(monkeypatch):
    store = FakeHeartbeatStore()
    monkeypatch.setattr(cli, "HeartbeatStore", lambda: store)
    return store


class FakeManager:
    def __init__(self, instance):
        self.instance = instance
        self.updated = []

    def get_by_name_or_id(self, key):
        return self.instance if key in {self.instance.id, self.instance.name} else None

    def update_instance(self, instance):
        self.updated.append(instance)


class FakeTmux:
    def __init__(self, status="stopped", result=(True, "ok")):
        self.status = status
        self.status_after = None
        self.status_calls = 0
        self.result = result
        self.start_calls = []
        self.stop_calls = []

    def get_status(self, _instance):
        self.status_calls += 1
        if self.status_after is not None and self.status_calls > 1:
            return self.status_after
        return self.status

    def start(self, instance, **kwargs):
        self.start_calls.append((instance, kwargs))
        return self.result

    def stop(self, instance, **kwargs):
        self.stop_calls.append((instance, kwargs))
        return self.result

    def is_java_foreground(self, _instance):
        return getattr(self, "java_foreground", True)


def _setup(status="stopped", action="start"):
    instance = SimpleNamespace(
        id="instance-uuid",
        name="Test server",
        tmux_session="instance-uuid",
        fml_query_result="ask",
        extra={},
    )
    manager = FakeManager(instance)
    tmux = FakeTmux(status)
    args = SimpleNamespace(action=action, instance_id=instance.id)
    return instance, manager, tmux, args


def test_start_shortcut_only_starts_confirmed_stopped_server(monkeypatch, isolate_heartbeat_store) -> None:
    instance, manager, tmux, args = _setup()
    notices = []
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: notices.append((a, k)))
    monkeypatch.setattr(cli, "_preflight_properties", lambda _instance: None)
    monkeypatch.setattr(cli, "inspect_instance_world_start", lambda _instance: SimpleNamespace(
            state="existing",
            requires_confirmation=False,
            error="",
            world_name="world",
            other_developed_worlds=(),
            world_path=Path("/tmp/server/world"),
        ))

    cli.cmd_shortcut_action(manager, tmux, args)

    assert len(tmux.start_calls) == 1
    assert tmux.start_calls[0][0] is instance
    assert tmux.start_calls[0][1] == {"fml_query_result": "ask"}
    assert manager.updated == [instance]
    assert isolate_heartbeat_store.events == [("running", instance.id, instance.tmux_session)]
    assert notices


@pytest.mark.parametrize("status", ["running", "starting", "stopping", "unknown", "unmanaged", "missing"])
def test_start_shortcut_refuses_any_non_stopped_state(monkeypatch, status) -> None:
    _instance, manager, tmux, args = _setup(status=status)
    notices = []
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: notices.append((a, k)))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_shortcut_action(manager, tmux, args)

    assert exc.value.code == 1
    assert tmux.start_calls == []
    assert notices


def test_stop_shortcut_is_graceful_only_and_requires_running(monkeypatch, isolate_heartbeat_store) -> None:
    instance, manager, tmux, args = _setup(status="running", action="stop")
    notices = []
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: notices.append((a, k)))

    cli.cmd_shortcut_action(manager, tmux, args)

    assert len(tmux.stop_calls) == 1
    assert tmux.stop_calls[0][0] is instance
    assert tmux.stop_calls[0][1] == {"graceful": True, "timeout_s": 90}
    assert manager.updated == []
    assert isolate_heartbeat_store.events == [
        ("stop_requested", instance.id),
        ("stopped_clean", instance.id),
    ]
    assert notices


@pytest.mark.parametrize("status", ["stopped", "starting", "stopping", "unknown", "unmanaged", "missing"])
def test_stop_shortcut_refuses_any_non_running_state(monkeypatch, status) -> None:
    _instance, manager, tmux, args = _setup(status=status, action="stop")
    notices = []
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: notices.append((a, k)))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_shortcut_action(manager, tmux, args)

    assert exc.value.code == 1
    assert tmux.stop_calls == []
    assert notices


def test_cli_world_start_confirmation_requires_typed_start(monkeypatch):
    summary = SimpleNamespace(
        state="new",
        requires_confirmation=True,
        error="",
        world_name="fresh-world",
        previous_world_name="old-world",
        other_developed_worlds=("old-world",),
        world_path=Path("/tmp/server/fresh-world"),
    )
    monkeypatch.setattr(cli, "inspect_instance_world_start", lambda _instance: summary)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "START")

    assert cli._confirm_cli_world_start(SimpleNamespace()) == "fresh-world"


def test_cli_world_start_confirmation_cancels_noninteractive_start(monkeypatch):
    summary = SimpleNamespace(
        state="new",
        requires_confirmation=True,
        error="",
        world_name="fresh-world",
        previous_world_name=None,
        other_developed_worlds=(),
        world_path=Path("/tmp/server/fresh-world"),
    )
    monkeypatch.setattr(cli, "inspect_instance_world_start", lambda _instance: summary)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    with pytest.raises(SystemExit) as exc:
        cli._confirm_cli_world_start(SimpleNamespace())
    assert exc.value.code == 1


def test_start_shortcut_refuses_unconfirmed_world_selection(monkeypatch):
    _instance, manager, tmux, args = _setup()
    notices = []
    summary = SimpleNamespace(
        state="new",
        requires_confirmation=True,
        error="",
        world_name="fresh-world",
        other_developed_worlds=("developed-world",),
        world_path=Path("/tmp/server/fresh-world"),
    )
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: notices.append((a, k)))
    monkeypatch.setattr(cli, "inspect_instance_world_start", lambda _instance: summary)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_shortcut_action(manager, tmux, args)

    assert exc.value.code == 1
    assert tmux.start_calls == []
    assert notices and notices[0][0][0] == "World selection needs review"


def test_start_failure_is_reported_and_registry_is_not_updated(monkeypatch, isolate_heartbeat_store) -> None:
    _instance, manager, tmux, args = _setup()
    tmux.result = (False, "server start failed")
    notices = []
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: notices.append((a, k)))
    monkeypatch.setattr(cli, "_preflight_properties", lambda _instance: None)
    monkeypatch.setattr(cli, "inspect_instance_world_start", lambda _instance: SimpleNamespace(
            state="existing",
            requires_confirmation=False,
            error="",
            world_name="world",
            other_developed_worlds=(),
            world_path=Path("/tmp/server/world"),
        ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_shortcut_action(manager, tmux, args)

    assert exc.value.code == 1
    assert manager.updated == []
    assert isolate_heartbeat_store.events == []
    assert notices[0][0][1] == "Test server: server start failed"


def test_failed_graceful_stop_restores_watchdog_heartbeat(monkeypatch, isolate_heartbeat_store) -> None:
    _instance, _manager, tmux, args = _setup(status="running", action="stop")
    tmux.result = (False, "server did not stop")
    notices = []
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: notices.append((a, k)))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_shortcut_action(_manager, tmux, args)

    assert exc.value.code == 1
    assert isolate_heartbeat_store.events == [
        ("stop_requested", "instance-uuid"),
        ("running", "instance-uuid", "instance-uuid"),
    ]
    assert notices


def test_uncertain_stop_failure_keeps_watchdog_disarmed(monkeypatch, isolate_heartbeat_store) -> None:
    _instance, _manager, tmux, args = _setup(status="running", action="stop")
    tmux.result = (False, "tmux status became unavailable")
    tmux.status_after = "unknown"
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: None)

    with pytest.raises(SystemExit):
        cli.cmd_shortcut_action(_manager, tmux, args)

    assert isolate_heartbeat_store.events == [("stop_requested", "instance-uuid")]


def test_failed_stop_does_not_rearm_heartbeat_while_wrapper_java_is_gone(
    monkeypatch, isolate_heartbeat_store
) -> None:
    _instance, _manager, tmux, args = _setup(status="running", action="stop")
    tmux.result = (False, "wrapper countdown did not exit")
    tmux.java_foreground = False
    monkeypatch.setattr(cli, "_shortcut_notification", lambda *a, **k: None)

    with pytest.raises(SystemExit):
        cli.cmd_shortcut_action(_manager, tmux, args)

    assert isolate_heartbeat_store.events == [("stop_requested", "instance-uuid")]
