"""
crucible/ui/tabs/info_tab.py

Info tab: read-only summary of the selected server instance.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout,
    QLabel, QFrame, QScrollArea,
)

from ...data.instance_model import ServerInstance
from .. import theme


def _field(label: str, value: str, value_color: str = theme.TEXT) -> tuple[QLabel, QLabel]:
    lbl = QLabel(label)
    lbl.setStyleSheet(
        f"color: {theme.SUBTEXT}; font-size: 11px; font-weight: 600; "
        f"letter-spacing: 0.5px;"
    )
    val = QLabel(value or "—")
    val.setWordWrap(True)
    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    val.setStyleSheet(f"color: {value_color}; font-size: 13px;")
    return lbl, val


class InfoTab(QWidget):
    """Read-only metadata display for one server instance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)

        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(20, 16, 20, 16)
        self._layout.setSpacing(20)

        # Placeholder until load() is called
        self._placeholder = QLabel("No instance selected.")
        self._placeholder.setStyleSheet(f"color: {theme.SURFACE2};")
        self._layout.addWidget(self._placeholder)
        self._layout.addStretch()

    def load(self, instance: ServerInstance, status: str = "stopped") -> None:
        """Rebuild the info display for the given instance."""
        # Clear existing widgets
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        grid = QGridLayout()
        grid.setColumnMinimumWidth(0, 140)
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(14)
        grid.setHorizontalSpacing(16)

        def add_row(row: int, label: str, value: str, color: str = theme.TEXT):
            lbl, val = _field(label, value, color)
            grid.addWidget(lbl, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(val, row, 1, Qt.AlignmentFlag.AlignTop)

        status_color = {
            "running":      theme.GREEN,
            "stopped":      theme.SUBTEXT,
            "tmux_missing": theme.YELLOW,
        }.get(status, theme.SUBTEXT)

        problems    = instance.validate()
        script      = instance.get_startscript()
        log         = instance.get_log_path()
        worlds      = instance.get_world_names()
        mod_count   = instance.get_mod_count()
        bundled     = instance.get_bundled_jars()

        # Build a human-readable log label that flags the FML log situation
        if log is None:
            log_display = "not found"
            log_color   = theme.RED
        elif log.name == "fml-server-latest.log":
            log_display = str(log) + "  (FML primary log)"
            log_color   = theme.TEXT
        else:
            log_display = str(log)
            log_color   = theme.TEXT

        add_row(0,  "NAME",         instance.name)
        add_row(1,  "ID",           instance.short_id(), theme.SURFACE2)
        add_row(2,  "VERSION",      instance.version)
        add_row(3,  "STATUS",       status.upper(), status_color)
        add_row(4,  "TMUX SESSION", instance.tmux_session, theme.ACCENT)
        add_row(5,  "PATH",         instance.path)
        add_row(6,  "START SCRIPT", str(script) if script else "NOT FOUND",
                    theme.TEXT if script else theme.RED)
        add_row(7,  "MODS",
                    f"{mod_count} enabled"
                    + (f"  +  {len(bundled)} bundled (unmanaged)" if bundled else ""))
        add_row(8,  "WORLDS",       ", ".join(worlds) if worlds else "none found")
        add_row(9,  "LOG",          log_display, log_color)
        add_row(10, "JAVA ARGS",    instance.java_args)
        add_row(11, "CREATED",      instance.created_at[:19].replace("T", "  "))
        add_row(12, "LAST STARTED",
                    instance.last_started[:19].replace("T", "  ") if instance.last_started
                    else "never (via Crucible)")

        self._layout.addLayout(grid)

        # Validation warnings
        if problems:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"color: {theme.SURFACE1};")
            self._layout.addWidget(sep)

            warn_title = QLabel("⚠  Validation Issues")
            warn_title.setStyleSheet(
                f"color: {theme.YELLOW}; font-size: 12px; font-weight: 600;"
            )
            self._layout.addWidget(warn_title)

            for p in problems:
                w = QLabel(f"  · {p}")
                w.setStyleSheet(f"color: {theme.YELLOW}; font-size: 12px;")
                w.setWordWrap(True)
                self._layout.addWidget(w)

        self._layout.addStretch()
