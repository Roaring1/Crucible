"""Real Qt interaction tests for PlayersTab avatar loading, success, and failure states.

All avatar requests use patched urllib responses and a temporary cache. No real
network, user avatar cache, or server is touched. Captures are made from the
actual PlayersTab widget at standard and small window sizes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

_DRIVER = r"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QSize, Qt
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QAbstractButton
from PyQt6.QtCore import QAbstractAnimation

from crucible.data.instance_model import ServerInstance
from crucible.ui import theme
from crucible.ui.tabs import players_tab as module
from crucible.ui.tabs.players_tab import PlayersTab, _AvatarFetcher

scenario = sys.argv[1]
output = Path(sys.argv[2])
output.mkdir(parents=True, exist_ok=True)

app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)
root = Path(tempfile.mkdtemp(prefix="crucible-player-avatar-state-"))
cache = root / "avatar-cache"
module._AVATAR_CACHE_DIR = cache
server_a = root / "server-a"
server_b = root / "server-b"
server_a.mkdir()
server_b.mkdir()
for path in (server_a, server_b):
    (path / "server.properties").write_text(
        "level-name=world\nserver-port=25565\n", encoding="utf-8"
    )

def make_png() -> bytes:
    pix = QPixmap(QSize(20, 20))
    pix.fill(QColor(theme.GREEN))
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("unable to open PNG buffer")
    if not pix.save(buffer, "PNG"):
        raise RuntimeError("unable to encode synthetic avatar")
    return bytes(buffer.data())

PNG_BYTES = make_png()

class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit: int):
        return self.payload[:limit]

def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return True
        QTest.qWait(10)
    app.processEvents()
    return bool(predicate())

def row_for(tab, name):
    for row in range(tab._online_list.count()):
        item = tab._online_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole) == name:
            return item
    return None

def state_for_render(tab, state):
    tab._online.clear()
    tab._seen.clear()
    tab._avatar_pending.clear()
    tab._avatar_errors.clear()
    tab._avatars.clear()
    if state == "empty":
        pass
    else:
        tab._online.add("Alex")
        if state == "loading":
            tab._avatar_pending.add("Alex")
        elif state == "success":
            pix = QPixmap()
            if not pix.loadFromData(PNG_BYTES):
                raise AssertionError("synthetic PNG did not load on the UI thread")
            tab._avatars["Alex"] = pix
        elif state == "error":
            tab._avatar_errors.add("Alex")
        else:
            raise AssertionError(state)
    tab._refresh_online_list()

tab = PlayersTab()
tab.resize(1040, 560)
tab.show()
app.processEvents()

report = {"scenario": scenario, "cache_root": str(cache)}

if scenario == "render":
    captures = []
    for width, height, label in ((1040, 560, "standard"), (640, 420, "small")):
        tab.resize(width, height)
        app.processEvents()
        for state in ("loading", "success", "error", "empty"):
            state_for_render(tab, state)
            app.processEvents()
            path = output / f"players-avatar-{state}-{label}.png"
            if not tab.grab().save(str(path), "PNG"):
                raise AssertionError(f"failed to capture {path}")
            item = row_for(tab, "Alex") if state != "empty" else None
            captures.append({
                "state": state,
                "window_size": [width, height],
                "screenshot": str(path),
                "row_count": tab._online_list.count(),
                "alex_icon_non_null": bool(item and not item.icon().isNull()),
                "alex_tooltip": item.toolTip() if item else "",
                "empty_label": (
                    tab._online_list.item(0).text()
                    if state == "empty" and tab._online_list.count() else ""
                ),
                "has_animation": bool(tab.findChildren(QAbstractAnimation)),
            })
    report["captures"] = captures
    report["motion_free"] = not bool(tab.findChildren(QAbstractAnimation))

elif scenario == "success":
    tab._online.add("Alex")
    with patch("urllib.request.urlopen", return_value=FakeResponse(PNG_BYTES)) as request:
        tab._fetch_avatar("Alex")
        report["loading_visible"] = (
            row_for(tab, "Alex") is not None
            and not row_for(tab, "Alex").icon().isNull()
            and "Loading avatar" in row_for(tab, "Alex").toolTip()
        )
        report["completed"] = wait_until(
            lambda: "Alex" not in tab._avatar_pending and not tab._avatar_threads
        )
        report["network_called"] = request.called
    item = row_for(tab, "Alex")
    report["avatar_loaded"] = (
        "Alex" in tab._avatars and not tab._avatars["Alex"].isNull()
    )
    report["error_state_cleared"] = "Alex" not in tab._avatar_errors
    report["cache_file_exists"] = (cache / "Alex.png").is_file()
    report["item_tooltip"] = item.toolTip() if item else ""
    tab.grab().save(str(output / "avatar-success.png"), "PNG")

elif scenario == "failure":
    tab._online.add("Alex")
    with patch("urllib.request.urlopen", side_effect=OSError("offline")) as request:
        tab._fetch_avatar("Alex")
        report["loading_visible"] = (
            row_for(tab, "Alex") is not None
            and not row_for(tab, "Alex").icon().isNull()
            and "Loading avatar" in row_for(tab, "Alex").toolTip()
        )
        report["completed"] = wait_until(
            lambda: "Alex" not in tab._avatar_pending and not tab._avatar_threads
        )
        report["network_called"] = request.called
    item = row_for(tab, "Alex")
    report["error_state"] = "Alex" in tab._avatar_errors
    report["avatar_not_cached"] = "Alex" not in tab._avatars
    report["fallback_icon_non_null"] = bool(item and not item.icon().isNull())
    report["fallback_tooltip"] = item.toolTip() if item else ""
    report["threads_retired"] = len(tab._avatar_threads) == 0
    tab.grab().save(str(output / "avatar-error.png"), "PNG")

elif scenario == "stale-cache":
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / "Alex.png"
    cached.write_bytes(PNG_BYTES)
    stale_time = time.time() - module._AVATAR_MAX_AGE_S - 60
    os.utime(cached, (stale_time, stale_time))
    tab._online.add("Alex")
    with patch("urllib.request.urlopen", side_effect=OSError("offline")) as request:
        tab._fetch_avatar("Alex")
        report["completed"] = wait_until(
            lambda: "Alex" not in tab._avatar_pending and not tab._avatar_threads
        )
        report["network_called"] = request.called
    report["stale_cache_kept_usable"] = (
        "Alex" in tab._avatars and not tab._avatars["Alex"].isNull()
    )
    report["error_state"] = "Alex" in tab._avatar_errors
    report["fallback_tooltip"] = (
        row_for(tab, "Alex").toolTip() if row_for(tab, "Alex") else ""
    )
    tab.grab().save(str(output / "avatar-stale-cache-fallback.png"), "PNG")

elif scenario == "close-running":
    entered = threading.Event()
    release = threading.Event()

    class SlowResponse(FakeResponse):
        def read(self, limit: int):
            entered.set()
            release.wait(2.0)
            return self.payload[:limit]

    tab._online.add("Alex")
    with patch("urllib.request.urlopen", return_value=SlowResponse(PNG_BYTES)):
        tab._fetch_avatar("Alex")
        report["request_entered"] = wait_until(entered.is_set, timeout=1.5)
        generation_before_close = tab._avatar_generation
        tab.close()
        report["close_invalidated_generation"] = (
            tab._avatar_generation > generation_before_close
        )
        tab.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        report["worker_retained_after_widget_destroyed"] = bool(module._ACTIVE_AVATAR_WORKERS)
        release.set()
        report["worker_released_after_request_finished"] = wait_until(
            lambda: not module._ACTIVE_AVATAR_WORKERS, timeout=3.0
        )
        report["worker_registry_empty"] = not module._ACTIVE_AVATAR_WORKERS

elif scenario == "stale":
    tab.load(ServerInstance(str(server_a), "Server A"))
    old_generation = tab._avatar_generation
    tab._online.add("Alex")
    tab._avatar_pending.add("Alex")
    tab._refresh_online_list()

    tab.load(ServerInstance(str(server_b), "Server B"))
    new_generation = tab._avatar_generation
    tab._online.add("Alex")
    tab._avatar_pending.add("Alex")  # Represents a new-server request with its new generation.
    tab._refresh_online_list()

    tab._on_avatar_completed("Alex", PNG_BYTES, old_generation)
    report["generation_advanced"] = new_generation > old_generation
    report["stale_avatar_ignored_after_server_switch"] = "Alex" not in tab._avatars
    report["new_server_keeps_player_loading_state"] = "Alex" in tab._avatar_pending
    report["empty_state_not_substituted"] = (
        tab._online_list.count() > 0 and "No players online" not in tab._online_list.item(0).text()
    )

    close_generation = tab._avatar_generation
    tab.close()
    app.processEvents()
    tab._on_avatar_completed("Alex", PNG_BYTES, close_generation)
    report["close_invalidated_requests"] = tab._avatar_generation > close_generation
    report["late_result_after_close_ignored"] = "Alex" not in tab._avatars
    report["no_animation_objects"] = not bool(tab.findChildren(QAbstractAnimation))

else:
    raise AssertionError("unknown scenario: " + scenario)

print(json.dumps(report))
if scenario != "close-running":
    tab.close()
    app.processEvents()
shutil.rmtree(root, ignore_errors=True)
"""


def _run(tmp_path: Path, scenario: str):
    output = tmp_path / scenario
    output.mkdir()
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER, scenario, str(output)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_avatar_fetch_success_replaces_static_loading_state_and_caches(tmp_path):
    result = _run(tmp_path, "success")
    assert result["loading_visible"]
    assert result["completed"]
    assert result["network_called"]
    assert result["avatar_loaded"]
    assert result["error_state_cleared"]
    assert result["cache_file_exists"]


def test_avatar_fetch_failure_always_retires_thread_and_shows_fallback(tmp_path):
    result = _run(tmp_path, "failure")
    assert result["loading_visible"]
    assert result["completed"], "worker never completed after network failure"
    assert result["network_called"]
    assert result["error_state"]
    assert result["avatar_not_cached"]
    assert result["fallback_icon_non_null"]
    assert "Avatar unavailable" in result["fallback_tooltip"]
    assert "actions remain available" in result["fallback_tooltip"]
    assert result["threads_retired"]


def test_offline_refresh_uses_valid_stale_cache_and_retires_worker(tmp_path):
    result = _run(tmp_path, "stale-cache")
    assert result["completed"]
    assert result["network_called"]
    assert result["stale_cache_kept_usable"]
    assert not result["error_state"]


def test_closing_tab_during_live_request_keeps_thread_alive_then_releases_it(tmp_path):
    result = _run(tmp_path, "close-running")
    assert result["request_entered"]
    assert result["close_invalidated_generation"]
    assert result["worker_retained_after_widget_destroyed"]
    assert result["worker_released_after_request_finished"]
    assert result["worker_registry_empty"]


def test_stale_avatar_completion_is_ignored_on_server_switch_and_close(tmp_path):
    result = _run(tmp_path, "stale")
    assert result["generation_advanced"]
    assert result["stale_avatar_ignored_after_server_switch"]
    assert result["new_server_keeps_player_loading_state"]
    assert result["empty_state_not_substituted"]
    assert result["close_invalidated_requests"]
    assert result["late_result_after_close_ignored"]
    assert result["no_animation_objects"]


def test_loading_success_error_and_empty_states_have_standard_and_small_captures(tmp_path):
    result = _run(tmp_path, "render")
    assert result["motion_free"], "avatar placeholders must remain static in this first pass"
    captures = result["captures"]
    assert len(captures) == 8
    for entry in captures:
        image = Path(entry["screenshot"])
        assert image.is_file()
        assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert len(image.read_bytes()) > 1_000
        assert not entry["has_animation"]
        if entry["state"] in {"loading", "success", "error"}:
            assert entry["alex_icon_non_null"], entry
        if entry["state"] == "loading":
            assert "Loading avatar" in entry["alex_tooltip"]
        elif entry["state"] == "error":
            assert "Avatar unavailable" in entry["alex_tooltip"]
        elif entry["state"] == "empty":
            assert "No players online" in entry["empty_label"]
