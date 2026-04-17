"""
crucible/ui/tabs/config_tab.py

Config tab: live editor for server.properties.

Displays all key=value pairs in a two-column table.  Important keys
(network, world, gameplay, performance) are highlighted in accent color
and sorted to the top.  Edits are buffered until the user clicks
"Save Changes"; the file is written atomically to avoid corruption.
Comments and blank lines in the original file are preserved on save.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QPushButton, QLineEdit,
    QMessageBox, QAbstractItemView,
)

from ...data.instance_model import ServerInstance
from .. import theme


# Keys we want to sort first and highlight — grouped for readability
_IMPORTANT: dict[str, list[str]] = {
    "Network":     ["server-port", "online-mode", "server-ip",
                    "enable-rcon", "rcon.port", "rcon.password",
                    "white-list"],
    "World":       ["level-name", "level-seed", "level-type",
                    "generate-structures", "allow-nether",
                    "spawn-monsters", "spawn-animals", "spawn-npcs"],
    "Gameplay":    ["gamemode", "difficulty", "max-players",
                    "pvp", "allow-flight", "force-gamemode",
                    "hardcore", "enable-command-block"],
    "Performance": ["view-distance", "max-tick-time",
                    "player-idle-timeout", "spawn-protection",
                    "entity-activation-range", "chunk-gc-period-in-ticks"],
}

# Keys where an ill-timed edit can corrupt a world or break a running server.
# Maps key → short danger note shown as a ⚠ tooltip on the key cell.
_DANGEROUS_KEYS: dict[str, str] = {
    "level-name": (
        "⚠ Changing this makes the server create/load a DIFFERENT world folder.\n"
        "Your existing world data is NOT deleted — you'd need to rename the folder\n"
        "on disk to match the new name before restarting.\n"
        "Safe to change between sessions when you know what you're doing."
    ),
    "level-seed": (
        "⚠ Seed only affects world generation for NEW chunks.\n"
        "Changing it mid-world will cause visible terrain seams at unexplored borders."
    ),
    "online-mode": (
        "⚠ Switching online-mode changes how player UUIDs are computed.\n"
        "Existing player data files (UUID-named .dat files) will become orphaned.\n"
        "Restart required."
    ),
    "white-list": (
        "Enables/disables the whitelist.\n"
        "Changing this takes effect after 'whitelist reload' or a server restart."
    ),
}

# Flat set of all important key names
_IMPORTANT_SET: set[str] = {k for keys in _IMPORTANT.values() for k in keys}


def _sort_key(k: str) -> tuple:
    """Sort: important keys first (in insertion order), then alphabetical."""
    for i, keys in enumerate(_IMPORTANT.values()):
        if k in keys:
            return (0, i, keys.index(k), k)
    return (1, 0, 0, k)


class ConfigTab(QWidget):
    """Editor for server.properties."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance:   ServerInstance | None = None
        self._props_path: Path | None           = None
        # Ordered map key → value (string, preserving original format)
        self._data: OrderedDict[str, str]       = OrderedDict()

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── Toolbar ──
        toolbar = QHBoxLayout()

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter properties…")
        self._filter_edit.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._filter_edit, stretch=1)

        self._save_btn = QPushButton("Save Changes")
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.clicked.connect(self._save)
        toolbar.addWidget(self._save_btn)

        reload_btn = QPushButton("↻")
        reload_btn.setFixedWidth(36)
        reload_btn.setToolTip("Reload from disk (discards unsaved edits)")
        reload_btn.clicked.connect(self._reload)
        toolbar.addWidget(reload_btn)

        layout.addLayout(toolbar)

        # ── Warning banner (shown when file is missing) ──
        self._warn = QLabel("")
        self._warn.setWordWrap(True)
        self._warn.setStyleSheet(
            f"color: {theme.YELLOW}; font-size: 12px; "
            f"background: {theme.SURFACE0}; border-radius: 4px; padding: 6px;"
        )
        self._warn.hide()
        layout.addWidget(self._warn)

        # ── Table ──
        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["Property", "Value"])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Only the Value column is editable
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                    | QAbstractItemView.EditTrigger.SelectedClicked)
        layout.addWidget(self._table, stretch=1)

        # ── Status line ──
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(self._status)

        # ── Legend ──
        legend = QLabel(
            f"<span style='color:{theme.ACCENT}'>■</span> Important properties   "
            f"<span style='color:{theme.GREEN}'>true</span> / "
            f"<span style='color:{theme.RED}'>false</span> highlighted   "
            "Double-click a value to edit"
        )
        legend.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(legend)

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, instance: ServerInstance) -> None:
        self._instance = instance
        self._reload()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _reload(self) -> None:
        if self._instance is None:
            return

        props_path = Path(self._instance.path) / "server.properties"
        self._props_path = props_path

        if not props_path.exists():
            self._table.setRowCount(0)
            self._warn.setText("⚠  server.properties not found — has the server been started at least once?")
            self._warn.show()
            self._status.setText("")
            return

        self._warn.hide()
        self._data = self._parse_props(props_path)
        self._populate_table()
        self._status.setText(
            f"  {len(self._data)} properties  ·  {props_path}"
        )
        self._set_status_neutral()

    def _parse_props(self, path: Path) -> OrderedDict[str, str]:
        """Read key=value pairs from server.properties, skip comments/blank lines."""
        result: OrderedDict[str, str] = OrderedDict()
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" in stripped:
                    k, _, v = stripped.partition("=")
                    result[k.strip()] = v   # value preserved verbatim
        except OSError:
            pass
        return result

    def _populate_table(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)

        sorted_keys = sorted(self._data.keys(), key=_sort_key)
        self._table.setRowCount(len(sorted_keys))

        for row, key in enumerate(sorted_keys):
            val = self._data[key]
            important = key in _IMPORTANT_SET

            # ── Key cell (read-only) ──
            key_item = QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            key_item.setForeground(QColor(theme.ACCENT if important else theme.SUBTEXT))
            # Tooltip: show which group this key belongs to, and danger warning if applicable
            group = next(
                (g for g, ks in _IMPORTANT.items() if key in ks), None
            )
            danger_note = _DANGEROUS_KEYS.get(key)
            if danger_note:
                key_item.setText(f"⚠ {key}")
                key_item.setToolTip(danger_note)
                key_item.setForeground(QColor(theme.YELLOW))
            elif group:
                key_item.setToolTip(f"Group: {group}")
            self._table.setItem(row, 0, key_item)

            # ── Value cell (editable) ──
            val_item = QTableWidgetItem(val)
            if val.lower() == "true":
                val_item.setForeground(QColor(theme.GREEN))
            elif val.lower() == "false":
                val_item.setForeground(QColor(theme.RED))
            elif important:
                val_item.setForeground(QColor(theme.TEXT))
            else:
                val_item.setForeground(QColor(theme.SUBTEXT))
            self._table.setItem(row, 1, val_item)
            self._table.setRowHeight(row, 30)

        self._table.setSortingEnabled(True)
        # Re-apply any active filter
        self._apply_filter(self._filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for row in range(self._table.rowCount()):
            key_item = self._table.item(row, 0)
            val_item = self._table.item(row, 1)
            match = (
                not text
                or (key_item and text in key_item.text().lower())
                or (val_item and text in val_item.text().lower())
            )
            self._table.setRowHidden(row, not match)

    def _save(self) -> None:
        if self._props_path is None or not self._props_path.exists():
            QMessageBox.warning(self, "No File", "server.properties not found.")
            return

        # Collect all current table values
        edited: dict[str, str] = {}
        for row in range(self._table.rowCount()):
            key_item = self._table.item(row, 0)
            val_item = self._table.item(row, 1)
            if key_item and val_item:
                # Strip the "⚠ " prefix we added to dangerous key display names
                raw_key = key_item.text().lstrip("⚠ ").strip()
                edited[raw_key] = val_item.text()

        # Warn if any dangerous keys changed
        changed_dangerous = [
            k for k in _DANGEROUS_KEYS
            if k in edited and edited[k] != self._data.get(k, "")
        ]
        if changed_dangerous:
            names = ", ".join(changed_dangerous)
            reply = QMessageBox.warning(
                self, "Confirm Save",
                f"You've changed: {names}\n\n"
                + "\n\n".join(_DANGEROUS_KEYS[k] for k in changed_dangerous)
                + "\n\nSave anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Read the original file to preserve comments and ordering
        try:
            original_lines = self._props_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError as exc:
            QMessageBox.critical(self, "Read Error", str(exc))
            return

        # Rewrite line-by-line: replace values, keep everything else
        new_lines: list[str] = []
        for line in original_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            k, _, _ = stripped.partition("=")
            k = k.strip()
            new_lines.append(f"{k}={edited.get(k, self._data.get(k, ''))}")

        try:
            tmp = self._props_path.with_suffix(".tmp")
            tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            tmp.replace(self._props_path)
        except OSError as exc:
            QMessageBox.critical(self, "Save Error", str(exc))
            return

        # Refresh internal state
        self._data = self._parse_props(self._props_path)
        self._set_status_saved()

    def _set_status_saved(self) -> None:
        self._status.setText(
            "✓  Saved — restart the server for changes to take effect"
        )
        self._status.setStyleSheet(
            f"color: {theme.GREEN}; font-size: 11px; font-weight: 600;"
        )

    def _set_status_neutral(self) -> None:
        self._status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
