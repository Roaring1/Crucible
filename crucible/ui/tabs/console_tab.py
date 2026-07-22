"""
crucible/ui/tabs/console_tab.py

Console tab: live log tail, command input with history, TPS/player status bar.
"""

from __future__ import annotations

import re
from collections import deque

from PyQt6.QtCore import Qt, QEvent, pyqtSlot
from PyQt6.QtGui import (
    QColor, QFont, QTextCharFormat, QTextCursor,
    QShortcut, QKeySequence, QTextDocument,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QLineEdit, QPushButton,
    QLabel, QCheckBox,
)

from ...data.instance_model import ServerInstance
from ...process.log_watcher import LogWatcher
from .. import theme

MAX_LINES    = 2000
HISTORY_SIZE = 100

# Map log level -> hex color
_LEVEL_RE = re.compile(
    r"\[(?:Server thread|main|Forge Version Check|FMLTweaker)/(\w+)\]"
)
# Dim the timestamp prefix "[HH:MM:SS]"
_TIMESTAMP_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")


def _level_color(line: str) -> str:
    m = _LEVEL_RE.search(line)
    if not m:
        return theme.TEXT
    level = m.group(1).upper()
    return theme.LOG_COLORS.get(level, theme.TEXT)


# Vanilla command vocabulary used for Minecraft-style Tab completion.
_BASE_COMMANDS = [
    "advancement", "attribute", "ban", "ban-ip", "banlist", "bossbar", "clear",
    "clone", "damage", "data", "datapack", "debug", "defaultgamemode", "deop",
    "difficulty", "effect", "enchant", "execute", "experience", "fill",
    "fillbiome", "forceload", "function", "gamemode", "gamerule", "give",
    "help", "item", "jfr", "kick", "kill", "list", "locate", "loot", "me",
    "msg", "op", "pardon", "pardon-ip", "particle", "place", "playsound",
    "publish", "random", "recipe", "reload", "return", "ride", "rotate",
    "save-all", "save-off", "save-on", "say", "schedule", "scoreboard", "seed",
    "setblock", "setidletimeout", "setworldspawn", "spawnpoint", "spectate",
    "spreadplayers", "stop", "stopsound", "summon", "tag", "team", "teleport",
    "tell", "tellraw", "tick", "time", "title", "tp", "transfer", "trigger",
    "w", "weather", "whitelist", "worldborder", "xp",
]

# Fixed second-token vocabularies, keyed by the first command token.
_SUBCOMMANDS = {
    "forge":           ["tps", "track", "generate", "entity", "dimensions", "mods", "tags", "help"],
    "neoforge":        ["tps", "track", "generate", "entity", "dimensions", "mods", "tags", "help"],
    "tick":            ["query", "rate", "freeze", "unfreeze", "step", "sprint"],
    "gamemode":        ["survival", "creative", "adventure", "spectator"],
    "defaultgamemode": ["survival", "creative", "adventure", "spectator"],
    "difficulty":      ["peaceful", "easy", "normal", "hard"],
    "weather":         ["clear", "rain", "thunder"],
    "whitelist":       ["add", "remove", "list", "on", "off", "reload"],
    "time":            ["set", "add", "query"],
    "datapack":        ["list", "enable", "disable"],
    "ban-list":        ["players", "ips"],
    "worldborder":     ["add", "center", "damage", "get", "set", "warning"],
    "schedule":        ["function", "clear"],
    "execute":         ["align", "anchored", "as", "at", "facing", "if", "in",
                        "on", "positioned", "rotated", "run", "store", "summon", "unless"],
}

# Commands whose arguments name a player (offer online player names).
_PLAYER_ARG_CMDS = {
    "tp", "teleport", "kick", "ban", "pardon", "op", "deop", "gamemode",
    "give", "tell", "msg", "w", "spectate", "kill", "effect", "enchant",
    "xp", "experience", "clear", "advancement", "title", "damage", "ride",
    "trigger", "spawnpoint",
}

_DIMENSIONS = ["minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"]

# Lines produced by TPS / tick polling. When "Hide TPS poll output" is on these
# are still parsed for the status-bar readout, but not printed -- otherwise they
# repeat every poll interval and bury real log output.
_PERF_NOISE_RE = re.compile(
    r"The game is running normally"
    r"|Target tick rate:"
    r"|Average time per tick:"
    r"|Percentiles: P50"
    r"|Mean tick time:"
    r"|Mean TPS:"
    r"|TPS from last"
    r"|Server tick times"
)


class _CommandLineEdit(QLineEdit):
    """Command box with Minecraft-style Tab completion and ↑/↓ history.

    Tab / Shift+Tab must be intercepted in ``event()`` because Qt consumes them
    for focus traversal *before* ``keyPressEvent`` runs.
    """

    def __init__(self, owner: "ConsoleTab", parent=None):
        super().__init__(parent)
        self._owner = owner

    def event(self, e):  # noqa: N802 (Qt naming)
        if e.type() == QEvent.Type.KeyPress and e.key() in (
            Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
        ):
            self._owner._handle_tab(backward=(e.key() == Qt.Key.Key_Backtab))
            return True
        return super().event(e)

    def keyPressEvent(self, e):  # noqa: N802
        key = e.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._owner._history_key(key)
            return
        # Any other keystroke invalidates an in-progress completion cycle.
        self._owner._reset_completion()
        super().keyPressEvent(e)


class ConsoleTab(QWidget):
    """
    Displays the live server log and allows sending commands.

    Usage:
        tab = ConsoleTab()
        tab.attach(instance, watcher)   # call when instance is selected
        tab.detach()                    # call when switching away
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher:  LogWatcher | None = None
        self._instance: ServerInstance | None = None
        self._auto_scroll = True
        self._history: deque[str] = deque(maxlen=HISTORY_SIZE)
        self._hist_idx = -1   # -1 = not browsing

        # Latest tick stats (for the focused-only console readout).
        self._last_tps: float | None = None
        self._last_mspt: float | None = None

        # Console display options.
        self._hide_perf = True

        # Tab-completion cycle state.
        self._comp_active = False
        self._comp_cands: list[str] = []
        self._comp_pos = 0
        self._comp_idx = 0
        self._comp_parts: list[str] = []
        self._comp_last = ""

        self._build_ui()

    # UI construction

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Log view
        self._view = QPlainTextEdit()
        self._view.setObjectName("ConsoleView")
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(MAX_LINES)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        font = QFont()
        font.setFamilies(["JetBrains Mono", "Fira Code", "Cascadia Code",
                          "Hack", "DejaVu Sans Mono", "Monospace"])
        font.setPointSize(10)
        font.setFixedPitch(True)
        self._view.setFont(font)

        # Detect manual scroll-up -> disable auto-scroll
        self._view.verticalScrollBar().valueChanged.connect(self._on_scroll)

        layout.addWidget(self._view, stretch=1)

        # Find bar (hidden until Ctrl+F)
        self._find_bar = QWidget()
        self._find_bar.setStyleSheet(
            f"background-color: {theme.MANTLE}; "
            f"border-top: 1px solid {theme.SURFACE1};"
        )
        fb_layout = QHBoxLayout(self._find_bar)
        fb_layout.setContentsMargins(8, 4, 8, 4)
        fb_layout.setSpacing(6)
        fb_label = QLabel("Find:")
        fb_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        fb_layout.addWidget(fb_label)
        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("Search log…")
        self._find_input.returnPressed.connect(lambda: self._find(forward=True))
        self._find_input.textChanged.connect(self._on_find_text_changed)
        fb_layout.addWidget(self._find_input, stretch=1)
        self._find_status = QLabel("")
        self._find_status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        fb_layout.addWidget(self._find_status)
        for label, slot in (("▲", lambda: self._find(forward=False)),
                            ("▼", lambda: self._find(forward=True)),
                            ("✕", self._hide_find_bar)):
            b = QPushButton(label)
            b.setFixedWidth(30)
            b.clicked.connect(slot)
            fb_layout.addWidget(b)
        self._find_bar.setVisible(False)
        layout.addWidget(self._find_bar)

        # Status bar row
        status_row = QWidget()
        status_row.setStyleSheet(
            f"background-color: {theme.CRUST}; "
            f"border-top: 1px solid {theme.SURFACE1};"
        )
        sr_layout = QHBoxLayout(status_row)
        sr_layout.setContentsMargins(8, 4, 8, 4)

        self._tps_label = QLabel("TPS: —")
        self._tps_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        self._players_label = QLabel("Players: —")
        self._players_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        self._server_state_label = QLabel("")
        self._server_state_label.setStyleSheet("font-size: 11px; font-weight: 600;")

        sr_layout.addWidget(self._tps_label)
        sr_layout.addSpacing(16)
        sr_layout.addWidget(self._players_label)
        sr_layout.addStretch()
        sr_layout.addWidget(self._server_state_label)

        layout.addWidget(status_row)

        # Command input row
        cmd_row = QWidget()
        cmd_row.setStyleSheet(
            f"background-color: {theme.MANTLE}; "
            f"border-top: 1px solid {theme.SURFACE1};"
        )
        cr_layout = QHBoxLayout(cmd_row)
        cr_layout.setContentsMargins(8, 6, 8, 6)
        cr_layout.setSpacing(6)

        prompt = QLabel("›")
        prompt.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 16px; font-family: monospace;"
        )
        cr_layout.addWidget(prompt)

        self._cmd_input = _CommandLineEdit(self)
        self._cmd_input.setObjectName("CommandInput")
        self._cmd_input.setPlaceholderText("Send command…  (Tab to complete · ↑↓ history)")
        self._cmd_input.returnPressed.connect(self._send_command)
        cr_layout.addWidget(self._cmd_input, stretch=1)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(64)
        send_btn.clicked.connect(self._send_command)
        cr_layout.addWidget(send_btn)

        # Options row
        opts_row = QWidget()
        opts_row.setStyleSheet(f"background-color: {theme.MANTLE};")
        or_layout = QHBoxLayout(opts_row)
        or_layout.setContentsMargins(8, 0, 8, 6)
        or_layout.setSpacing(12)

        self._autoscroll_cb = QCheckBox("Auto-scroll")
        self._autoscroll_cb.setChecked(True)
        self._autoscroll_cb.toggled.connect(self._on_autoscroll_toggle)
        or_layout.addWidget(self._autoscroll_cb)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._view.clear)
        or_layout.addWidget(clear_btn)

        open_log_btn = QPushButton("Open log file")
        open_log_btn.clicked.connect(self._open_log)
        or_layout.addWidget(open_log_btn)

        copy_btn = QPushButton("Copy log")
        copy_btn.setFixedWidth(72)
        copy_btn.clicked.connect(self._copy_log)
        or_layout.addWidget(copy_btn)

        self._hide_perf_cb = QCheckBox("Hide TPS poll output")
        self._hide_perf_cb.setChecked(True)
        self._hide_perf_cb.setToolTip(
            "Hide the repeating TPS / tick-query responses from the console.\n"
            "They're still used for the TPS readout above."
        )
        self._hide_perf_cb.toggled.connect(self._on_hide_perf_toggle)
        or_layout.addWidget(self._hide_perf_cb)

        or_layout.addStretch()

        layout.addWidget(cmd_row)
        layout.addWidget(opts_row)

        # Quick commands row
        quick_row = QWidget()
        quick_row.setStyleSheet(
            f"background-color: {theme.CRUST}; "
            f"border-top: 1px solid {theme.SURFACE1};"
        )
        qr_layout = QHBoxLayout(quick_row)
        qr_layout.setContentsMargins(8, 4, 8, 4)
        qr_layout.setSpacing(6)

        quick_label = QLabel("Quick:")
        quick_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        qr_layout.addWidget(quick_label)

        # Each tuple: (button label, command to send). "__TPS__"/None are
        # handled specially (TPS is loader-aware; Say opens a mini prompt).
        _QUICK_CMDS = [
            ("TPS",         "__TPS__"),
            ("List",        "list"),
            ("Save",        "save-all"),
            ("Whitelist",   "whitelist list"),
            ("Say…",        None),   # None = open mini prompt
        ]
        for label, cmd in _QUICK_CMDS:
            btn = QPushButton(label)
            btn.setFixedHeight(24)
            btn.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; "
                "border-radius: 4px;"
            )
            if cmd == "__TPS__":
                btn.clicked.connect(self._quick_tps)
            elif cmd is not None:
                btn.clicked.connect(lambda _checked, c=cmd: self._quick_send(c))
            else:
                btn.clicked.connect(self._quick_say)
            qr_layout.addWidget(btn)

        qr_layout.addStretch()
        layout.addWidget(quick_row)

        # Placeholder when no instance is selected
        self._placeholder = QLabel("Select a server instance to view its console.")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 14px;")

        self._active_players: set[str] = set()

        # Keyboard shortcuts: Ctrl+F toggles find, Esc closes it.
        find_sc = QShortcut(QKeySequence.StandardKey.Find, self)
        find_sc.activated.connect(self._toggle_find_bar)
        esc_sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self._find_input)
        esc_sc.activated.connect(self._hide_find_bar)

    # Public API

    def attach(self, instance: ServerInstance, watcher: LogWatcher) -> None:
        """Connect to a new instance and its log watcher."""
        self.detach()
        self._instance = instance
        self._watcher  = watcher
        self._active_players.clear()
        self._last_tps = None
        self._last_mspt = None
        self._render_tps()
        self._update_player_label()
        self._view.clear()

        watcher.new_lines.connect(self._on_new_lines)
        watcher.tps_update.connect(self._on_tps)
        watcher.mspt_update.connect(self._on_mspt)
        watcher.player_joined.connect(self._on_joined)
        watcher.player_left.connect(self._on_left)
        watcher.server_started.connect(self._on_server_started)
        watcher.server_stopping.connect(self._on_server_stopping)
        watcher.log_rotated.connect(self._on_log_rotated)
        watcher.log_missing.connect(self._on_log_missing)

        self._append_system(f"── Attached to {instance.name} ──")
        self._set_state("○ Waiting for log file…", theme.SURFACE2)

    def detach(self) -> None:
        """Disconnect the current watcher."""
        if self._watcher is not None:
            try:
                self._watcher.new_lines.disconnect(self._on_new_lines)
                self._watcher.tps_update.disconnect(self._on_tps)
                self._watcher.mspt_update.disconnect(self._on_mspt)
                self._watcher.player_joined.disconnect(self._on_joined)
                self._watcher.player_left.disconnect(self._on_left)
                self._watcher.server_started.disconnect(self._on_server_started)
                self._watcher.server_stopping.disconnect(self._on_server_stopping)
                self._watcher.log_rotated.disconnect(self._on_log_rotated)
                self._watcher.log_missing.disconnect(self._on_log_missing)
            except (RuntimeError, TypeError):
                pass
            self._watcher  = None
            self._instance = None

    def clear_console(self) -> None:
        self._view.clear()

    # Slots

    @pyqtSlot(list)
    def _on_new_lines(self, lines: list[str]) -> None:
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        for line in lines:
            # Optionally drop the repeating TPS/tick-poll responses (still parsed
            # for the readout by the log watcher; this only hides the echo).
            if self._hide_perf and _PERF_NOISE_RE.search(line):
                continue
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_level_color(line)))
            # Dim the timestamp
            ts_m = _TIMESTAMP_RE.match(line)
            if ts_m:
                dim_fmt = QTextCharFormat()
                dim_fmt.setForeground(QColor(theme.SURFACE2))
                cursor.insertText(ts_m.group(0), dim_fmt)
                remainder = line[ts_m.end():]
                cursor.insertText(remainder + "\n", fmt)
            else:
                cursor.insertText(line + "\n", fmt)

        if self._auto_scroll:
            self._view.verticalScrollBar().setValue(
                self._view.verticalScrollBar().maximum()
            )

    def _render_tps(self) -> None:
        """Paint the TPS label, including MSPT detail when we have it."""
        if self._last_tps is None:
            self._tps_label.setText("TPS: —")
            self._tps_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
            return
        tps = self._last_tps
        color = (
            theme.GREEN  if tps >= 19.0 else
            theme.YELLOW if tps >= 15.0 else
            theme.RED
        )
        text = f"TPS: {tps:.1f}"
        if self._last_mspt is not None:
            text += f"  ·  {self._last_mspt:.1f} mspt"
        self._tps_label.setText(text)
        self._tps_label.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")

    @pyqtSlot(float)
    def _on_tps(self, tps: float) -> None:
        self._last_tps = tps
        self._render_tps()

    @pyqtSlot(float)
    def _on_mspt(self, mspt: float) -> None:
        self._last_mspt = mspt
        self._render_tps()

    @pyqtSlot(str)
    def _on_joined(self, name: str) -> None:
        self._active_players.add(name)
        self._update_player_label()

    @pyqtSlot(str)
    def _on_left(self, name: str) -> None:
        self._active_players.discard(name)
        self._update_player_label()

    @pyqtSlot(float)
    def _on_server_started(self, secs: float) -> None:
        self._set_state(f"● Online  (started in {secs:.1f}s)", theme.GREEN)

    @pyqtSlot()
    def _on_server_stopping(self) -> None:
        self._active_players.clear()
        self._update_player_label()
        self._set_state("● Stopping…", theme.ORANGE)

    @pyqtSlot()
    def _on_log_rotated(self) -> None:
        """Server restarted — wipe stale player list and state."""
        self._active_players.clear()
        self._update_player_label()
        self._last_tps = None
        self._last_mspt = None
        self._render_tps()
        self._set_state("○ Restarting…", theme.YELLOW)

    @pyqtSlot()
    def _on_log_missing(self) -> None:
        self._set_state("○ No log file yet — server offline or still starting", theme.SURFACE2)

    # Internal helpers

    def _update_player_label(self) -> None:
        n = len(self._active_players)
        if n == 0:
            self._players_label.setText("Players: —")
        elif n == 1:
            name = next(iter(self._active_players))
            self._players_label.setText(f"Players: {name}")
        else:
            self._players_label.setText(f"Players: {n}")

    def notify_status(self, status: str) -> None:
        """Called by InstancePanel._update_status_display to keep header and
        console state label in sync.  This is the authoritative path — log-watcher
        signals (server_started, server_stopping, log_rotated) can still override
        with more specific text, but this ensures a crash or external stop is
        always reflected even if no log signal fires.
        """
        mapping = {
            "running":  ("● Online",      theme.GREEN),
            "starting": ("⚡ Starting…",   theme.YELLOW),
            "stopping": ("◌ Stopping…",   theme.ORANGE),
            "stopped":  ("○ Offline",     theme.SURFACE2),
            "tmux_missing": ("⚠ tmux missing", theme.RED),
        }
        text, color = mapping.get(status, (status.capitalize(), theme.SURFACE2))
        # Don't clobber a more-specific log-watcher message for running state --
        # e.g. "● Online  (started in 12.3s)" should survive a health-check ping.
        current = self._server_state_label.text()
        if status == "running" and "Online" in current:
            return
        self._set_state(text, color)

    def _set_state(self, text: str, color: str) -> None:
        self._server_state_label.setText(text)
        self._server_state_label.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;"
        )

    def _append_system(self, msg: str) -> None:
        """Append a dim system message (not from the log file)."""
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(theme.SURFACE2))
        cursor.insertText(f"{msg}\n", fmt)

    # Quick commands

    def _quick_send(self, cmd: str) -> None:
        """Send a preset command directly."""
        if self._instance is None:
            return
        from ...process.tmux_manager import TmuxManager
        tmux = TmuxManager()
        if tmux.send_command(self._instance, cmd):
            self._append_system(f"» {cmd}")
        else:
            self._append_system("Quick command failed (is server running?)")

    def _quick_tps(self) -> None:
        """Send the loader-appropriate TPS command, or explain if there isn't one."""
        if self._instance is None:
            return
        cmd = self._instance.tps_command()
        if not cmd:
            loader = (self._instance.loader or "vanilla") or "vanilla"
            self._append_system(
                f"This server ({loader}) has no built-in TPS command. "
                "TPS is available on Minecraft 1.21+ (any loader), Forge/NeoForge, "
                "and Paper-family servers.")
            return
        self._quick_send(cmd)

    def _quick_say(self) -> None:
        """Open the command input pre-filled with 'say ' for a broadcast message."""
        self._cmd_input.setText("say ")
        self._cmd_input.setFocus()
        self._cmd_input.setCursorPosition(len("say "))

    # Command sending

    def _send_command(self) -> None:
        if self._instance is None:
            return
        cmd = self._cmd_input.text().strip()
        if not cmd:
            return

        from ...process.tmux_manager import TmuxManager
        tmux = TmuxManager()
        if tmux.send_command(self._instance, cmd):
            self._append_system(f"» {cmd}")
            # Save to history
            if not self._history or self._history[-1] != cmd:
                self._history.append(cmd)
            self._hist_idx = -1
        else:
            self._append_system("Failed to send (is server running?)")

        self._cmd_input.clear()

    def _history_key(self, key) -> None:
        """↑/↓ history navigation, invoked by the command line edit."""
        self._reset_completion()
        if key == Qt.Key.Key_Up:
            if self._history:
                if self._hist_idx == -1:
                    self._hist_idx = len(self._history) - 1
                elif self._hist_idx > 0:
                    self._hist_idx -= 1
                self._cmd_input.setText(self._history[self._hist_idx])
                self._cmd_input.setCursorPosition(len(self._cmd_input.text()))
        elif key == Qt.Key.Key_Down:
            if self._hist_idx >= 0:
                self._hist_idx += 1
                if self._hist_idx >= len(self._history):
                    self._hist_idx = -1
                    self._cmd_input.clear()
                else:
                    self._cmd_input.setText(self._history[self._hist_idx])
                    self._cmd_input.setCursorPosition(len(self._cmd_input.text()))

    # Minecraft-style Tab completion

    def _reset_completion(self) -> None:
        self._comp_active = False

    def _known_players(self) -> list[str]:
        """Player names available for argument completion (online players)."""
        return sorted(self._active_players)

    def _completion_candidates(self, parts: list[str], idx: int, prefix: str) -> list[str]:
        pl = prefix.lower()
        # First token = the command itself.
        if idx == 0:
            vocab = set(_BASE_COMMANDS)
            loader = ((self._instance.loader if self._instance else "") or "").lower()
            if loader == "forge":
                vocab.add("forge")
            elif loader == "neoforge":
                vocab.add("neoforge")
            return [c for c in sorted(vocab) if c.startswith(pl)]
        cmd = parts[0].lower()
        prev = parts[idx - 1].lower() if idx >= 1 else ""
        # Dimension argument after 'in' (e.g. 'execute in <dim>').
        if prev == "in":
            return [d for d in _DIMENSIONS if d.startswith(pl)]
        # Fixed subcommand sets.
        if idx == 1 and cmd in _SUBCOMMANDS:
            subs = [s for s in _SUBCOMMANDS[cmd] if s.startswith(pl)]
            if subs:
                return subs
        # Player-name arguments.
        if cmd in _PLAYER_ARG_CMDS:
            names = [n for n in self._known_players() if n.lower().startswith(pl)]
            if names:
                return names
        return []

    def _handle_tab(self, backward: bool = False) -> None:
        """Cycle Tab completions for the token under the cursor (Minecraft-style)."""
        if self._instance is None:
            return
        text = self._cmd_input.text()
        if self._comp_active and text == self._comp_last and self._comp_cands:
            # Keep cycling the existing candidate set.
            step = -1 if backward else 1
            self._comp_pos = (self._comp_pos + step) % len(self._comp_cands)
        else:
            parts = text.split(" ")
            self._comp_idx = len(parts) - 1
            prefix = parts[self._comp_idx]
            cands = self._completion_candidates(parts, self._comp_idx, prefix)
            if not cands:
                self._comp_active = False
                return
            self._comp_parts = parts
            self._comp_cands = cands
            self._comp_pos = (len(cands) - 1) if backward else 0
            if len(cands) > 1:
                # Show the option list once, the way Minecraft lists them.
                shown = "   ".join(cands[:24])
                more = "  …" if len(cands) > 24 else ""
                self._append_system(f"  ⇥ {shown}{more}")
        cand = self._comp_cands[self._comp_pos]
        parts = list(self._comp_parts)
        parts[self._comp_idx] = cand
        new_text = " ".join(parts)
        self._cmd_input.setText(new_text)
        self._cmd_input.setCursorPosition(len(new_text))
        self._comp_last = new_text
        self._comp_active = True

    # Auto-scroll

    def _on_scroll(self, value: int) -> None:
        """If user scrolls away from bottom, pause auto-scroll."""
        sb  = self._view.verticalScrollBar()
        at_bottom = value >= sb.maximum() - 4
        if at_bottom != self._auto_scroll:
            self._auto_scroll = at_bottom
            self._autoscroll_cb.blockSignals(True)
            self._autoscroll_cb.setChecked(at_bottom)
            self._autoscroll_cb.blockSignals(False)

    def _on_autoscroll_toggle(self, checked: bool) -> None:
        self._auto_scroll = checked
        if checked:
            self._view.verticalScrollBar().setValue(
                self._view.verticalScrollBar().maximum()
            )

    # Open log

    def _open_log(self) -> None:
        if self._instance is None:
            return
        log = self._instance.get_log_path()
        if log is None:
            return
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log)))

    # Display options

    def _on_hide_perf_toggle(self, checked: bool) -> None:
        self._hide_perf = checked

    def _copy_log(self) -> None:
        """Copy the whole console buffer to the clipboard (handy for crash help)."""
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._view.toPlainText())
        self._append_system("Console log copied to clipboard.")

    # Find bar

    def _toggle_find_bar(self) -> None:
        if self._find_bar.isVisible():
            self._hide_find_bar()
        else:
            self._find_bar.setVisible(True)
            self._find_input.setFocus()
            self._find_input.selectAll()

    def _hide_find_bar(self) -> None:
        self._find_bar.setVisible(False)
        self._find_status.setText("")
        cursor = self._view.textCursor()
        cursor.clearSelection()
        self._view.setTextCursor(cursor)
        self._cmd_input.setFocus()

    def _on_find_text_changed(self, text: str) -> None:
        if not text:
            self._find_status.setText("")
            return
        # Search from the start of the current selection so typing extends the
        # current match instead of skipping ahead.
        cursor = self._view.textCursor()
        cursor.setPosition(cursor.selectionStart())
        self._view.setTextCursor(cursor)
        self._find(forward=True)

    def _find(self, *, forward: bool) -> None:
        term = self._find_input.text()
        if not term:
            self._find_status.setText("")
            return
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        found = self._view.find(term, flags)
        if not found:
            # Wrap around to the other end and try once more.
            cursor = self._view.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End if not forward
                else QTextCursor.MoveOperation.Start
            )
            self._view.setTextCursor(cursor)
            found = self._view.find(term, flags)
        self._find_status.setText("" if found else "No matches")
