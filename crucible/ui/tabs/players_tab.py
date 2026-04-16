"""
crucible/ui/tabs/players_tab.py

Player management tab.

Top:    Online Now  — live from LogWatcher signals
Bottom: Sub-tabs    — Whitelist | Ops | Banned
        Each reads the server's JSON file and lets you add/remove entries.

NOTE on "Add by name":
  We generate a placeholder UUID here. The server replaces it with the
  real UUID on the player's next login (for online-mode=false servers
  this is fine; for online-mode=true the server must be running and the
  whitelist command should be used via the console instead).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor
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


class PlayersTab(QWidget):
    """Online players + whitelist/ops/banned management."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
        self._watcher:  LogWatcher | None     = None
        self._online:   set[str]              = set()
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # ── Online Now section ──
        hdr = QLabel("ONLINE NOW")
        hdr.setStyleSheet(
            f"color: {theme.SUBTEXT}; font-size: 11px; "
            f"font-weight: 600; letter-spacing: 1px;"
        )
        layout.addWidget(hdr)

        self._online_list = QListWidget()
        self._online_list.setFixedHeight(90)
        self._online_list.setStyleSheet(
            f"background: {theme.SURFACE0}; border-radius: 4px;"
        )
        self._empty_label = QListWidgetItem("  No players online")
        self._empty_label.setForeground(QColor(theme.SURFACE2))
        self._empty_label.setFlags(Qt.ItemFlag.NoItemFlags)
        self._online_list.addItem(self._empty_label)
        layout.addWidget(self._online_list)

        # ── JSON list sub-tabs ──
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
        path = instance.path
        self._whitelist_w.load(path, "whitelist.json")
        self._ops_w.load(path,       "ops.json")
        self._banned_w.load(path,    "banned-players.json")

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

    @pyqtSlot(str)
    def _on_left(self, name: str) -> None:
        self._online.discard(name)
        self._refresh_online_list()

    def _refresh_online_list(self) -> None:
        self._online_list.clear()
        if not self._online:
            item = QListWidgetItem("  No players online")
            item.setForeground(QColor(theme.SURFACE2))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._online_list.addItem(item)
            return
        for name in sorted(self._online):
            item = QListWidgetItem(f"  ●  {name}")
            item.setForeground(QColor(theme.GREEN))
            self._online_list.addItem(item)


class _PlayerListWidget(QWidget):
    """
    Reusable widget showing/editing one of Minecraft's player JSON files.

    Handles:
      whitelist.json      → [{"uuid": "...", "name": "..."}]
      ops.json            → [{"uuid": "...", "name": "...", "level": 4, ...}]
      banned-players.json → [{"uuid": "...", "name": "...", "reason": "...", ...}]
    """

    def __init__(self, filename: str, allow_add: bool, parent=None):
        super().__init__(parent)
        self._filename  = filename
        self._allow_add = allow_add
        self._path:  Path | None  = None
        self._data:  list[dict]   = []
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

        self._table = QTableWidget(0, 3)   # Name | UUID | [Remove]
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
            QMessageBox.information(self, "Already Listed", f"{name} is already in this list.")
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
            tmp.write_text(
                json.dumps(self._data, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
