from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from crucible.process.crash_recovery import RUNNING, STOP_REQUESTED, STOPPED_CLEAN
from crucible.process.watchdog import Watchdog


class _Heartbeat:
    def __init__(self, state: str) -> None:
        self.state = state

    def read(self, _instance_id: str) -> dict[str, str]:
        return {"state": self.state}

    def mark_stop_requested(self, _instance_id: str) -> None:
        self.state = STOP_REQUESTED

    def mark_stopped_clean(self, _instance_id: str) -> None:
        self.state = STOPPED_CLEAN

    def mark_running(self, _instance_id: str, _session: str) -> None:
        self.state = RUNNING


def _watchdog(state: str = RUNNING) -> tuple[Watchdog, _Heartbeat]:
    instance = SimpleNamespace(
        id="server-id", tmux_session="server-session", auto_restart=True
    )
    watchdog = Watchdog()
    heartbeat = _Heartbeat(state)
    watchdog._active = True
    watchdog._instances[instance.id] = instance
    watchdog._watching[instance.id] = True
    watchdog._auto_restart[instance.id] = True
    watchdog._miss_count[instance.id] = 0
    watchdog._java_miss_count[instance.id] = 0
    watchdog._heartbeat_state[instance.id] = RUNNING
    watchdog._heartbeats = heartbeat
    watchdog._tmux = MagicMock()
    watchdog._tmux.watchdog_snapshot.return_value = {"server-session": "java"}
    return watchdog, heartbeat


def test_external_stop_request_disarms_crash_restart_while_wrapper_countdown_runs() -> None:
    watchdog, heartbeat = _watchdog(STOP_REQUESTED)
    watchdog._tmux.watchdog_snapshot.return_value = {"server-session": "sleep"}

    watchdog._poll()

    assert watchdog._watching["server-id"] is False
    assert heartbeat.state == STOP_REQUESTED
    assert watchdog._miss_count["server-id"] == 0
    assert watchdog._java_miss_count["server-id"] == 0
    watchdog._tmux.watchdog_snapshot.assert_called_once()
    watchdog._tmux.start.assert_not_called()


def test_watchdog_resumes_only_when_external_stop_failed_and_running_was_restored() -> None:
    watchdog, heartbeat = _watchdog(STOP_REQUESTED)
    watchdog._tmux.watchdog_snapshot.side_effect = [
        {"server-session": "sleep"},
        {"server-session": "java"},
    ]

    watchdog._poll()
    assert watchdog._watching["server-id"] is False

    heartbeat.state = RUNNING
    watchdog._poll()

    assert watchdog._watching["server-id"] is True
    assert watchdog._tmux.watchdog_snapshot.call_count == 2
    watchdog._tmux.start.assert_not_called()


def test_clean_external_stop_is_not_rearmed_by_a_later_external_start() -> None:
    watchdog, heartbeat = _watchdog(STOPPED_CLEAN)

    watchdog._poll()
    assert "server-id" not in watchdog._watching
    assert "server-id" not in watchdog._instances

    heartbeat.state = RUNNING
    watchdog._poll()

    # A fresh run should be registered by the normal GUI readiness signal,
    # not watched from the first tmux session tick while Java is still booting.
    assert "server-id" not in watchdog._watching
    watchdog._tmux.watchdog_snapshot.assert_called_once()


class _Timer:
    def __init__(self) -> None:
        self.active = True
        self.stop_calls = 0
        self.start_calls = []

    def isActive(self) -> bool:
        return self.active

    def stop(self) -> None:
        self.active = False
        self.stop_calls += 1

    def start(self, interval: int) -> None:
        self.active = True
        self.start_calls.append(interval)


def test_watchdog_timer_stops_after_last_selected_server_is_manually_unwatched() -> None:
    watchdog, _heartbeat = _watchdog()
    timer = _Timer()
    watchdog._poll_timer = timer

    watchdog.unwatch("server-id")

    assert not timer.active
    assert timer.stop_calls == 1
    assert not watchdog._instances
    assert not watchdog._watching


def test_watchdog_timer_remains_active_while_external_stop_is_pending() -> None:
    watchdog, _heartbeat = _watchdog(STOP_REQUESTED)
    watchdog._tmux.watchdog_snapshot.return_value = {"server-session": "sleep"}
    timer = _Timer()
    watchdog._poll_timer = timer

    watchdog._poll()

    assert timer.active
    assert timer.stop_calls == 0
    assert watchdog._instances["server-id"]


def test_pending_external_stop_resumes_only_when_java_returns() -> None:
    watchdog, heartbeat = _watchdog(STOP_REQUESTED)
    watchdog._tmux.watchdog_snapshot.side_effect = [
        {"server-session": "sleep"},
        {"server-session": "java"},
    ]

    watchdog._poll()
    assert watchdog._watching["server-id"] is False
    assert heartbeat.state == STOP_REQUESTED

    watchdog._poll()

    assert watchdog._watching["server-id"] is True
    assert heartbeat.state == RUNNING
    assert watchdog._auto_restart["server-id"] is True
    watchdog._tmux.start.assert_not_called()


def test_pending_external_stop_is_cleaned_when_tmux_session_disappears() -> None:
    watchdog, heartbeat = _watchdog(STOP_REQUESTED)
    watchdog._tmux.watchdog_snapshot.return_value = {}

    watchdog._poll()

    assert heartbeat.state == STOPPED_CLEAN
    assert "server-id" not in watchdog._instances
    assert "server-id" not in watchdog._watching


def test_watchdog_records_stop_intent_then_clean_completion() -> None:
    watchdog, heartbeat = _watchdog()

    watchdog.unwatch("server-id")

    assert heartbeat.state == STOP_REQUESTED
    assert "server-id" not in watchdog._instances

    watchdog.stop_completed("server-id")

    assert heartbeat.state == STOPPED_CLEAN
    assert "server-id" not in watchdog._heartbeat_state


def test_auto_restart_policy_is_restored_only_after_java_returns() -> None:
    watchdog, _heartbeat = _watchdog()
    watchdog._auto_restart["server-id"] = False
    watchdog._tmux.watchdog_snapshot.side_effect = [
        {"server-session": "sleep"},
        {"server-session": "java"},
    ]

    watchdog._poll()
    assert watchdog._auto_restart["server-id"] is False

    watchdog._poll()
    assert watchdog._auto_restart["server-id"] is True



def test_watchdog_uses_one_global_snapshot_for_multiple_registered_instances() -> None:
    watchdog, _heartbeat = _watchdog()
    second = SimpleNamespace(
        id="second-server-id",
        tmux_session="second-server-session",
        auto_restart=True,
    )
    watchdog._instances[second.id] = second
    watchdog._watching[second.id] = True
    watchdog._auto_restart[second.id] = True
    watchdog._crash_count[second.id] = 0
    watchdog._miss_count[second.id] = 0
    watchdog._java_miss_count[second.id] = 0
    watchdog._watch_generation[second.id] = 1
    watchdog._heartbeat_state[second.id] = RUNNING
    watchdog._tmux.watchdog_snapshot.return_value = {
        "server-session": "java",
        "second-server-session": "sleep",
    }

    watchdog._poll()

    watchdog._tmux.watchdog_snapshot.assert_called_once_with()
    watchdog._tmux.probe_running.assert_not_called()
    watchdog._tmux.is_java_foreground.assert_not_called()
    assert watchdog._java_miss_count["server-id"] == 0
    assert watchdog._java_miss_count[second.id] == 1
    assert watchdog._crash_count[second.id] == 0


def test_incomplete_global_snapshot_preserves_every_instances_watch_state() -> None:
    watchdog, _heartbeat = _watchdog()
    second = SimpleNamespace(
        id="second-server-id",
        tmux_session="second-server-session",
        auto_restart=True,
    )
    watchdog._instances[second.id] = second
    watchdog._watching[second.id] = True
    watchdog._auto_restart[second.id] = True
    watchdog._crash_count[second.id] = 0
    watchdog._miss_count[second.id] = 0
    watchdog._java_miss_count[second.id] = 0
    watchdog._watch_generation[second.id] = 1
    watchdog._heartbeat_state[second.id] = RUNNING
    watchdog._tmux.watchdog_snapshot.return_value = None

    watchdog._poll()

    watchdog._tmux.watchdog_snapshot.assert_called_once_with()
    for instance_id in ("server-id", second.id):
        assert watchdog._watching[instance_id] is True
        assert watchdog._miss_count[instance_id] == 0
        assert watchdog._java_miss_count[instance_id] == 0
        assert watchdog._crash_count.get(instance_id, 0) == 0
