"""Real-Qt integration regression for Add Mod icon pagination."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from crucible.mods import icon_fetch
from crucible.ui.add_mod_dialog import AddModDialog


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def _png_bytes():
    image = QImage(24, 24, QImage.Format.Format_ARGB32)
    image.fill(QColor("#c8903a"))
    buffer = QBuffer()
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def test_pagination_keeps_top_icon_worker_and_binds_icons_to_project_rows(monkeypatch, qapp):
    entered = threading.Event()
    release = threading.Event()
    image_data = _png_bytes()

    class Hit:
        def __init__(self, project_id, title, icon_url):
            self.project_id = project_id
            self.title = title
            self.icon_url = icon_url
            self.downloads = 1000
            self.description = "Synthetic icon-preview fixture."
            self.author = "Preview fixture"
            self.follows = 0
            self.categories = []
            self.client_side = "optional"
            self.server_side = "required"
            self.is_client_only = False

        def server_label(self):
            return "Client & server"

    def fake_fetch(url, *, provider):
        assert provider == "modrinth"
        if url.endswith("/top"):
            entered.set()
            assert release.wait(3), "test did not release the first-page icon request"
        return image_data

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    dialog = AddModDialog(SimpleNamespace(loader="fabric", minecraft_version="1.21.1"))
    dialog._initial_search_timer.stop()
    dialog._has_more = False
    dialog.resize(900, 600)
    dialog.show()
    qapp.processEvents()

    top = Hit("top-project", "Top result", "https://cdn.modrinth.com/top")
    later = Hit("later-project", "Later page result", "https://cdn.modrinth.com/later")
    try:
        dialog._all_hits = [top]
        dialog._rebuild_view()
        assert entered.wait(3), "the first icon fetch did not enter its controlled delay"
        original_worker = dialog._icon_worker

        # A second page arrives while the top-of-list image is still in flight.
        dialog._all_hits.append(later)
        dialog._rebuild_view()
        assert dialog._icon_worker is original_worker, "pagination started a second icon worker"

        release.set()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            qapp.processEvents()
            if (
                {"top-project", "later-project"} <= set(dialog._pixmaps)
                and dialog._icon_worker is None
                and not dialog._workers_running()
            ):
                break
            QTest.qWait(10)

        assert {"top-project", "later-project"} <= set(dialog._pixmaps)
        assert dialog._icon_states["top-project"] == "loaded"
        assert dialog._icon_states["later-project"] == "loaded"
        assert dialog._results.count() == 2
        assert not dialog._results.item(0).icon().isNull()
        assert not dialog._results.item(1).icon().isNull()

        dialog._resolved["later-project"] = []
        dialog._results.setCurrentRow(1)
        qapp.processEvents()
        assert dialog._d_title.text() == "Later page result"
        assert dialog._d_icon_status.text() == ""
        assert not dialog._d_icon.pixmap().isNull()
    finally:
        release.set()
        deadline = time.monotonic() + 4
        while dialog._workers_running() and time.monotonic() < deadline:
            QTest.qWait(10)
        dialog.reject()
        dialog.deleteLater()
        qapp.processEvents()


def test_selecting_evicted_icon_reloads_detail_without_unbounded_cache(monkeypatch, qapp):
    image_data = _png_bytes()
    fetched = []

    class Hit:
        project_id = "evicted-project"
        title = "Evicted Icon"
        icon_url = "https://cdn.modrinth.com/evicted.png"
        downloads = 100
        description = "Synthetic cache regression."
        author = "Fixture"
        follows = 0
        categories = []
        client_side = "optional"
        server_side = "required"
        is_client_only = False

        @staticmethod
        def server_label():
            return "Client & server"

    def fake_fetch(url, *, provider):
        fetched.append((url, provider))
        return image_data

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    dialog = AddModDialog(SimpleNamespace(loader="fabric", minecraft_version="1.21.1"))
    dialog._initial_search_timer.stop()
    dialog._resolved["evicted-project"] = []
    hit = Hit()
    dialog._all_hits = [hit]
    dialog._view = [hit]
    # Force this icon out of the LRU while keeping its result row's identity.
    from PyQt6.QtGui import QPixmap
    dialog._pixmaps["evicted-project"] = QPixmap(2, 2)
    for i in range(256):
        dialog._pixmaps[f"other-{i}"] = QPixmap(2, 2)
    while len(dialog._pixmaps) > 256:
        evicted, _pixmap = dialog._pixmaps.popitem(last=False)
        dialog._icon_states[evicted] = "idle"
    assert "evicted-project" not in dialog._pixmaps
    assert dialog._icon_states["evicted-project"] == "idle"

    try:
        dialog._on_sel(0)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            qapp.processEvents()
            if (
                "evicted-project" in dialog._pixmaps
                and dialog._icon_worker is None
                and not dialog._workers_running()
            ):
                break
            QTest.qWait(10)
        assert fetched == [("https://cdn.modrinth.com/evicted.png", "modrinth")]
        assert "evicted-project" in dialog._pixmaps
        assert dialog._icon_states["evicted-project"] == "loaded"
        assert dialog._d_icon_status.text() == ""
        assert not dialog._d_icon.pixmap().isNull()
        assert len(dialog._pixmaps) <= 256
    finally:
        deadline = time.monotonic() + 4
        while dialog._workers_running() and time.monotonic() < deadline:
            QTest.qWait(10)
        dialog.reject()
        dialog.deleteLater()
        qapp.processEvents()


def test_closing_during_icon_fetch_cancels_result_and_waits_for_thread(monkeypatch, qapp):
    entered = threading.Event()
    release = threading.Event()
    applied = []
    image_data = _png_bytes()

    class Hit:
        project_id = "cancelled-project"
        title = "Cancel During Fetch"
        icon_url = "https://cdn.modrinth.com/cancel.png"
        downloads = 1
        description = "Cancellation fixture."
        author = "Fixture"
        follows = 0
        categories = []
        client_side = "optional"
        server_side = "required"
        is_client_only = False

        @staticmethod
        def server_label():
            return "Client & server"

    def fake_fetch(_url, *, provider):
        assert provider == "modrinth"
        entered.set()
        assert release.wait(3), "test did not release the controlled icon request"
        return image_data

    original_on_icon = AddModDialog._on_icon

    def counted_on_icon(self, pid, data):
        applied.append(pid)
        return original_on_icon(self, pid, data)

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    monkeypatch.setattr(AddModDialog, "_on_icon", counted_on_icon)
    dialog = AddModDialog(SimpleNamespace(loader="fabric", minecraft_version="1.21.1"))
    dialog._initial_search_timer.stop()
    dialog.show()
    dialog._all_hits = [Hit()]
    try:
        dialog._rebuild_view()
        assert entered.wait(3), "icon request did not start"
        worker = dialog._icon_worker
        dialog.reject()
        assert dialog._close_requested
        assert worker is not None
        assert worker._cancelled
        assert not dialog.isHidden(), "dialog closed before worker retirement"
        release.set()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            qapp.processEvents()
            if dialog.isHidden() and not dialog._workers_running():
                break
            QTest.qWait(10)

        assert dialog.isHidden(), "dialog did not close once the cancelled worker exited"
        assert not dialog._workers_running()
        assert dialog._threads == []
        assert dialog._icon_worker is None
        assert applied == [], "cancelled worker delivered an icon callback after close was requested"
        assert "cancelled-project" not in dialog._pixmaps
        qapp.processEvents()
        assert applied == []
    finally:
        release.set()
        deadline = time.monotonic() + 4
        while dialog._workers_running() and time.monotonic() < deadline:
            QTest.qWait(10)
        if not dialog.isHidden():
            dialog.reject()
        dialog.deleteLater()
        qapp.processEvents()


def test_pixmap_eviction_releases_row_icon_and_icon_state_lru_is_bounded(monkeypatch, qapp):
    from PyQt6.QtGui import QIcon, QPixmap
    from PyQt6.QtWidgets import QListWidgetItem

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", lambda *_args, **_kwargs: _png_bytes())
    dialog = AddModDialog(SimpleNamespace(loader="fabric", minecraft_version="1.21.1"))
    dialog._initial_search_timer.stop()
    try:
        first_item = None
        first_key = None
        for i in range(256):
            pid = f"cached-project-{i}"
            pixmap = QPixmap(4, 4)
            pixmap.fill(QColor("#c8903a"))
            dialog._pixmaps[pid] = pixmap
            dialog._set_icon_state(pid, "loaded")
            item = QListWidgetItem(pid)
            item.setIcon(QIcon(pixmap))
            dialog._item_by_pid[pid] = item
            if i == 0:
                first_item = item
                first_key = item.icon().cacheKey()

        dialog._on_icon("new-project", _png_bytes())
        assert len(dialog._pixmaps) == 256
        assert "cached-project-0" not in dialog._pixmaps
        assert dialog._icon_states["cached-project-0"] == "idle"
        assert first_item.icon().cacheKey() != first_key, "evicted row kept its old image QIcon"

        for i in range(1200):
            dialog._set_icon_state(f"state-only-{i}", "failed")
        assert len(dialog._icon_states) == 1024
        assert "state-only-1199" in dialog._icon_states
        assert "state-only-0" not in dialog._icon_states
    finally:
        dialog.reject()
        dialog.deleteLater()
        qapp.processEvents()


def test_scrolling_prioritizes_new_viewport_without_queueing_entire_catalog(monkeypatch, qapp):
    from PyQt6.QtCore import QPoint
    from crucible.ui import add_mod_dialog

    entered = threading.Event()
    release = threading.Event()
    fetched = []
    image_data = _png_bytes()

    class Hit:
        def __init__(self, i):
            self.project_id = f"scroll-{i}"
            self.title = f"Scroll Mod {i}"
            self.icon_url = f"https://cdn.modrinth.com/scroll-{i}.png"
            self.downloads = i
            self.description = "Viewport priority fixture."
            self.author = "Fixture"
            self.follows = 0
            self.categories = []
            self.client_side = "optional"
            self.server_side = "required"
            self.is_client_only = False

        @staticmethod
        def server_label():
            return "Client & server"

    def fake_fetch(url, *, provider):
        assert provider == "modrinth"
        fetched.append(url.rsplit("/", 1)[-1])
        if url.endswith("/scroll-0.png"):
            entered.set()
            assert release.wait(3), "test did not release the in-flight top icon"
        return image_data

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    monkeypatch.setattr(add_mod_dialog, "_MAX_QUEUED_ICON_JOBS", 48)
    dialog = AddModDialog(SimpleNamespace(loader="fabric", minecraft_version="1.21.1"))
    dialog._initial_search_timer.stop()
    dialog._has_more = False
    dialog.resize(900, 600)
    dialog.show()
    hits = [Hit(i) for i in range(200)]
    try:
        dialog._all_hits = hits
        dialog._rebuild_view()
        dialog._results.setCurrentRow(-1)
        assert entered.wait(3), "first visible icon did not begin loading"
        worker = dialog._icon_worker
        assert worker is not None
        queued = [pid for pid, _url in worker._jobs]
        assert len(queued) <= 48
        assert len(queued) < len(hits) // 2, "scheduler queued most of the off-screen catalog"

        bar = dialog._results.verticalScrollBar()
        bar.setValue(bar.maximum())
        qapp.processEvents()
        top_index = dialog._results.indexAt(QPoint(2, 2))
        assert top_index.isValid(), "scrolled viewport has no visible top row"
        visible_top_pid = dialog._view[top_index.row()].project_id
        queued_after_scroll = [pid for pid, _url in worker._jobs]
        assert queued_after_scroll[0] == visible_top_pid, (
            f"new visible top row {visible_top_pid} was not promoted: {queued_after_scroll[:4]}"
        )
        assert len(worker._jobs) <= 48

        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            qapp.processEvents()
            if dialog._icon_worker is None and not dialog._workers_running():
                break
            QTest.qWait(10)
        assert dialog._icon_worker is None
        assert not dialog._workers_running()
        assert len(dialog._pixmaps) <= 256
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while dialog._workers_running() and time.monotonic() < deadline:
            QTest.qWait(10)
        if not dialog.isHidden():
            dialog.reject()
        dialog.deleteLater()
        qapp.processEvents()
