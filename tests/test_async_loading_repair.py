from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication, QEventLoop, QThread

from crucible.ui.main_window import MainWindow, _LoadingRepairWorker


def _run_worker(worker):
    app = QCoreApplication.instance() or QCoreApplication([])
    thread = QThread()
    worker.moveToThread(thread)
    loop = QEventLoop()
    results = []
    worker.finished.connect(lambda *args: (results.append(args), loop.quit()))
    worker.finished.connect(worker.deleteLater)
    worker.finished.connect(thread.quit)
    thread.started.connect(worker.run)
    thread.start()
    loop.exec()
    assert thread.wait(3000)
    thread.deleteLater()
    return app, results


def test_loading_diagnosis_runs_off_gui_thread_and_never_applies():
    instance = SimpleNamespace(id="server-a", path="/tmp/server-a")
    observed = {}
    tmux = SimpleNamespace(
        get_status=lambda _instance: (
            observed.setdefault("status_thread", QThread.currentThread()) and "stopped"
        )
    )
    diagnosis = SimpleNamespace(diagnosis=SimpleNamespace(found_crash=True))
    with patch(
        "crucible.diagnostics.loadcheck.autofix_loading",
        side_effect=lambda path, *, apply: (
            observed.update(
                path=path, apply=apply, diagnosis_thread=QThread.currentThread()
            )
            or diagnosis
        ),
    ):
        app, results = _run_worker(_LoadingRepairWorker(instance, "diagnose", tmux))

    assert len(results) == 1
    assert results[0][0:2] == ("server-a", "diagnose")
    assert results[0][2] is diagnosis
    assert results[0][3] == ""
    assert observed["path"] == instance.path
    assert observed["apply"] is False
    assert observed["status_thread"] is observed["diagnosis_thread"]
    assert observed["diagnosis_thread"] is not app.thread()


def test_loading_repair_rechecks_stopped_state_before_quarantine():
    instance = SimpleNamespace(id="server-b", path="/tmp/server-b")
    calls = []
    tmux = SimpleNamespace(get_status=lambda _instance: "running")
    with patch(
        "crucible.diagnostics.loadcheck.autofix_loading",
        side_effect=lambda *args, **kwargs: calls.append((args, kwargs)),
    ):
        _app, results = _run_worker(_LoadingRepairWorker(instance, "apply", tmux))

    assert results == [("server-b", "apply", {"status": "running"}, "")]
    assert calls == []


def test_loading_repair_applies_only_after_worker_confirms_stopped():
    instance = SimpleNamespace(id="server-c", path="/tmp/server-c")
    applied = SimpleNamespace(quarantined=["client-only.jar"], unresolved=[])
    observed = {}
    tmux = SimpleNamespace(get_status=lambda _instance: "stopped")
    with patch(
        "crucible.diagnostics.loadcheck.autofix_loading",
        side_effect=lambda path, *, apply: (
            observed.update(path=path, apply=apply) or applied
        ),
    ):
        _app, results = _run_worker(_LoadingRepairWorker(instance, "apply", tmux))

    assert results == [("server-c", "apply", applied, "")]
    assert observed == {"path": instance.path, "apply": True}


def test_stale_loading_repair_result_cannot_act_on_replaced_instance():
    messages = []
    old_instance = SimpleNamespace(id="same-id", path="/old/server")
    replacement = SimpleNamespace(id="same-id", path="/new/server")
    fake_window = SimpleNamespace(
        _manager=SimpleNamespace(instances=[replacement]),
        _loading_repair_instance=old_instance,
        statusBar=lambda: SimpleNamespace(showMessage=messages.append),
        _present_loading_repair_result=lambda *_args: pytest.fail(
            "stale result must not mutate or report against the replacement server"
        ),
        _present_loading_diagnosis=lambda *_args: pytest.fail(
            "stale diagnosis must not be presented for the replacement server"
        ),
    )

    MainWindow._on_loading_repair_finished(
        fake_window, "same-id", "apply", SimpleNamespace(quarantined=["wrong.jar"]), ""
    )

    assert messages == ["Loading repair cancelled because the server entry changed."]


def test_confirmed_apply_starts_immediately_if_diagnosis_thread_already_stopped():
    started = []
    pending = SimpleNamespace(id="server-d", path="/tmp/server-d")
    current = SimpleNamespace(id="server-d", path="/tmp/server-d")
    fake_window = SimpleNamespace(
        _loading_repair_thread=None,
        _loading_repair_pending_apply=pending,
        _manager=SimpleNamespace(instances=[current]),
        _start_loading_repair=lambda instance, phase: started.append((instance, phase)),
    )

    MainWindow._start_pending_loading_repair_if_idle(fake_window)

    assert started == [(current, "apply")]
    assert fake_window._loading_repair_pending_apply is None


def test_confirmed_apply_waits_for_diagnosis_thread_teardown():
    started = []
    pending = SimpleNamespace(id="server-e", path="/tmp/server-e")
    fake_window = SimpleNamespace(
        _loading_repair_thread=object(),
        _loading_repair_pending_apply=pending,
        _manager=SimpleNamespace(instances=[pending]),
        _start_loading_repair=lambda instance, phase: started.append((instance, phase)),
    )

    MainWindow._start_pending_loading_repair_if_idle(fake_window)

    assert started == []
    assert fake_window._loading_repair_pending_apply is pending


def test_confirmed_apply_is_discarded_if_server_entry_was_replaced():
    started = []
    pending = SimpleNamespace(id="server-f", path="/old/server")
    replacement = SimpleNamespace(id="server-f", path="/new/server")
    fake_window = SimpleNamespace(
        _loading_repair_thread=None,
        _loading_repair_pending_apply=pending,
        _manager=SimpleNamespace(instances=[replacement]),
        _start_loading_repair=lambda instance, phase: started.append((instance, phase)),
    )

    MainWindow._start_pending_loading_repair_if_idle(fake_window)

    assert started == []
    assert fake_window._loading_repair_pending_apply is None
