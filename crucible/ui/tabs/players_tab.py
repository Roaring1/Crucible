"""
crucible/ui/tabs/players_tab.py

Player management tab.

Top:    Online Now  — live from LogWatcher signals, with 20×20 player-head
        avatars (cached to disk at ~/.local/share/crucible/avatars/).
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
    QMenu, QInputDialog,
    QDialog, QComboBox, QRadioButton, QButtonGroup,
    QFormLayout, QDialogButtonBox,
)

from ...data.instance_model import ServerInstance
from ...process.log_watcher import LogWatcher
from ...process.tmux_manager import TmuxManager
from .. import theme

_AVATAR_CACHE_DIR = Path.home() / ".local" / "share" / "crucible" / "avatars"
_AVATAR_MAX_AGE_S = 7 * 24 * 3600


# Avatar fetcher

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

        # Fresh disk cache -- no network needed
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
            url = "https://minotar.net/avatar/%s/20" % self._name
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = resp.read()
            cache.write_bytes(data)
            pix = QPixmap()
            pix.loadFromData(data)
            if not pix.isNull():
                self.fetched.emit(self._name, pix)
        except Exception:
            # Offline / minotar down -- try stale cache
            if cache.exists():
                pix = QPixmap(str(cache))
                if not pix.isNull():
                    self.fetched.emit(self._name, pix)


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
        self.setModal(True)
        self.setMinimumWidth(360)
        self._build_ui(online_players)

    def _build_ui(self, online_players: list[str]) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 14, 16, 14)

        lay.addWidget(QLabel(f"Where should {self._name} go?"))

        self._coord_radio  = QRadioButton("To coordinates")
        self._coord_radio.setChecked(True)
        self._player_radio = QRadioButton("To another player")
        grp = QButtonGroup(self)
        grp.addButton(self._coord_radio)
        grp.addButton(self._player_radio)

        lay.addWidget(self._coord_radio)

        form = QFormLayout()
        self._dim_combo = QComboBox()
        for label, _dimid in self._DIMS:
            self._dim_combo.addItem(label)
        form.addRow("Dimension:", self._dim_combo)

        self._x = QLineEdit("~")
        self._y = QLineEdit("~")
        self._z = QLineEdit("~")
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
        self._avatars:  dict[str, QPixmap]    = {}
        self._avatar_threads:  list[QThread]        = []
        self._avatar_fetchers: list[_AvatarFetcher] = []
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
        self._online_list.setFixedHeight(110)
        self._online_list.setIconSize(QSize(20, 20))
        self._online_list.setStyleSheet(
            f"background: {theme.SURFACE0}; border-radius: 4px;"
        )
        # Click a player to act on them (op, kick, ban, teleport, give…).
        self._online_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._online_list.customContextMenuRequested.connect(
            self._show_player_menu)
        self._online_list.itemDoubleClicked.connect(
            lambda item: self._show_player_menu(
                self._online_list.visualItemRect(item).center()))
        layout.addWidget(self._online_list)

        hint = QLabel("Tip: click a player above for actions (op, kick, ban, "
                      "gamemode, teleport, give…).")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 10px;")
        layout.addWidget(hint)

        sub = QTabWidget()
        sub.setDocumentMode(True)
        self._whitelist_w = _PlayerListWidget("whitelist.json",      allow_add=True)
        self._ops_w       = _PlayerListWidget("ops.json",            allow_add=True)
        self._banned_w    = _PlayerListWidget("banned-players.json", allow_add=False)
        sub.addTab(self._whitelist_w, "Whitelist")
        sub.addTab(self._ops_w,       "Ops")
        sub.addTab(self._banned_w,    "Banned")
        layout.addWidget(sub, stretch=1)

    # Public API

    def load(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._online.clear()
        self._join_times.clear()
        self._load_seen()
        self._refresh_online_list()
        self._whitelist_w.load(instance.path, "whitelist.json")
        self._ops_w.load(instance.path,       "ops.json")
        self._banned_w.load(instance.path,    "banned-players.json")

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
        self._refresh_online_list()
        if name not in self._avatars:
            self._fetch_avatar(name)

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
        if self._instance is None:
            self._seen_path = None
            return
        self._seen_path = Path(self._instance.path) / ".crucible" / "players_seen.json"
        try:
            if self._seen_path.exists():
                data = json.loads(self._seen_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._seen = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError):
            self._seen = {}

    def _save_seen(self) -> None:
        if self._seen_path is None:
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
        self._save_seen()

    def _avatar_for(self, name: str) -> QPixmap | None:
        pix = self._avatars.get(name)
        if pix is not None and not pix.isNull():
            return pix
        cache = _AVATAR_CACHE_DIR / f"{name}.png"
        if cache.exists():
            p = QPixmap(str(cache))
            if not p.isNull():
                self._avatars[name] = p
                return p
        return None

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
                    for e in json.loads(f.read_text(encoding="utf-8")):
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
            data = json.loads(stats_file.read_text(encoding="utf-8"))
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
                self._online_list.addItem(item)


# Per-file list widget

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
