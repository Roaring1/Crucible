from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from crucible.process.resource_monitor import ProcStat
from crucible.ui.tabs.system_tab import SystemTab


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


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


def test_system_metrics_sampling_runs_off_gui_thread_and_only_when_visible():
    app = _app()
    main_thread = threading.get_ident()
    observed_threads = []
    instance = SimpleNamespace(id="server-a", path="/srv/server-a")

    class FakeSampler:
        def find_instances_pids(self, instances, *, owner_uid=None):
            observed_threads.append(threading.get_ident())
            assert owner_uid == os.getuid()
            assert instances == [instance]
            return {"server-a": [202]}

        def gui_pid(self):
            observed_threads.append(threading.get_ident())
            return 101

        def sample_pids(self, pids):
            observed_threads.append(threading.get_ident())
            assert pids == [202, 101]
            return {
                202: ProcStat(pid=202, rss_mb=512.0, cpu_pct=80.0),
                101: ProcStat(pid=101, rss_mb=128.0, cpu_pct=20.0),
            }

    tab = SystemTab()
    tab._sampler = FakeSampler()

    with patch(
        "crucible.ui.tabs.system_tab.system_memory_mb",
        side_effect=lambda: (
            observed_threads.append(threading.get_ident()) or (2048.0, 8192.0)
        ),
    ):
        try:
            # Server selection while Diagnostics is hidden must not start I/O.
            tab.load(instance)
            assert observed_threads == []
            assert tab._thread is None

            tab.show()
            assert _wait(lambda: tab._thread is None and len(observed_threads) == 4)
            assert all(thread_id != main_thread for thread_id in observed_threads)
            assert tab._server_card["pid"].text() == "PID 202"
            assert tab._server_card["mem"].text() == "512 MB"
            assert tab._gui_card["pid"].text() == "PID 101"
            assert tab._mem_bar.format() == "2.0 GB / 8.0 GB"

            tab.hide()
            count = len(observed_threads)
            tab._refresh()
            app.processEvents()
            assert not tab._timer.isActive()
            assert len(observed_threads) == count
        finally:
            tab.close()
            app.processEvents()


def test_system_metrics_discards_old_server_result_and_runs_latest_request():
    app = _app()
    first = SimpleNamespace(id="server-a", path="/srv/server-a")
    second = SimpleNamespace(id="server-b", path="/srv/server-b")
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    class FakeSampler:
        def find_instances_pids(self, instances, *, owner_uid=None):
            instance = instances[0]
            calls.append(instance.id)
            if instance.id == "server-a":
                first_started.set()
                assert release_first.wait(3.0)
                return {"server-a": [101]}
            return {"server-b": [202]}

        def gui_pid(self):
            return 999

        def sample_pids(self, pids):
            return {
                pid: ProcStat(pid=pid, rss_mb=float(pid), cpu_pct=1.0)
                for pid in pids
            }

    tab = SystemTab()
    tab._sampler = FakeSampler()

    with patch(
        "crucible.ui.tabs.system_tab.system_memory_mb",
        return_value=(1000.0, 8000.0),
    ):
        try:
            tab.load(first)
            tab.show()
            assert first_started.wait(1.0)

            # The old result must not update the newly selected server.
            tab.load(second)
            assert tab._refresh_pending
            tab._refresh()  # a timer tick cannot start an overlapping procfs scan
            assert calls == ["server-a"]
            release_first.set()

            assert _wait(
                lambda: tab._thread is None
                and calls == ["server-a", "server-b"]
                and tab._server_card["pid"].text() == "PID 202"
            )
            assert tab._server_card["mem"].text() == "202 MB"
        finally:
            release_first.set()
            if tab._thread is not None:
                _wait(lambda: tab._thread is None)
            tab.close()
            app.processEvents()


def test_system_metrics_keeps_unknown_procfs_state_distinct_from_offline():
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
        try:
            tab.load(instance)
            tab.show()
            assert _wait(lambda: tab._thread is None)
            assert tab._server_card["cpu"].text() == "Unavailable"
            assert tab._server_card["mem"].text() == "Unavailable"
            assert tab._server_card["pid"].text() == "Process scan unavailable"
        finally:
            tab.close()
            app.processEvents()
