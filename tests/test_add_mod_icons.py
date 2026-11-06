"""Deterministic regressions for Add Mod icon paging and network boundaries."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from urllib.request import Request

import pytest
from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from crucible.mods import icon_fetch
from crucible.ui.add_mod_dialog import AddModDialog, _IconWorker


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


class _Response:
    def __init__(self, data=b"icon", content_length=None):
        self._data = data
        self._offset = 0
        self.read_size = None
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        self.read_size = size
        chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


class _Opener:
    def __init__(self, response):
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


def test_icon_worker_accepts_later_page_while_top_icon_is_in_flight(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    fetched = []
    emitted = []
    failed = []

    def fake_fetch(url, *, provider):
        fetched.append((url, provider))
        if url.endswith("/top"):
            entered.set()
            assert release.wait(3), "test did not release the controlled first request"
        return url.encode("utf-8")

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    worker = _IconWorker([("top-project", "https://cdn.modrinth.com/top")])
    worker.icon.connect(
        lambda pid, data: emitted.append((pid, data)),
        Qt.ConnectionType.DirectConnection,
    )
    worker.failed.connect(
        failed.append, Qt.ConnectionType.DirectConnection
    )
    worker.done.connect(done.set, Qt.ConnectionType.DirectConnection)

    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert entered.wait(3), "top-page icon fetch did not start"

    assert worker.add_jobs([
        ("later-project", "https://cdn.modrinth.com/later"),
    ])
    release.set()
    assert done.wait(4), "icon queue did not drain"
    thread.join(1)

    assert not thread.is_alive()
    assert failed == []
    assert [pid for pid, _data in emitted] == ["top-project", "later-project"]
    assert [provider for _url, provider in fetched] == ["modrinth", "modrinth"]


def test_curseforge_icon_fetch_is_disabled_before_network(monkeypatch):
    def unexpected_network(*_args, **_kwargs):
        pytest.fail("disabled CurseForge icon request reached the network opener")

    monkeypatch.setattr(icon_fetch.urllib.request, "build_opener", unexpected_network)
    assert icon_fetch.icon_provider_enabled("modrinth")
    assert not icon_fetch.icon_provider_enabled("curseforge")
    with pytest.raises(icon_fetch.IconFetchError, match="disabled pending provider authorization"):
        icon_fetch.fetch_icon_bytes(
            "https://media.forgecdn.net/file/icon.png", provider="curseforge"
        )


def test_icon_worker_discards_obsolete_queued_jobs_but_accepts_new_search(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    fetched = []
    emitted = []

    def fake_fetch(url, *, provider):
        fetched.append(url)
        if url.endswith("/current"):
            entered.set()
            assert release.wait(3)
        return url.encode("utf-8")

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    worker = _IconWorker([
        ("current", "https://cdn.modrinth.com/current"),
        ("obsolete", "https://cdn.modrinth.com/obsolete"),
    ])
    worker.icon.connect(
        lambda pid, data: emitted.append(pid),
        Qt.ConnectionType.DirectConnection,
    )
    worker.done.connect(done.set, Qt.ConnectionType.DirectConnection)

    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert entered.wait(3)
    assert worker.discard_queued() == ["obsolete"]
    assert worker.add_jobs([("new-result", "https://cdn.modrinth.com/new")])
    release.set()
    assert done.wait(4)
    thread.join(1)

    assert emitted == ["current", "new-result"]
    assert "https://cdn.modrinth.com/obsolete" not in fetched


def test_icon_worker_cancel_discards_queued_work_and_suppresses_in_flight_result(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    emitted = []
    fetched = []

    def fake_fetch(url, *, provider):
        fetched.append(url)
        entered.set()
        assert release.wait(3)
        return b"icon"

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    worker = _IconWorker([
        ("first", "https://cdn.modrinth.com/first"),
        ("second", "https://cdn.modrinth.com/second"),
    ])
    worker.icon.connect(
        lambda pid, data: emitted.append(pid),
        Qt.ConnectionType.DirectConnection,
    )
    worker.done.connect(done.set, Qt.ConnectionType.DirectConnection)

    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert entered.wait(3)
    worker.cancel()
    release.set()
    assert done.wait(4)
    thread.join(1)

    assert emitted == []
    assert fetched == ["https://cdn.modrinth.com/first"]


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("http://cdn.modrinth.com/icon.png", "modrinth"),
        ("https://user:secret@cdn.modrinth.com/icon.png", "modrinth"),
        ("https://evil.example/icon.png", "modrinth"),
        ("https://cdn.modrinth.com:8443/icon.png", "modrinth"),
        ("https://media.forgecdn.net/icon.png", "modrinth"),
        ("https://127.0.0.1/icon.png", "curseforge"),
    ],
)
def test_icon_fetch_rejects_unsafe_or_unapproved_urls_before_network(url, provider, monkeypatch):
    def unexpected_network(*_args, **_kwargs):
        pytest.fail("unsafe icon URL reached the network opener")

    monkeypatch.setattr(icon_fetch.urllib.request, "build_opener", unexpected_network)
    with pytest.raises(icon_fetch.IconFetchError):
        icon_fetch.fetch_icon_bytes(url, provider=provider)


def test_redirect_rejects_unapproved_host_and_drops_all_request_credentials():
    handler = icon_fetch._SafeIconRedirectHandler(frozenset({"cdn.modrinth.com"}))
    req = Request(
        "https://cdn.modrinth.com/start.png",
        headers={"X-Api-Key": "should-never-forward", "Authorization": "should-never-forward"},
    )

    with pytest.raises(icon_fetch.IconFetchError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/redirect")

    redirected = handler.redirect_request(
        req, None, 302, "Found", {}, "https://cdn.modrinth.com/next.png"
    )
    assert redirected.full_url == "https://cdn.modrinth.com/next.png"
    assert "X-api-key" not in redirected.headers
    assert "Authorization" not in redirected.headers
    assert redirected.get_header("User-agent")


def test_icon_fetch_uses_timeout_byte_limit_and_no_api_key(monkeypatch):
    response = _Response(data=b"abc", content_length=3)
    opener = _Opener(response)
    monkeypatch.setattr(
        icon_fetch.urllib.request, "build_opener", lambda *_args: opener
    )

    result = icon_fetch.fetch_icon_bytes("https://cdn.modrinth.com/icon.png", provider="modrinth")

    assert result == b"abc"
    assert opener.timeout == icon_fetch._DEFAULT_TIMEOUT
    assert response.read_size == icon_fetch._READ_CHUNK_BYTES
    assert opener.request.get_header("User-agent")
    assert opener.request.get_header("X-api-key") is None
    assert opener.request.get_header("Authorization") is None


def test_icon_fetch_rejects_oversized_content_length_before_read(monkeypatch):
    monkeypatch.setattr(icon_fetch, "_MAX_ICON_BYTES", 4)
    response = _Response(data=b"12345", content_length=5)
    opener = _Opener(response)
    monkeypatch.setattr(
        icon_fetch.urllib.request, "build_opener", lambda *_args: opener
    )

    with pytest.raises(icon_fetch.IconFetchError, match="too large"):
        icon_fetch.fetch_icon_bytes("https://cdn.modrinth.com/icon.png", provider="modrinth")

    assert response.read_size is None


def test_icon_fetch_rejects_oversized_stream_without_content_length(monkeypatch):
    monkeypatch.setattr(icon_fetch, "_MAX_ICON_BYTES", 4)
    response = _Response(data=b"12345")
    opener = _Opener(response)
    monkeypatch.setattr(
        icon_fetch.urllib.request, "build_opener", lambda *_args: opener
    )

    with pytest.raises(icon_fetch.IconFetchError, match="too large"):
        icon_fetch.fetch_icon_bytes("https://cdn.modrinth.com/icon.png", provider="modrinth")

    assert response.read_size == 5


def _png_bytes(width=8, height=8):
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#c8903a"))
    buffer = QBuffer()
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def test_add_mod_detail_icon_states_replace_permanent_ellipsis(qapp):
    dialog = AddModDialog(SimpleNamespace(loader="fabric", minecraft_version="1.21.1"))
    dialog._initial_search_timer.stop()
    dialog._sel_pid = "state-test"

    dialog._icon_states["state-test"] = "loading"
    dialog._set_detail_icon_state("state-test")
    assert dialog._d_icon_status.text() == "Loading icon…"

    dialog._icon_states["state-test"] = "missing"
    dialog._set_detail_icon_state("state-test")
    assert dialog._d_icon_status.text() == "No icon provided"

    dialog._icon_states["state-test"] = "failed"
    dialog._set_detail_icon_state("state-test")
    assert dialog._d_icon_status.text() == "Icon unavailable"
    assert not dialog._d_icon.pixmap().isNull()

    dialog._on_icon("state-test", b"not an image")
    assert dialog._icon_states["state-test"] == "failed"
    assert dialog._d_icon_status.text() == "Icon unavailable"

    valid = dialog._decode_icon_pixmap(_png_bytes())
    assert valid is not None and not valid.isNull()
    assert valid.width() <= 64 and valid.height() <= 64
    assert dialog._decode_icon_pixmap(_png_bytes(4097, 1)) is None
    assert dialog._decode_icon_pixmap(b"garbage") is None

    dialog.reject()
    dialog.deleteLater()
    qapp.processEvents()


def test_icon_queue_has_hard_cap_and_promotes_selected_visible_jobs(monkeypatch):
    import threading
    from crucible.ui import add_mod_dialog

    monkeypatch.setattr(add_mod_dialog, "_MAX_QUEUED_ICON_JOBS", 3)
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    fetched = []

    def fake_fetch(url, *, provider):
        fetched.append(url.rsplit("/", 1)[-1])
        if url.endswith("/in-flight"):
            entered.set()
            assert release.wait(3), "test did not release the in-flight icon"
        return b"icon"

    monkeypatch.setattr(icon_fetch, "fetch_icon_bytes", fake_fetch)
    worker = _IconWorker([("in-flight", "https://cdn.modrinth.com/in-flight")])
    worker.done.connect(done.set, Qt.ConnectionType.DirectConnection)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert entered.wait(3), "the first icon did not enter its controlled delay"

    assert worker.add_jobs([
        (f"queued-{i}", f"https://cdn.modrinth.com/queued-{i}")
        for i in range(1, 6)
    ])
    assert len(worker._jobs) == 3
    assert worker.take_rejected() == ["queued-4", "queued-5"]

    # Mimic a scroll/selection event: promote a queued visible row and place
    # the selected mod ahead of other off-screen entries without exceeding cap.
    assert worker.add_jobs([
        ("queued-3", "https://cdn.modrinth.com/queued-3"),
        ("selected", "https://cdn.modrinth.com/selected"),
    ], priority=True)
    assert [pid for pid, _url in worker._jobs] == ["queued-3", "selected", "queued-1"]
    assert len(worker._jobs) == 3
    assert worker.take_rejected() == ["queued-2"]

    release.set()
    assert done.wait(4), "bounded priority queue did not drain"
    thread.join(1)
    assert not thread.is_alive()
    assert fetched == ["in-flight", "queued-3", "selected", "queued-1"]


def test_icon_fetch_enforces_absolute_deadline_during_slow_drip(monkeypatch):
    import time

    class SlowResponse(_Response):
        def read1(self, _size):
            time.sleep(0.03)
            return b"x"

    response = SlowResponse()
    opener = _Opener(response)
    monkeypatch.setattr(
        icon_fetch.urllib.request, "build_opener", lambda *_args: opener
    )

    with pytest.raises(icon_fetch.IconFetchError, match="timed out"):
        icon_fetch.fetch_icon_bytes(
            "https://cdn.modrinth.com/icon.png", provider="modrinth", timeout=0.01
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_icon_fetch_rejects_unbounded_or_nonpositive_timeouts(timeout):
    with pytest.raises(icon_fetch.IconFetchError, match="timeout"):
        icon_fetch.fetch_icon_bytes(
            "https://cdn.modrinth.com/icon.png", provider="modrinth", timeout=timeout
        )
