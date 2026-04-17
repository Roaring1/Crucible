"""
crucible/ui/sidebar.py

Left sidebar: lists registered server instances with live status dots.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QHBoxLayout, QMenu,
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
        # Four leading spaces give the dot enough room (dot is drawn at left+14, ~10px wide)
        self.setText(f"    {self.instance.name}")
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
    # Context menu actions
    start_requested   = pyqtSignal(object)  # ServerInstance
    stop_requested    = pyqtSignal(object)  # ServerInstance
    restart_requested = pyqtSignal(object)  # ServerInstance
    remove_requested  = pyqtSignal(object)  # ServerInstance

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
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
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

    def _on_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if not isinstance(item, InstanceItem):
            return
        inst   = item.instance
        status = item.data(Qt.ItemDataRole.UserRole + 1)

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: #1e1e2e; color: #cdd6f4; border: 1px solid #45475a; "
            f"border-radius: 6px; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 20px 6px 12px; border-radius: 4px; }}"
            f"QMenu::item:selected {{ background: #313244; }}"
            f"QMenu::separator {{ height: 1px; background: #45475a; margin: 3px 8px; }}"
        )

        title_act = menu.addAction(f"  {inst.name}")
        title_act.setEnabled(False)
        menu.addSeparator()

        start_act   = menu.addAction("▶  Start")
        stop_act    = menu.addAction("■  Stop")
        restart_act = menu.addAction("↺  Restart")
        menu.addSeparator()
        remove_act  = menu.addAction("🗑  Remove from Crucible…")

        running = (status == "running")
        start_act.setEnabled(not running)
        stop_act.setEnabled(running)

        chosen = menu.exec(self._list.mapToGlobal(pos))
        if chosen == start_act:
            self.start_requested.emit(inst)
        elif chosen == stop_act:
            self.stop_requested.emit(inst)
        elif chosen == restart_act:
            self.restart_requested.emit(inst)
        elif chosen == remove_act:
            self.remove_requested.emit(inst)
