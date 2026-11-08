"""
crucible/ui/tabs/players_tab.py

Player management tab.

Top:    Online Now  — live from LogWatcher signals, with 20×20 player-head
        avatars (cached to disk at ~/.local/share/crucible/avatars/).
Bottom: Sub-tabs    — Whitelist | Ops | Banned

Avatar cache: fetched from minotar.net on first join, stored as PNG.
Re-fetched if older than 7 days. Loading and failure states use static local
20×20 placeholders; network failures use a valid stale cache when available,
or a deterministic fallback icon and tooltip when no cache is available.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QSize, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...data.instance_model import ServerInstance
from ...process.log_watcher import LogWatcher
from ...process.tmux_manager import TmuxManager
from .. import theme
from ..thread_lifecycle import connect_thread_cleanup

_AVATAR_CACHE_DIR = Path.home() / ".local" / "share" / "crucible" / "avatars"
_AVATAR_MAX_AGE_S = 7 * 24 * 3600
_MAX_AVATAR_BYTES = 2 * 1024 * 1024
_MAX_PLAYER_JSON_BYTES = 16 * 1024 * 1024


def _read_json_file(path: Path):
    if path.stat().st_size > _MAX_PLAYER_JSON_BYTES:
        raise OSError(f"JSON file exceeds 16 MiB safety limit: {path}")
    with path.open("rb") as fh:
        raw = fh.read(_MAX_PLAYER_JSON_BYTES + 1)
    if len(raw) > _MAX_PLAYER_JSON_BYTES:
        raise OSError(f"JSON file exceeds 16 MiB safety limit: {path}")
    return json.loads(raw.decode("utf-8"))


class _PlayerDataLoadWorker(QObject):
    """Read the four player JSON files without blocking the Qt event loop."""

    completed = pyqtSignal(int, object)

    def __init__(
        self,
        generation: int,
        seen_path: Path,
        list_paths: dict[str, Path],
    ):
        super().__init__()
        self._generation = generation
        self._seen_path = seen_path
        self._list_paths = dict(list_paths)

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            path.stat()
        except FileNotFoundError:
            return {"exists": False, "data": None, "error": ""}
        except OSError as exc:
            return {"exists": True, "data": None, "error": str(exc)}
        try:
            return {"exists": True, "data": _read_json_file(path), "error": ""}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {"exists": True, "data": None, "error": str(exc)}

    def run(self) -> None:
        try:
            result = {
                "seen": self._read(self._seen_path),
                "lists": {
                    filename: self._read(path)
                    for filename, path in self._list_paths.items()
                },
            }
        except Exception as exc:  # noqa: BLE001 - completion must always retire the QThread
            result = {
                "seen": {"exists": False, "data": None, "error": str(exc)},
                "lists": {
                    filename: {"exists": False, "data": None, "error": str(exc)}
                    for filename in self._list_paths
                },
            }
        finally:
            self.completed.emit(self._generation, result)


# Keep read workers alive through native QThread shutdown if the tab closes.
_ACTIVE_PLAYER_DATA_WORKERS: dict[QThread, _PlayerDataLoadWorker] = {}


# Avatar fetcher

class _AvatarFetcher(QObject):
    """Fetch/validate one avatar off-thread and always report completion."""

    completed = pyqtSignal(str, object, int)  # player name, PNG bytes or None, generation

    def __init__(self, name: str, generation: int, parent=None):
        super().__init__(parent)
        self._name = name
        self._generation = generation

    def _cache_path(self) -> Path:
        _AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return _AVATAR_CACHE_DIR / f"{self._name}.png"

    @staticmethod
    def _valid_png(data: bytes | None) -> bool:
        if not data or len(data) > _MAX_AVATAR_BYTES:
            return False
        return not QImage.fromData(data).isNull()

    def run(self) -> None:
        """Always emit completed, including for invalid cache and network failures."""
        cache: Path | None = None
        data: bytes | None = None
        try:
            cache = self._cache_path()
            if cache.exists():
                age = time.time() - cache.stat().st_mtime
                if age < _AVATAR_MAX_AGE_S:
                    candidate = cache.read_bytes()
                    if self._valid_png(candidate):
                        data = candidate

            if data is None:
                import urllib.request

                url = "https://minotar.net/avatar/%s/20" % self._name
                with urllib.request.urlopen(url, timeout=8) as response:
                    candidate = response.read(_MAX_AVATAR_BYTES + 1)
                if not self._valid_png(candidate):
                    raise ValueError("avatar response was empty, invalid, or exceeded 2 MiB")

                partial = cache.with_suffix(cache.suffix + ".partial")
                try:
                    partial.write_bytes(candidate)
                    partial.replace(cache)
                finally:
                    partial.unlink(missing_ok=True)
                data = candidate
        except Exception:
            # If the network or cache path fails, try one last time to read a
            # valid stale cache. A missing/invalid stale cache is still a result.
            try:
                if cache is None:
                    cache = self._cache_path()
                if cache.exists():
                    candidate = cache.read_bytes()
                    if self._valid_png(candidate):
                        data = candidate
            except Exception:
                data = None
        finally:
            # This signal is the lifecycle boundary: the UI can settle the
            # loading slot and the QThread can always stop on every outcome.
            self.completed.emit(self._name, data, self._generation)


# Keep a worker alive until its native QThread has fully stopped, even if the
# PlayersTab is closed or its parent window is destroyed mid-request.
_ACTIVE_AVATAR_WORKERS: dict[QThread, _AvatarFetcher] = {}


# Teleport dialog

class _TeleportDialog(QDialog):
    """Teleport a player to coordinates (optionally in a chosen dimension) or to
    another online player — covers end/nether/overworld/dimension select."""

    _DIMS = [
        ("(current dimension)", ""),
        ("Overworld", "minecraft:overworld"),
        ("Nether", "minecraft:the_nether"),
        ("End", "minecraft:the_end"),
    ]

    def __init__(self, name: str, online_players: list[str], parent=None):
        super().__init__(parent)
        self._name = name
        self.setWindowTitle(f"Teleport {name}")
        self.setAccessibleName(f"Teleport player {name}")
        self.setModal(True)
        self.setMinimumWidth(360)
        self._build_ui(online_players)

    def _build_ui(self, online_players: list[str]) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 14, 16, 14)

        lay.addWidget(QLabel(f"Where should {self._name} go?"))

        self._coord_radio  = QRadioButton("To coordinates")
        self._coord_radio.setAccessibleName(
            f"Teleport {self._name} to coordinates and a dimension"
        )
        self._coord_radio.setChecked(True)
        self._player_radio = QRadioButton("To another player")
        self._player_radio.setAccessibleName(
            f"Teleport {self._name} to another online player"
        )
        grp = QButtonGroup(self)
        grp.addButton(self._coord_radio)
        grp.addButton(self._player_radio)

        lay.addWidget(self._coord_radio)

        form = QFormLayout()
        self._dim_combo = QComboBox()
        self._dim_combo.setAccessibleName(
            f"Destination dimension for player {self._name}"
        )
        for label, _dimid in self._DIMS:
            self._dim_combo.addItem(label)
        form.addRow("Dimension:", self._dim_combo)

        self._x = QLineEdit("~")
        self._x.setAccessibleName(f"Destination X coordinate for {self._name}")
        self._y = QLineEdit("~")
        self._y.setAccessibleName(f"Destination Y coordinate for {self._name}")
        self._z = QLineEdit("~")
        self._z.setAccessibleName(f"Destination Z coordinate for {self._name}")
        coord_row = QHBoxLayout()
        for axis, field in (("X", self._x), ("Y", self._y), ("Z", self._z)):
            coord_row.addWidget(QLabel(axis))
            field.setFixedWidth(70)
            coord_row.addWidget(field)
        coord_w = QWidget()
        coord_w.setLayout(coord_row)
        form.addRow("Coords:", coord_w)
        lay.addLayout(form)

        lay.addWidget(self._player_radio)
        self._target_combo = QComboBox()
        self._target_combo.setAccessibleName(
            f"Online player destination for {self._name}"
        )
        others = [p for p in online_players if p != self._name]
        self._target_combo.addItems(others)
        self._target_combo.setEnabled(False)
        lay.addWidget(self._target_combo)
        if not others:
            self._player_radio.setEnabled(False)

        self._coord_radio.toggled.connect(self._sync_enabled)
        self._sync_enabled()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _sync_enabled(self) -> None:
        coords = self._coord_radio.isChecked()
        self._dim_combo.setEnabled(coords)
        for field in (self._x, self._y, self._z):
            field.setEnabled(coords)
        self._target_combo.setEnabled(not coords)

    def command(self) -> str | None:
        if self._player_radio.isChecked():
            target = self._target_combo.currentText().strip()
            if not target:
                return None
            return f"tp {self._name} {target}"
        x = self._x.text().strip() or "~"
        y = self._y.text().strip() or "~"
        z = self._z.text().strip() or "~"
        dim = self._DIMS[self._dim_combo.currentIndex()][1]
        if dim:
            return f"execute in {dim} run tp {self._name} {x} {y} {z}"
        return f"tp {self._name} {x} {y} {z}"


# Main tab

class PlayersTab(QWidget):
    """Online players + whitelist/ops/banned management."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
        self._watcher:  LogWatcher | None     = None
        self._online:   set[str]              = set()
        self._seen:     dict[str, dict]       = {}     # name -> {first_seen,last_seen,…}
        self._seen_path: Path | None          = None
        self._join_times: dict[str, float]    = {}     # name -> join epoch (this run)
        self._avatars: dict[str, QPixmap] = {}
        self._avatar_pending: set[str] = set()
        self._avatar_errors: set[str] = set()
        self._avatar_generation = 0
        self._avatar_placeholders: dict[str, QPixmap] = {}
        self._avatar_threads: list[QThread] = []
        self._avatar_fetchers: list[_AvatarFetcher] = []
        self._player_data_generation = 0
        self._player_data_threads: list[QThread] = []
        self._player_data_workers: list[_PlayerDataLoadWorker] = []
        self._seen_loading = False
        self._seen_persistence_blocked = False
        self._seen_updates_during_load: dict[str, dict] = {}
        self._seen_deleted_during_load: set[str] = set()
        self._tmux = TmuxManager()
        self._build_ui()

    # UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        hdr = QLabel("ONLINE NOW")
        hdr.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px; "
            f"font-weight: 600; letter-spacing: 1px;"
        )
        layout.addWidget(hdr)

        self._online_list = QListWidget()
        self._online_list.setAccessibleName("Players currently online on the selected server")
        self._online_list.setAccessibleDescription(
            "Select a player and open the context menu to grant operator access, "
            "kick, ban, teleport or give an item."
        )
        self._online_list.setMinimumHeight(72)
        self._online_list.setIconSize(QSize(20, 20))
        self._online_list.setObjectName("OnlinePlayersList")
        self._online_list.setStyleSheet(
            f"background: {theme.SURFACE0}; border-radius: 4px; "
            f"QListWidget#OnlinePlayersList:focus {{ "
            f"border: 2px solid {theme.ACCENT}; }}"
        )
        # Click a player to act on them (op, kick, ban, teleport, give…).
        self._online_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._online_list.customContextMenuRequested.connect(
            self._show_player_menu)
        self._online_list.itemDoubleClicked.connect(
            lambda item: self._show_player_menu(
                self._online_list.visualItemRect(item).center()))
        self._players_splitter = QSplitter(Qt.Orientation.Vertical)
        self._players_splitter.setChildrenCollapsible(False)
        self._players_splitter.setHandleWidth(9)
        self._players_splitter.setToolTip("Drag the divider to resize Online now")
        self._players_splitter.addWidget(self._online_list)

        hint = QLabel("Right-click or double-click a player for actions. Drag the divider below to resize Online now.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 10px;")
        layout.addWidget(hint)

        self._history_status = QLabel("")
        self._history_status.setObjectName("PlayerHistoryLoadStatus")
        self._history_status.setWordWrap(True)
        self._history_status.setAccessibleName("Player history persistence status")
        self._history_status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 10px;")
        self._history_status.hide()
        layout.addWidget(self._history_status)

        sub = QTabWidget()
        sub.setAccessibleName("Whitelist, operator and banned player lists")
        sub.setDocumentMode(True)
        self._whitelist_w = _PlayerListWidget("whitelist.json",      allow_add=True)
        self._ops_w       = _PlayerListWidget("ops.json",            allow_add=True)
        self._banned_w    = _PlayerListWidget("banned-players.json", allow_add=False)
        sub.addTab(self._whitelist_w, "Whitelist")
        sub.addTab(self._ops_w,       "Ops")
        sub.addTab(self._banned_w,    "Banned")
        self._players_splitter.addWidget(sub)
        self._players_splitter.setSizes([200, 360])
        layout.addWidget(self._players_splitter, stretch=1)

    # Public API

    def load(self, instance: ServerInstance) -> None:
        """Load player history and permission lists without blocking the UI."""
        # Invalidate avatar and JSON results from the previous selected server.
        self._invalidate_avatar_requests()
        self._player_data_generation += 1
        generation = self._player_data_generation
        self._instance = instance
        self._online.clear()
        self._join_times.clear()
        self._seen = {}
        self._seen_path = Path(instance.path) / ".crucible" / "players_seen.json"
        self._seen_loading = True
        self._seen_persistence_blocked = False
        self._history_status.clear()
        self._history_status.hide()
        self._seen_updates_during_load = {}
        self._seen_deleted_during_load = set()
        self._refresh_online_list()

        list_paths = {
            "whitelist.json": Path(instance.path) / "whitelist.json",
            "ops.json": Path(instance.path) / "ops.json",
            "banned-players.json": Path(instance.path) / "banned-players.json",
        }
        self._whitelist_w.begin_async_load(
            instance, "whitelist.json", generation
        )
        self._ops_w.begin_async_load(instance, "ops.json", generation)
        self._banned_w.begin_async_load(
            instance, "banned-players.json", generation
        )

        thread = QThread()
        worker = _PlayerDataLoadWorker(
            generation, self._seen_path, list_paths
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_player_data_loaded)
        worker.completed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)

        def _cleanup():
            _ACTIVE_PLAYER_DATA_WORKERS.pop(thread, None)
            if thread in self._player_data_threads:
                self._player_data_threads.remove(thread)
            if worker in self._player_data_workers:
                self._player_data_workers.remove(worker)

        connect_thread_cleanup(thread, _cleanup)
        _ACTIVE_PLAYER_DATA_WORKERS[thread] = worker
        self._player_data_threads.append(thread)
        self._player_data_workers.append(worker)
        thread.start()

    @pyqtSlot(int, object)
    def _on_player_data_loaded(self, generation: int, result: object) -> None:
        if generation != self._player_data_generation:
            return
        if not isinstance(result, dict):
            self._seen_loading = False
            self._seen_persistence_blocked = True
            self._history_status.setText(
                "Player history could not be read. Live changes are held in memory; "
                "the existing file is protected. Reselect this server to retry."
            )
            self._history_status.show()
            return

        seen_payload = result.get("seen")
        loaded_seen = (
            seen_payload.get("data") if isinstance(seen_payload, dict) else None
        )
        seen_exists = (
            bool(seen_payload.get("exists", False))
            if isinstance(seen_payload, dict) else True
        )
        seen_error = (
            not isinstance(seen_payload, dict)
            or bool(seen_payload.get("error"))
            or (seen_exists and not isinstance(loaded_seen, dict))
            or (
                isinstance(loaded_seen, dict)
                and any(not isinstance(record, dict) for record in loaded_seen.values())
            )
        )

        if seen_error:
            # Do not reinterpret an unreadable, malformed, oversized, or
            # wrong-schema existing file as an empty history. Keep watcher
            # deltas in memory and prevent _save_seen() from replacing it.
            self._seen_loading = False
            self._seen_persistence_blocked = True
            self._history_status.setText(
                "Player history could not be read. Live changes are held in memory; "
                "the existing file is protected. Reselect this server to retry."
            )
            self._history_status.show()
        else:
            seen = (
                {
                    name: record
                    for name, record in loaded_seen.items()
                    if isinstance(name, str)
                }
                if isinstance(loaded_seen, dict) else {}
            )

            # Log-watcher join/leave events may arrive while JSON is being read.
            # Merge those deltas over the disk snapshot instead of losing them.
            pending_history_changes = bool(
                self._seen_updates_during_load or self._seen_deleted_during_load
            )
            for name, update in self._seen_updates_during_load.items():
                record = dict(seen.get(name, {}))
                if record.get("first_seen") is None:
                    record["first_seen"] = update.get("first_seen")
                record["last_seen"] = update.get("last_seen")
                if update.get("joined"):
                    try:
                        sessions = int(record.get("sessions", 0))
                    except (TypeError, ValueError, OverflowError):
                        sessions = 0
                    record["sessions"] = sessions + int(update["joined"])
                if update.get("seconds_played"):
                    try:
                        seconds_played = float(record.get("seconds_played", 0.0))
                    except (TypeError, ValueError, OverflowError):
                        seconds_played = 0.0
                    record["seconds_played"] = (
                        seconds_played + float(update["seconds_played"])
                    )
                seen[name] = record
            for name in self._seen_deleted_during_load:
                seen.pop(name, None)

            self._seen = seen
            self._seen_loading = False
            self._seen_persistence_blocked = False
            self._seen_updates_during_load = {}
            self._seen_deleted_during_load = set()
            self._history_status.clear()
            self._history_status.hide()
            # Do not create/rewrite players_seen.json just because it was read.
            # Persist only live watcher/forget changes that arrived during loading.
            if pending_history_changes:
                self._save_seen()

        list_payloads = result.get("lists", {})
        if isinstance(list_payloads, dict):
            self._whitelist_w.apply_async_data(
                generation, list_payloads.get("whitelist.json", {})
            )
            self._ops_w.apply_async_data(
                generation, list_payloads.get("ops.json", {})
            )
            self._banned_w.apply_async_data(
                generation, list_payloads.get("banned-players.json", {})
            )
        self._refresh_online_list()

    def attach_watcher(self, watcher: LogWatcher) -> None:
        self.detach_watcher()
        self._watcher = watcher
        watcher.player_joined.connect(self._on_joined)
        watcher.player_left.connect(self._on_left)
        watcher.server_stopping.connect(self._on_server_stopped)
        watcher.log_rotated.connect(self._on_server_stopped)

    def detach_watcher(self) -> None:
        if self._watcher:
            try:
                self._watcher.player_joined.disconnect(self._on_joined)
                self._watcher.player_left.disconnect(self._on_left)
                self._watcher.server_stopping.disconnect(self._on_server_stopped)
                self._watcher.log_rotated.disconnect(self._on_server_stopped)
            except (RuntimeError, TypeError):
                pass
            self._watcher = None

    # Slots

    @pyqtSlot(str)
    def _on_joined(self, name: str) -> None:
        self._online.add(name)
        self._join_times[name] = time.time()
        self._record_seen(name, joined=True)
        if name not in self._avatars and name not in self._avatar_pending:
            self._avatar_errors.discard(name)
            self._fetch_avatar(name)
        self._refresh_online_list()

    @pyqtSlot(str)
    def _on_left(self, name: str) -> None:
        self._online.discard(name)
        self._record_seen(name, joined=False)
        self._refresh_online_list()

    @pyqtSlot()
    def _on_server_stopped(self) -> None:
        """Server stopped/restarted — move everyone online into the 'played' state."""
        for name in list(self._online):
            self._record_seen(name, joined=False)
        self._online.clear()
        self._refresh_online_list()

    # Recently-played ("played" state) persistence

    def _load_seen(self) -> None:
        self._seen = {}
        self._seen_loading = False
        self._seen_persistence_blocked = False
        self._seen_updates_during_load = {}
        self._seen_deleted_during_load = set()
        if self._instance is None:
            self._seen_path = None
            return
        self._seen_path = Path(self._instance.path) / ".crucible" / "players_seen.json"
        try:
            try:
                self._seen_path.stat()
            except FileNotFoundError:
                pass
            else:
                data = _read_json_file(self._seen_path)
                if not isinstance(data, dict) or any(
                    not isinstance(record, dict) for record in data.values()
                ):
                    raise ValueError("player history has an invalid JSON schema")
                self._seen = data
            self._history_status.clear()
            self._history_status.hide()
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
            self._seen_persistence_blocked = True
            self._history_status.setText(
                "Player history could not be read. Live changes are held in memory; "
                "the existing file is protected. Reselect this server to retry."
            )
            self._history_status.show()

    def _save_seen(self) -> None:
        if (
            self._seen_path is None
            or self._seen_loading
            or self._seen_persistence_blocked
        ):
            return
        try:
            self._seen_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._seen_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._seen, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self._seen_path)
        except OSError:
            pass

    def _record_seen(self, name: str, *, joined: bool) -> None:
        now = time.time()
        rec = self._seen.get(name) or {}
        rec.setdefault("first_seen", now)
        rec["last_seen"] = now
        if joined:
            rec["sessions"] = int(rec.get("sessions", 0)) + 1
        else:
            jt = self._join_times.pop(name, None)
            if jt is not None:
                rec["seconds_played"] = float(rec.get("seconds_played", 0.0)) + max(0.0, now - jt)
        self._seen[name] = rec
        if self._seen_loading:
            update = self._seen_updates_during_load.setdefault(
                name,
                {
                    "first_seen": rec.get("first_seen"),
                    "last_seen": now,
                    "joined": 0,
                    "seconds_played": 0.0,
                },
            )
            update["last_seen"] = now
            if joined:
                update["joined"] += 1
            elif jt is not None:
                update["seconds_played"] += max(0.0, now - jt)
        else:
            self._save_seen()

    def _avatar_for(self, name: str) -> QPixmap | None:
        pix = self._avatars.get(name)
        if pix is not None and not pix.isNull():
            return pix
        cache = _AVATAR_CACHE_DIR / f"{name}.png"
        if cache.exists():
            try:
                p = QPixmap(str(cache))
                if not p.isNull():
                    self._avatars[name] = p
                    return p
            except Exception:
                pass
        return None

    def _avatar_placeholder(self, state: str) -> QPixmap:
        """Create a small, local static avatar placeholder; no animation/network."""
        if state in self._avatar_placeholders:
            return self._avatar_placeholders[state]
        pixmap = QPixmap(20, 20)
        pixmap.fill(QColor(theme.SURFACE0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if state == "loading":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SURFACE1))
            painter.drawRoundedRect(2, 2, 16, 16, 3, 3)
            painter.setBrush(QColor(theme.SURFACE2))
            painter.drawRoundedRect(5, 5, 10, 3, 1.5, 1.5)
            painter.drawRoundedRect(5, 11, 7, 3, 1.5, 1.5)
        else:
            # The error state is semantically important; use the existing alert
            # token rather than the low-contrast SURFACE2 used for secondary text.
            painter.setPen(QColor(theme.RED))
            painter.setFont(QFont("sans-serif", 10, QFont.Weight.Bold))
            painter.drawEllipse(2, 2, 16, 16)
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "!")
        painter.end()
        self._avatar_placeholders[state] = pixmap
        return pixmap

    def _invalidate_avatar_requests(self) -> None:
        """Ignore late avatar results when changing server or closing the view."""
        self._avatar_generation += 1
        self._avatar_pending.clear()
        self._avatar_errors.clear()

    def closeEvent(self, event) -> None:
        self._invalidate_avatar_requests()
        self.detach_watcher()
        super().closeEvent(event)

    @staticmethod
    def _ago(ts) -> str:
        if not ts:
            return ""
        delta = max(0, int(time.time() - float(ts)))
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"

    @staticmethod
    def _fmt_time(ts) -> str:
        try:
            import datetime as _dt
            return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return "—"

    @staticmethod
    def _fmt_duration(seconds) -> str:
        s = int(float(seconds))
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    # Avatar fetching

    def _fetch_avatar(self, name: str) -> None:
        if name in self._avatar_pending or name in self._avatars:
            return
        self._avatar_pending.add(name)
        generation = self._avatar_generation
        thread = QThread()
        fetcher = _AvatarFetcher(name, generation)
        fetcher.moveToThread(thread)
        thread.started.connect(fetcher.run)
        fetcher.completed.connect(self._on_avatar_completed)
        fetcher.completed.connect(thread.quit)
        thread.finished.connect(fetcher.deleteLater)

        def _cleanup():
            _ACTIVE_AVATAR_WORKERS.pop(thread, None)
            if thread in self._avatar_threads:
                self._avatar_threads.remove(thread)
            if fetcher in self._avatar_fetchers:
                self._avatar_fetchers.remove(fetcher)

        connect_thread_cleanup(thread, _cleanup)
        _ACTIVE_AVATAR_WORKERS[thread] = fetcher
        self._avatar_threads.append(thread)
        self._avatar_fetchers.append(fetcher)
        self._refresh_online_list()
        thread.start()

    @pyqtSlot(str, object, int)
    def _on_avatar_completed(self, name: str, data: object, generation: int) -> None:
        # The worker still completes/retires if stale, but stale generations
        # cannot revive or mutate the current server's player list.
        if generation != self._avatar_generation:
            return
        self._avatar_pending.discard(name)
        pix = QPixmap()
        if isinstance(data, (bytes, bytearray)) and pix.loadFromData(bytes(data)) and not pix.isNull():
            self._avatars[name] = pix
            self._avatar_errors.discard(name)
        else:
            self._avatar_errors.add(name)
            self._avatars.pop(name, None)
        if name in self._online or name in self._seen:
            self._refresh_online_list()

    # List rendering

    # Player actions (click a name in the online list)

    def _selected_player(self) -> str | None:
        item = self._online_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.ItemDataRole.UserRole)
        return str(name) if name else None

    def _send(self, command: str) -> bool:
        """Send a console command for the current instance via tmux."""
        if self._instance is None:
            return False
        return self._tmux.send_command(self._instance, command)

    def _show_player_menu(self, pos) -> None:
        item = self._online_list.itemAt(pos) if hasattr(pos, "x") else None
        if item is not None:
            self._online_list.setCurrentItem(item)
        name = self._selected_player()
        if not name:
            return

        online = name in self._online

        menu = QMenu(self)
        hdr = menu.addAction(name if online else f"{name}  (offline)")
        hdr.setEnabled(False)
        menu.addSeparator()

        menu.addAction("Player info / stats…", lambda: self._show_player_stats(name))
        menu.addSeparator()

        menu.addAction(f"Make {name} an operator", lambda: self._send(f"op {name}"))
        menu.addAction(f"Remove operator from {name}", lambda: self._send(f"deop {name}"))
        menu.addSeparator()

        gm = menu.addMenu("Gamemode")
        gm.setEnabled(online)
        for mode in ("survival", "creative", "adventure", "spectator"):
            gm.addAction(mode.capitalize(),
                         lambda m=mode: self._send(f"gamemode {m} {name}"))

        fx = menu.addMenu("Quick effects")
        fx.setEnabled(online)
        # (label, command) -- hidden particles (last arg 'true') keep it clean.
        for label, cmd in (
            ("Heal",                  f"effect give {name} minecraft:instant_health 1 4 true"),
            ("Feed (saturation)",     f"effect give {name} minecraft:saturation 1 10 true"),
            ("Fire resistance (5m)",  f"effect give {name} minecraft:fire_resistance 300 0 true"),
            ("Night vision (5m)",     f"effect give {name} minecraft:night_vision 300 0 true"),
            ("Water breathing (5m)",  f"effect give {name} minecraft:water_breathing 300 0 true"),
            ("Clear all effects",     f"effect clear {name}"),
        ):
            fx.addAction(label, lambda c=cmd: self._send(c))

        tp_act = menu.addAction("Teleport…", lambda: self._teleport_dialog(name))
        tp_act.setEnabled(online)
        give_act = menu.addAction("Give item…", lambda: self._give_item(name))
        give_act.setEnabled(online)
        whisper_act = menu.addAction("Whisper…", lambda: self._whisper(name))
        whisper_act.setEnabled(online)
        menu.addSeparator()
        kick_act = menu.addAction("Kick…", lambda: self._kick(name))
        kick_act.setEnabled(online)
        menu.addAction("Ban…", lambda: self._ban(name))
        menu.addAction("Pardon (unban)", lambda: self._send(f"pardon {name}"))

        if not online and name in self._seen:
            menu.addSeparator()
            menu.addAction("Forget (remove from recently played)",
                           lambda: self._forget_player(name))

        menu.exec(self._online_list.mapToGlobal(pos)
                  if hasattr(pos, "x") else self._online_list.cursor().pos())

    def _give_item(self, name: str) -> None:
        item, ok = QInputDialog.getText(
            self, "Give item", f"Item to give {name} (e.g. minecraft:diamond 64):")
        if ok and item.strip():
            self._send(f"give {name} {item.strip()}")

    def _whisper(self, name: str) -> None:
        msg, ok = QInputDialog.getText(
            self, "Whisper", f"Private message to {name}:")
        if ok and msg.strip():
            self._send(f"tell {name} {msg.strip()}")

    def _kick(self, name: str) -> None:
        reason, ok = QInputDialog.getText(
            self, "Kick player", f"Reason for kicking {name} (optional):")
        if ok:
            self._send(f"kick {name} {reason.strip()}".rstrip())

    def _ban(self, name: str) -> None:
        if QMessageBox.question(
            self, "Ban player", f"Ban {name} from the server?"
        ) == QMessageBox.StandardButton.Yes:
            self._send(f"ban {name}")

    def _forget_player(self, name: str) -> None:
        self._seen.pop(name, None)
        if self._seen_loading:
            self._seen_deleted_during_load.add(name)
            self._seen_updates_during_load.pop(name, None)
        else:
            self._save_seen()
        self._refresh_online_list()

    def _teleport_dialog(self, name: str) -> None:
        dlg = _TeleportDialog(name, sorted(self._online), self)
        if dlg.exec():
            cmd = dlg.command()
            if cmd:
                self._send(cmd)

    def _show_player_stats(self, name: str) -> None:
        lines = [f"Player: {name}", ""]
        rec = self._seen.get(name)
        if rec:
            if rec.get("first_seen"):
                lines.append(f"First seen:  {self._fmt_time(rec['first_seen'])}")
            if rec.get("last_seen"):
                lines.append(f"Last seen:   {self._fmt_time(rec['last_seen'])}"
                             f"  ({self._ago(rec['last_seen'])})")
            if rec.get("sessions"):
                lines.append(f"Sessions:    {int(rec['sessions'])}")
            if rec.get("seconds_played"):
                lines.append("Tracked playtime (this app): "
                             f"{self._fmt_duration(rec['seconds_played'])}")
            lines.append("")

        stats = self._read_world_stats(name)
        if stats:
            lines.append("World stats:")
            lines.extend(f"  {label}: {value}" for label, value in stats)
        else:
            lines.append("World stats: not available yet "
                         "(no saved stats for this player, or stats are off).")

        QMessageBox.information(self, f"{name} — info", "\n".join(lines))

    def _uuid_for(self, name: str) -> str | None:
        if self._instance is None:
            return None
        base = Path(self._instance.path)
        for fn in ("usercache.json", "whitelist.json", "ops.json"):
            f = base / fn
            try:
                if f.exists():
                    for e in _read_json_file(f):
                        if isinstance(e, dict) and \
                                e.get("name", "").lower() == name.lower():
                            uid = e.get("uuid")
                            if uid:
                                return str(uid)
            except (json.JSONDecodeError, OSError):
                continue
        return None

    def _read_world_stats(self, name: str) -> list[tuple[str, str]]:
        if self._instance is None:
            return []
        uid = self._uuid_for(name)
        if not uid:
            return []
        base = Path(self._instance.path)
        candidates = list(self._instance.get_world_names()) + ["world"]
        stats_file = None
        for w in candidates:
            cand = base / w / "stats" / f"{uid}.json"
            if cand.exists():
                stats_file = cand
                break
        if stats_file is None:
            return []
        try:
            data = _read_json_file(stats_file)
        except (json.JSONDecodeError, OSError):
            return []
        custom = (data.get("stats", {}) or {}).get("minecraft:custom", {}) or {}
        out: list[tuple[str, str]] = []
        play = custom.get("minecraft:play_time") or custom.get("minecraft:play_one_minute")
        if play is not None:
            out.append(("Play time", self._fmt_duration(float(play) / 20.0)))
        for key, label in (
            ("minecraft:deaths", "Deaths"),
            ("minecraft:mob_kills", "Mob kills"),
            ("minecraft:player_kills", "Player kills"),
            ("minecraft:jump", "Jumps"),
        ):
            if key in custom:
                out.append((label, str(custom[key])))
        for key, label in (
            ("minecraft:damage_dealt", "Damage dealt"),
            ("minecraft:damage_taken", "Damage taken"),
        ):
            if key in custom:
                out.append((label, f"{float(custom[key]) / 10.0:.1f} hearts"))
        walk = custom.get("minecraft:walk_one_cm")
        if walk is not None:
            out.append(("Distance walked", f"{float(walk) / 100000.0:.1f} km"))
        return out

    def _refresh_online_list(self) -> None:
        self._online_list.clear()

        # Online players (green, with avatar).
        if self._online:
            for name in sorted(self._online):
                item = QListWidgetItem(f"  {name}")
                item.setForeground(QColor(theme.GREEN))
                item.setData(Qt.ItemDataRole.UserRole, name)
                pix = self._avatar_for(name)
                if pix is not None:
                    item.setIcon(QIcon(pix))
                elif name in self._avatar_pending:
                    item.setIcon(QIcon(self._avatar_placeholder("loading")))
                    item.setToolTip("Loading avatar; player actions remain available.")
                elif name in self._avatar_errors:
                    item.setIcon(QIcon(self._avatar_placeholder("error")))
                    item.setToolTip("Avatar unavailable; player actions remain available.")
                self._online_list.addItem(item)
        else:
            empty = QListWidgetItem("  No players online")
            empty.setForeground(QColor(theme.SURFACE2))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._online_list.addItem(empty)

        # Recently played — players we've seen before who are now offline.
        offline = [n for n in self._seen if n not in self._online]
        if offline:
            offline.sort(key=lambda n: self._seen[n].get("last_seen", 0), reverse=True)
            sep = QListWidgetItem("  — recently played —")
            sep.setForeground(QColor(theme.SURFACE2))
            sep.setFlags(Qt.ItemFlag.NoItemFlags)
            self._online_list.addItem(sep)
            for name in offline[:12]:
                ago = self._ago(self._seen[name].get("last_seen"))
                label = f"  {name}   ·   {ago}" if ago else f"  {name}"
                item = QListWidgetItem(label)
                item.setForeground(QColor(theme.SUBTEXT))
                item.setData(Qt.ItemDataRole.UserRole, name)
                pix = self._avatar_for(name)
                if pix is not None:
                    item.setIcon(QIcon(pix))
                elif name in self._avatar_pending:
                    item.setIcon(QIcon(self._avatar_placeholder("loading")))
                    item.setToolTip("Loading avatar; player actions remain available.")
                elif name in self._avatar_errors:
                    item.setIcon(QIcon(self._avatar_placeholder("error")))
                    item.setToolTip("Avatar unavailable; player actions remain available.")
                self._online_list.addItem(item)


# Per-file list widget

class _PlayerListWidget(QWidget):
    """Reusable editor for whitelist / ops / banned JSON files."""

    def __init__(self, filename: str, allow_add: bool, parent=None):
        super().__init__(parent)
        self._filename  = filename
        self._allow_add = allow_add
        self._path: Path | None = None
        self._instance: ServerInstance | None = None
        self._tmux = TmuxManager()
        self._data: list[dict]  = []
        self._load_generation = 0
        self._render_generation = 0
        self._render_cursor = 0
        self._loading = False
        self._editable = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)

        if self._allow_add:
            add_row = QHBoxLayout()
            self._name_input = QLineEdit()
            self._name_input.setAccessibleName(
                f"Player name to add to {self._filename}"
            )
            self._name_input.setPlaceholderText("Player name…")
            self._name_input.returnPressed.connect(self._add_player)
            self._add_btn = QPushButton("+ Add")
            self._add_btn.setAccessibleName(f"Add player to {self._filename}")
            # The plus sign and label need a little more room under the
            # styled font metrics used on Linux CI.
            self._add_btn.setFixedWidth(68)
            self._add_btn.clicked.connect(self._add_player)
            add_row.addWidget(self._name_input, stretch=1)
            add_row.addWidget(self._add_btn)
            layout.addLayout(add_row)

        self._table = QTableWidget(0, 3)
        self._table.setAccessibleName(f"Players in {self._filename}")
        self._table.setAccessibleDescription(
            "Player names and UUIDs. Use the Remove button in a row to "
            f"remove that player from {self._filename}."
        )
        self._table.setHorizontalHeaderLabels(["Name", "UUID", ""])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(2, 36)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self._table, stretch=1)

        self._status = QLabel("")
        self._status.setAccessibleName(f"{self._filename} player list status")
        self._status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(self._status)

    def begin_async_load(
        self, instance: ServerInstance, filename: str, generation: int
    ) -> None:
        """Prepare an empty, non-editable list while its JSON is read off-thread."""
        self._load_generation = generation
        self._render_generation += 1
        self._loading = True
        self._editable = False
        self._instance = instance
        self._path = Path(instance.path) / filename
        self._data = []
        self._table.setRowCount(0)
        self._table.setEnabled(False)
        if self._allow_add:
            self._name_input.setEnabled(False)
            self._add_btn.setEnabled(False)
        self._status.setText(f"{filename} loading…")

    def apply_async_data(self, generation: int, payload: object) -> None:
        if generation != self._load_generation:
            return
        if not isinstance(payload, dict):
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(
                f"{self._filename} could not be read; editing disabled",
                editable=False,
            )
            return
        if payload.get("error"):
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(
                f"{self._filename} could not be read safely; editing disabled. "
                "Reselect the server to retry.",
                editable=False,
            )
            return
        if not payload.get("exists", False):
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(f"{self._filename} not found")
            return

        data = payload.get("data")
        if not isinstance(data, list) or any(
            not isinstance(entry, dict) for entry in data
        ):
            # Never convert an unreadable, malformed, or wrong-schema file into
            # an editable empty list: a later mutation could erase server data.
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(
                f"{self._filename} could not be read safely; editing disabled. "
                "Reselect the server to retry.",
                editable=False,
            )
            return

        self._data = data
        self._render_generation += 1
        render_generation = self._render_generation
        self._render_cursor = 0
        self._table.setRowCount(0)
        if not self._data:
            self._finish_async_load("0 entries")
            return
        self._status.setText(f"Loading entries… 0 / {len(self._data)}")
        QTimer.singleShot(
            0, lambda g=render_generation: self._render_async_chunk(g)
        )

    def _finish_async_load(self, status: str, *, editable: bool = True) -> None:
        self._loading = False
        self._editable = editable
        self._table.setEnabled(editable)
        if self._allow_add:
            self._name_input.setEnabled(editable)
            self._add_btn.setEnabled(editable)
        self._status.setText(status)

    def _render_async_chunk(self, generation: int) -> None:
        if generation != self._render_generation or not self._loading:
            return
        start = self._render_cursor
        end = min(start + 40, len(self._data))
        if self._table.rowCount() < end:
            self._table.setRowCount(end)
        for row in range(start, end):
            self._render_row(row, self._data[row])
        self._render_cursor = end
        if end < len(self._data):
            self._status.setText(
                f"Loading entries… {end:,} / {len(self._data):,}"
            )
            # Yield to paint/input between batches on large player lists.
            QTimer.singleShot(
                1, lambda g=generation: self._render_async_chunk(g)
            )
            return
        count = len(self._data)
        self._finish_async_load(
            f"{count} entr{'y' if count == 1 else 'ies'}"
        )

    def load(self, instance: ServerInstance, filename: str) -> None:
        """Synchronous compatibility path for direct widget callers."""
        self._load_generation += 1
        self._render_generation += 1
        self._loading = False
        self._editable = False
        self._instance = instance
        self._path = Path(instance.path) / filename
        try:
            self._path.stat()
        except FileNotFoundError:
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(f"{filename} not found")
            return
        except OSError:
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(
                f"{filename} could not be read safely; editing disabled. "
                "Reselect the server to retry.",
                editable=False,
            )
            return
        try:
            data = _read_json_file(self._path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(
                f"{filename} could not be read safely; editing disabled. "
                "Reselect the server to retry.",
                editable=False,
            )
            return
        if not isinstance(data, list) or any(
            not isinstance(entry, dict) for entry in data
        ):
            self._data = []
            self._table.setRowCount(0)
            self._finish_async_load(
                f"{filename} has an invalid JSON structure; editing disabled.",
                editable=False,
            )
            return
        self._data = data
        self._editable = True
        self._refresh_table()

    def _render_row(self, row: int, entry: dict) -> None:
        name = str(entry.get("name", "?"))
        uid = str(entry.get("uuid", "?"))
        self._table.setItem(row, 0, QTableWidgetItem(name))
        uid_item = QTableWidgetItem(uid)
        uid_item.setForeground(QColor(theme.SURFACE2))
        self._table.setItem(row, 1, uid_item)
        rm = QPushButton("Remove")
        rm.setMinimumWidth(56)
        rm.setObjectName("DangerButton")
        rm.setAccessibleName(f"Remove {name} from {self._filename}")
        rm.clicked.connect(lambda _=False, r=row: self._remove_player(r))
        self._table.setCellWidget(row, 2, rm)
        self._table.setRowHeight(row, 30)

    def _refresh_table(self) -> None:
        # Immediate compatibility renderer for direct calls and post-edit UI.
        # Callers reaching this renderer own a validated in-memory snapshot.
        self._render_generation += 1
        self._loading = False
        self._editable = True
        self._table.setEnabled(self._editable)
        if self._allow_add:
            self._name_input.setEnabled(self._editable)
            self._add_btn.setEnabled(self._editable)
        self._table.setRowCount(len(self._data))
        for row, entry in enumerate(self._data):
            self._render_row(row, entry)
        n = len(self._data)
        self._status.setText(f"{n} entr{'y' if n == 1 else 'ies'}")

    def _add_player(self) -> None:
        if self._loading or not self._editable:
            return
        name = self._name_input.text().strip()
        if not name:
            return
        if any(e.get("name", "").lower() == name.lower() for e in self._data):
            QMessageBox.information(self, "Already Listed",
                                    f"{name} is already in this list.")
            return
        if self._instance is None:
            return
        running = self._tmux.probe_running(self._instance)
        if running is None:
            QMessageBox.warning(
                self, "Server status unavailable",
                "Crucible could not verify the configured tmux session. No command "
                "was sent; retry when the status check recovers.",
            )
            return
        if not running:
            if self._tmux._unmanaged_pids(self._instance):
                detail = (
                    "The server appears to be running outside its configured tmux "
                    "session, so Crucible cannot safely address its console."
                )
            else:
                detail = "The server is not running in its configured tmux session."
            QMessageBox.warning(
                self, "Managed server console unavailable",
                detail + " Crucible will not invent a UUID; restore/start the managed "
                "session and try again.",
            )
            return
        command = "whitelist add" if self._filename == "whitelist.json" else "op"
        ok, detail = self._tmux.send_command_result(
            self._instance, f"{command} {name}"
        )
        if not ok:
            QMessageBox.critical(
                self, "Command failed",
                f"Could not send the command to the server:\n{detail}",
            )
            return
        self._name_input.clear()
        self._status.setText(f"Sent: {command} {name} — refreshing…")
        instance = self._instance
        filename = self._filename
        QTimer.singleShot(
            1500, lambda: self._reload_if_current(instance, filename)
        )

    def _reload_if_current(self, instance: ServerInstance, filename: str) -> None:
        if self._instance is instance and self._filename == filename:
            self.load(instance, filename)

    def _remove_player(self, row: int) -> None:
        if self._loading or not self._editable:
            return
        if 0 <= row < len(self._data):
            name = self._data[row].get("name", "?")
            reply = QMessageBox.question(
                self, "Remove Player",
                f"Remove {name} from {self._filename}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                del self._data[row]
                self._save()
                self._refresh_table()

    def _save(self) -> None:
        if self._path and self._editable:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self._path)
