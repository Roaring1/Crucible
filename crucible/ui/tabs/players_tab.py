"""
crucible/ui/tabs/players_tab.py

Player management tab.

Top:    Online Now  — live from LogWatcher signals, with 20×20 player-head
        avatars (cached to disk at ~/.local/share/crucible-backups/avatars/).
Bottom: Sub-tabs    — Whitelist | Ops | Banned

Avatar cache: fetched from minotar.net on first join, stored as PNG.
Re-fetched if older than 7 days.  Network failures silently fall back
to stale cache, or show no icon if no cache exists.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import Qt, QObject, QSize, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem,
    QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
    QLineEdit, QPushButton, QMessageBox,
)

from ...data.instance_model import ServerInstance
from ...process.log_watcher import LogWatcher
from .. import theme

_AVATAR_CACHE_DIR = Path.home() / ".local" / "share" / "crucible-backups" / "avatars"
_AVATAR_MAX_AGE_S = 7 * 24 * 3600


# ── Avatar fetcher ─────────────────────────────────────────────────────────────

class _AvatarFetcher(QObject):
    """
    Loads a player-head PNG.  Checks disk cache first; only hits minotar.net
    if the cached file is missing or older than 7 days.
    """
    fetched = pyqtSignal(str, QPixmap)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name

    def _cache_path(self) -> Path:
        _AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return _AVATAR_CACHE_DIR / f"{self._name}.png"

    def run(self) -> None:
        cache = self._cache_path()

        # Fresh disk cache — no network needed
        if cache.exists():
            age = time.time() - cache.stat().st_mtime
            if age < _AVATAR_MAX_AGE_S:
                pix = QPixmap(str(cache))
                if not pix.isNull():
                    self.fetched.emit(self._name, pix)
                    return

        # Fetch from network
        try:
            import urllib.request
            url = f"https://minotar.net/avatar/{self._name}/20"
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = resp.read()
            cache.write_bytes(data)
            pix = QPixmap()
            pix.loadFromData(data)
            if not pix.isNull():
                self.fetched.emit(self._name, pix)
        except Exception:
            # Offline / minotar down — try stale cache
            if cache.exists():
                pix = QPixmap(str(cache))
                if not pix.isNull():
                    self.fetched.emit(self._name, pix)


# ── Main tab ───────────────────────────────────────────────────────────────────

class PlayersTab(QWidget):
    """Online players + whitelist/ops/banned management."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
        self._watcher:  LogWatcher | None     = None
        self._online:   set[str]              = set()
        self._avatars:  dict[str, QPixmap]    = {}
        self._avatar_threads:  list[QThread]        = []
        self._avatar_fetchers: list[_AvatarFetcher] = []
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

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
        self._online_list.setFixedHeight(110)
        self._online_list.setIconSize(QSize(20, 20))
        self._online_list.setStyleSheet(
            f"background: {theme.SURFACE0}; border-radius: 4px;"
        )
        layout.addWidget(self._online_list)

        sub = QTabWidget()
        sub.setDocumentMode(True)
        self._whitelist_w = _PlayerListWidget("whitelist.json",      allow_add=True)
        self._ops_w       = _PlayerListWidget("ops.json",            allow_add=True)
        self._banned_w    = _PlayerListWidget("banned-players.json", allow_add=False)
        sub.addTab(self._whitelist_w, "Whitelist")
        sub.addTab(self._ops_w,       "Ops")
        sub.addTab(self._banned_w,    "Banned")
        layout.addWidget(sub, stretch=1)

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._online.clear()
        self._refresh_online_list()
        self._whitelist_w.load(instance.path, "whitelist.json")
        self._ops_w.load(instance.path,       "ops.json")
        self._banned_w.load(instance.path,    "banned-players.json")

    def attach_watcher(self, watcher: LogWatcher) -> None:
        self.detach_watcher()
        self._watcher = watcher
        watcher.player_joined.connect(self._on_joined)
        watcher.player_left.connect(self._on_left)

    def detach_watcher(self) -> None:
        if self._watcher:
            try:
                self._watcher.player_joined.disconnect(self._on_joined)
                self._watcher.player_left.disconnect(self._on_left)
            except (RuntimeError, TypeError):
                pass
            self._watcher = None

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_joined(self, name: str) -> None:
        self._online.add(name)
        self._refresh_online_list()
        if name not in self._avatars:
            self._fetch_avatar(name)

    @pyqtSlot(str)
    def _on_left(self, name: str) -> None:
        self._online.discard(name)
        self._refresh_online_list()

    # ── Avatar fetching ───────────────────────────────────────────────────────

    def _fetch_avatar(self, name: str) -> None:
        thread  = QThread()
        fetcher = _AvatarFetcher(name)
        fetcher.moveToThread(thread)
        thread.started.connect(fetcher.run)
        fetcher.fetched.connect(self._on_avatar_fetched)
        fetcher.fetched.connect(thread.quit)

        def _cleanup():
            if thread in self._avatar_threads:
                self._avatar_threads.remove(thread)
            if fetcher in self._avatar_fetchers:
                self._avatar_fetchers.remove(fetcher)

        thread.finished.connect(_cleanup)
        self._avatar_threads.append(thread)
        self._avatar_fetchers.append(fetcher)
        thread.start()

    @pyqtSlot(str, QPixmap)
    def _on_avatar_fetched(self, name: str, pix: QPixmap) -> None:
        self._avatars[name] = pix
        if name in self._online:
            self._refresh_online_list()

    # ── List rendering ────────────────────────────────────────────────────────

    def _refresh_online_list(self) -> None:
        self._online_list.clear()
        if not self._online:
            item = QListWidgetItem("  No players online")
            item.setForeground(QColor(theme.SURFACE2))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._online_list.addItem(item)
            return
        for name in sorted(self._online):
            item = QListWidgetItem(f"  {name}")
            item.setForeground(QColor(theme.GREEN))
            pix = self._avatars.get(name)
            if pix and not pix.isNull():
                item.setIcon(QIcon(pix))
            self._online_list.addItem(item)


# ── Per-file list widget ───────────────────────────────────────────────────────

class _PlayerListWidget(QWidget):
    """Reusable editor for whitelist / ops / banned JSON files."""

    def __init__(self, filename: str, allow_add: bool, parent=None):
        super().__init__(parent)
        self._filename  = filename
        self._allow_add = allow_add
        self._path: Path | None = None
        self._data: list[dict]  = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)

        if self._allow_add:
            add_row = QHBoxLayout()
            self._name_input = QLineEdit()
            self._name_input.setPlaceholderText("Player name…")
            self._name_input.returnPressed.connect(self._add_player)
            self._add_btn = QPushButton("+ Add")
            self._add_btn.setFixedWidth(64)
            self._add_btn.clicked.connect(self._add_player)
            add_row.addWidget(self._name_input, stretch=1)
            add_row.addWidget(self._add_btn)
            layout.addLayout(add_row)

        self._table = QTableWidget(0, 3)
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
        self._status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(self._status)

    def load(self, server_path: str, filename: str) -> None:
        self._path = Path(server_path) / filename
        if not self._path.exists():
            self._data = []
            self._table.setRowCount(0)
            self._status.setText(f"{filename} not found")
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._data = []
        self._refresh_table()

    def _refresh_table(self) -> None:
        self._table.setRowCount(len(self._data))
        for row, entry in enumerate(self._data):
            name = entry.get("name", "?")
            uid  = entry.get("uuid", "?")
            self._table.setItem(row, 0, QTableWidgetItem(name))
            uid_item = QTableWidgetItem(uid)
            uid_item.setForeground(QColor(theme.SURFACE2))
            self._table.setItem(row, 1, uid_item)
            rm = QPushButton("×")
            rm.setFixedWidth(28)
            rm.setObjectName("DangerButton")
            rm.clicked.connect(lambda _=False, r=row: self._remove_player(r))
            self._table.setCellWidget(row, 2, rm)
            self._table.setRowHeight(row, 30)
        n = len(self._data)
        self._status.setText(f"{n} entr{'y' if n == 1 else 'ies'}")

    def _add_player(self) -> None:
        name = self._name_input.text().strip()
        if not name:
            return
        if any(e.get("name", "").lower() == name.lower() for e in self._data):
            QMessageBox.information(self, "Already Listed",
                                    f"{name} is already in this list.")
            return
        self._data.append({"uuid": str(uuid.uuid4()), "name": name})
        self._save()
        self._refresh_table()
        self._name_input.clear()

    def _remove_player(self, row: int) -> None:
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
        if self._path:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self._path)
