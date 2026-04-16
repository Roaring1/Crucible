"""
crucible/ui/sidebar.py

Left sidebar: lists registered server instances with live status dots.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QHBoxLayout,
)

from ..data.instance_model import ServerInstance
from . import theme


class InstanceItem(QListWidgetItem):
    """A single entry in the sidebar list."""

    DOT_COLORS = {
        "running":      theme.GREEN,
        "stopped":      theme.SURFACE2,
        "tmux_missing": theme.YELLOW,
        "starting":     theme.ORANGE,
        "unknown":      theme.SURFACE2,
    }

    def __init__(self, instance: ServerInstance, status: str = "stopped"):
        super().__init__()
        self.instance = instance
        self._status  = status
        self._refresh()

    def _refresh(self) -> None:
        color = self.DOT_COLORS.get(self._status, theme.SURFACE2)
        # Build display text: colored dot + name
        self.setText(f"  {self.instance.name}")
        self.setToolTip(
            f"{self.instance.name}\n"
            f"Path: {self.instance.path}\n"
            f"Session: {self.instance.tmux_session}\n"
            f"Status: {self._status}"
        )
        # Store color so the delegate can paint the dot
        self.setData(Qt.ItemDataRole.UserRole, color)
        self.setData(Qt.ItemDataRole.UserRole + 1, self._status)
        self.setSizeHint(QSize(0, 44))

    def update_status(self, status: str) -> None:
        if self._status != status:
            self._status = status
            self._refresh()


class SidebarList(QListWidget):
    """QListWidget that paints a colored status dot before each item name."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def drawRow(self, painter: QPainter, option, index) -> None:
        super().drawRow(painter, option, index)
        # Paint the status dot on top of the item
        color_hex = index.data(Qt.ItemDataRole.UserRole)
        if color_hex:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QColor(color_hex))
            painter.setPen(Qt.PenStyle.NoPen)
            rect = self.visualRect(index)
            cx   = rect.left() + 14
            cy   = rect.center().y()
            painter.drawEllipse(cx - 5, cy - 5, 10, 10)
            painter.restore()


class Sidebar(QWidget):
    """
    Left sidebar widget.

    Signals
    ───────
    instance_selected(ServerInstance)  — user clicked an item
    add_requested()                    — user clicked "+ Add Server"
    """

    instance_selected = pyqtSignal(object)  # ServerInstance
    add_requested     = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumWidth(220)
        self.setMaximumWidth(300)

        self._items: dict[str, InstanceItem] = {}  # id → item

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Title bar ──
        title = QLabel("SERVERS")
        title.setObjectName("SidebarTitle")
        layout.addWidget(title)

        # ── Instance list ──
        self._list = SidebarList()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list, stretch=1)

        # ── Add button ──
        add_btn = QPushButton("＋  Add Server")
        add_btn.setObjectName("SidebarAddButton")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self.add_requested)
        layout.addWidget(add_btn)

        # Small version footer
        footer = QLabel("Crucible v0.3.2")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 10px; padding: 4px;")
        layout.addWidget(footer)

    # ── Population ────────────────────────────────────────────────────────────

    def populate(
        self,
        instances: list[ServerInstance],
        status_map: dict[str, str],
    ) -> None:
        """Replace the entire list with the given instances."""
        self._list.blockSignals(True)
        self._list.clear()
        self._items.clear()

        for inst in instances:
            status = status_map.get(inst.id, "stopped")
            item   = InstanceItem(inst, status)
            self._list.addItem(item)
            self._items[inst.id] = item

        self._list.blockSignals(False)

        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def update_status(self, instance_id: str, status: str) -> None:
        """Update the status dot for one instance without rebuilding the list."""
        if item := self._items.get(instance_id):
            item.update_status(status)
            self._list.update()

    def update_all_statuses(self, status_map: dict[str, str]) -> None:
        for iid, status in status_map.items():
            self.update_status(iid, status)

    def add_instance(self, inst: ServerInstance, status: str = "stopped") -> None:
        item = InstanceItem(inst, status)
        self._list.addItem(item)
        self._items[inst.id] = item

    def remove_instance(self, instance_id: str) -> None:
        if item := self._items.pop(instance_id, None):
            row = self._list.row(item)
            self._list.takeItem(row)

    def select_by_id(self, instance_id: str) -> None:
        if item := self._items.get(instance_id):
            self._list.setCurrentItem(item)

    def selected_instance(self) -> ServerInstance | None:
        item = self._list.currentItem()
        if isinstance(item, InstanceItem):
            return item.instance
        return None

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_selection_changed(
        self, current: QListWidgetItem, _prev: QListWidgetItem
    ) -> None:
        if isinstance(current, InstanceItem):
            self.instance_selected.emit(current.instance)
