"""
crucible/ui/tabs/console_tab.py

Console tab: live log tail, command input with history, TPS/player status bar.
"""

from __future__ import annotations

import re
from collections import deque

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QLineEdit, QPushButton,
    QLabel, QCheckBox, QSizePolicy,
)

from ...data.instance_model import ServerInstance
from ...process.log_watcher import LogWatcher
from .. import theme

MAX_LINES    = 2000
HISTORY_SIZE = 100

# Map log level → hex color
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

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Log view ──
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

        # Detect manual scroll-up → disable auto-scroll
        self._view.verticalScrollBar().valueChanged.connect(self._on_scroll)

        layout.addWidget(self._view, stretch=1)

        # ── Status bar row ──
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
        self._server_state_label.setStyleSheet(f"font-size: 11px; font-weight: 600;")

        sr_layout.addWidget(self._tps_label)
        sr_layout.addSpacing(16)
        sr_layout.addWidget(self._players_label)
        sr_layout.addStretch()
        sr_layout.addWidget(self._server_state_label)

        layout.addWidget(status_row)

        # ── Command input row ──
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

        self._cmd_input = QLineEdit()
        self._cmd_input.setObjectName("CommandInput")
        self._cmd_input.setPlaceholderText("Send command…  (↑↓ for history)")
        self._cmd_input.returnPressed.connect(self._send_command)
        self._cmd_input.keyPressEvent = self._cmd_key_press
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

        or_layout.addStretch()

        layout.addWidget(cmd_row)
        layout.addWidget(opts_row)

        # ── Quick commands row ──
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

        # Each tuple: (button label, command to send)
        _QUICK_CMDS = [
            ("TPS",         "/forge tps"),
            ("List",        "list"),
            ("Save",        "save-all"),
            ("Whitelist",   "whitelist list"),
            ("Say…",        None),   # None = open mini prompt
        ]
        for label, cmd in _QUICK_CMDS:
            btn = QPushButton(label)
            btn.setFixedHeight(24)
            btn.setStyleSheet(
                f"font-size: 11px; padding: 2px 8px; "
                f"border-radius: 4px;"
            )
            if cmd is not None:
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

    # ── Public API ────────────────────────────────────────────────────────────

    def attach(self, instance: ServerInstance, watcher: LogWatcher) -> None:
        """Connect to a new instance and its log watcher."""
        self.detach()
        self._instance = instance
        self._watcher  = watcher
        self._active_players.clear()
        self._update_player_label()
        self._view.clear()

        watcher.new_lines.connect(self._on_new_lines)
        watcher.tps_update.connect(self._on_tps)
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

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(list)
    def _on_new_lines(self, lines: list[str]) -> None:
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        for line in lines:
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

    @pyqtSlot(float)
    def _on_tps(self, tps: float) -> None:
        color = (
            theme.GREEN  if tps >= 19.0 else
            theme.YELLOW if tps >= 15.0 else
            theme.RED
        )
        self._tps_label.setText(f"TPS: {tps:.1f}")
        self._tps_label.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")

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
        self._tps_label.setText("TPS: —")
        self._tps_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        self._set_state("○ Restarting…", theme.YELLOW)

    @pyqtSlot()
    def _on_log_missing(self) -> None:
        self._set_state("○ No log file yet — server offline or still starting", theme.SURFACE2)

    # ── Internal helpers ──────────────────────────────────────────────────────

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
        # Don't clobber a more-specific log-watcher message for running state —
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

    # ── Quick commands ────────────────────────────────────────────────────────

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

    def _quick_say(self) -> None:
        """Open the command input pre-filled with 'say ' for a broadcast message."""
        self._cmd_input.setText("say ")
        self._cmd_input.setFocus()
        self._cmd_input.setCursorPosition(len("say "))

    # ── Command sending ───────────────────────────────────────────────────────

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
            self._append_system(f"Failed to send (is server running?)")

        self._cmd_input.clear()

    def _cmd_key_press(self, event) -> None:
        """History navigation: ↑/↓ in command input."""
        from PyQt6.QtCore import Qt
        key = event.key()
        if key == Qt.Key.Key_Up:
            if self._history:
                if self._hist_idx == -1:
                    self._hist_idx = len(self._history) - 1
                elif self._hist_idx > 0:
                    self._hist_idx -= 1
                self._cmd_input.setText(self._history[self._hist_idx])
        elif key == Qt.Key.Key_Down:
            if self._hist_idx >= 0:
                self._hist_idx += 1
                if self._hist_idx >= len(self._history):
                    self._hist_idx = -1
                    self._cmd_input.clear()
                else:
                    self._cmd_input.setText(self._history[self._hist_idx])
        else:
            # Default handling for all other keys
            QLineEdit.keyPressEvent(self._cmd_input, event)

    # ── Auto-scroll ───────────────────────────────────────────────────────────

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

    # ── Open log ──────────────────────────────────────────────────────────────

    def _open_log(self) -> None:
        if self._instance is None:
            return
        log = self._instance.get_log_path()
        if log is None:
            return
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log)))
