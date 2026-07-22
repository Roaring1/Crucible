"""
crucible/ui/sidebar.py

Left sidebar: lists registered server instances with live status dots.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QSize, QUrl, QMimeData, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QDrag, QDesktopServices
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMenu, QAbstractItemView, QApplication,
)

from ..data.instance_model import ServerInstance
from ..data.backup_manager import BackupManager
from . import theme
from crucible import __version__


class InstanceItem(QListWidgetItem):
    """A single entry in the sidebar list."""

    DOT_COLORS = {
        "running":      theme.GREEN,
        "stopped":      theme.SURFACE2,
        "tmux_missing": theme.YELLOW,
        "starting":     theme.ORANGE,
        "stopping":     theme.ORANGE,
        "missing":      theme.RED,
        "unmanaged":    theme.YELLOW,
        "unknown":      theme.YELLOW,
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
    """QListWidget that paints a colored status dot before each item name.

    Drag & drop behaviour
    ─────────────────────
    * Internal drag  → reorder the server list (persisted by the window).
    * External drop  → a dropped Prism instance / .mrpack / .zip / server
      folder is imported (handled by the window).
    * Drag *out*     → each row exposes its server folder as a file URL
      (text/uri-list), so a server can be dragged into a file manager or
      another launcher's import flow.
    """

    # Emitted after an internal reorder, with the new top-to-bottom id order.
    order_changed = pyqtSignal(list)   # list[str] of instance ids
    # Emitted when external file paths are dropped onto the list.
    paths_dropped = pyqtSignal(list)   # list[str] of local paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Enable drag-to-reorder + accept external file drops simultaneously.
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    # Drag-out: expose the selected server's folder as a file URL so it can be
    # dropped into a file manager / another launcher.
    def startDrag(self, supportedActions) -> None:
        item = self.currentItem()
        inst = getattr(item, "instance", None)
        if inst is None:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(inst.path))])
        mime.setText(str(inst.path))
        # Internal-move marker so dropEvent can tell self-drags apart.
        mime.setData("application/x-crucible-instance",
                     str(inst.id).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                  Qt.DropAction.MoveAction)

    def _is_internal(self, event) -> bool:
        return (event.source() is self
                or event.mimeData().hasFormat("application/x-crucible-instance"))

    def dragEnterEvent(self, event) -> None:
        if self._is_internal(event) or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._is_internal(event) or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        # Internal reorder: let Qt move the row, then report the new order.
        if self._is_internal(event):
            super().dropEvent(event)
            order = []
            for row in range(self.count()):
                it = self.item(row)
                inst = getattr(it, "instance", None)
                if inst is not None:
                    order.append(inst.id)
            self.order_changed.emit(order)
            return
        # External file drop → import.
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls()
                     if u.toLocalFile()]
            if paths:
                event.acceptProposedAction()
                self.paths_dropped.emit(paths)
                return
        event.ignore()

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
    fix_loading_requested = pyqtSignal(object)  # ServerInstance
    export_requested  = pyqtSignal(object)  # ServerInstance
    # Drag & drop
    order_changed     = pyqtSignal(list)    # list[str] of instance ids (new order)
    paths_dropped     = pyqtSignal(list)    # list[str] of dropped local paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumWidth(160)
        # No setMaximumWidth -- let the splitter handle sizing freely

        self._items: dict[str, InstanceItem] = {}  # id → item

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Title bar
        title = QLabel("SERVERS")
        title.setObjectName("SidebarTitle")
        layout.addWidget(title)

        # Instance list
        self._list = SidebarList()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        # Forward drag & drop signals upward.
        self._list.order_changed.connect(self.order_changed)
        self._list.paths_dropped.connect(self.paths_dropped)
        self._list.setToolTip(
            "Drag to reorder. Drop a Prism/.mrpack/.zip here to import. "
            "Drag a server out to copy its folder.")
        layout.addWidget(self._list, stretch=1)

        # Add button
        add_btn = QPushButton("＋  Add Server")
        add_btn.setObjectName("SidebarAddButton")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self.add_requested)
        layout.addWidget(add_btn)

        # Small version footer
        footer = QLabel(f"Crucible v{__version__}")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet(f"color: {theme.SURFACE2}; font-size: 10px; padding: 4px;")
        layout.addWidget(footer)

    # Population

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

    def status_for(self, instance_id: str) -> str:
        item = self._items.get(instance_id)
        if item is None:
            return "unknown"
        return str(item.data(Qt.ItemDataRole.UserRole + 1) or "unknown")

    def selected_instance(self) -> ServerInstance | None:
        item = self._list.currentItem()
        if isinstance(item, InstanceItem):
            return item.instance
        return None

    # Events

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
            "QMenu { background: #1e1e2e; color: #cdd6f4; border: 1px solid #45475a; "
            "border-radius: 6px; padding: 4px; }"
            "QMenu::item { padding: 6px 20px 6px 12px; border-radius: 4px; }"
            "QMenu::item:selected { background: #313244; }"
            "QMenu::separator { height: 1px; background: #45475a; margin: 3px 8px; }"
        )

        title_act = menu.addAction(f"  {inst.name}")
        title_act.setEnabled(False)
        menu.addSeparator()

        start_act   = menu.addAction("▶  Start")
        stop_act    = menu.addAction("■  Stop")
        restart_act = menu.addAction("↺  Restart")
        menu.addSeparator()
        open_folder_act  = menu.addAction("📂  Open server folder")
        open_backups_act = menu.addAction("💾  Open backups folder")
        copy_path_act    = menu.addAction("📋  Copy server path")
        menu.addSeparator()
        fixload_act = menu.addAction("🩺  Fix loading errors…")
        export_act  = menu.addAction("📤  Export for Prism…")
        menu.addSeparator()
        remove_act  = menu.addAction("🗑  Remove from Crucible…")

        running = (status == "running")
        stopped = (status == "stopped")
        start_act.setEnabled(stopped)
        stop_act.setEnabled(running)
        restart_act.setEnabled(running or stopped)

        chosen = menu.exec(self._list.mapToGlobal(pos))
        if chosen == start_act:
            self.start_requested.emit(inst)
        elif chosen == stop_act:
            self.stop_requested.emit(inst)
        elif chosen == restart_act:
            self.restart_requested.emit(inst)
        elif chosen == open_folder_act:
            self._open_folder(inst.path)
        elif chosen == open_backups_act:
            self._open_backups_folder(inst)
        elif chosen == copy_path_act:
            QApplication.clipboard().setText(inst.path)
        elif chosen == fixload_act:
            self.fix_loading_requested.emit(inst)
        elif chosen == export_act:
            self.export_requested.emit(inst)
        elif chosen == remove_act:
            self.remove_requested.emit(inst)

    @staticmethod
    def _open_folder(path: str) -> None:
        """Open a folder in the OS file manager. Silently does nothing if the
        folder no longer exists on disk (e.g. a server moved/deleted outside
        Crucible) rather than raising into the RMB menu handler."""
        from pathlib import Path
        p = Path(path)
        if p.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _open_backups_folder(self, inst: ServerInstance) -> None:
        # BackupManager creates this directory on first use if it does not
        # already exist, so there is always something to open here.
        backup_dir = BackupManager(inst).backup_dir()
        self._open_folder(str(backup_dir))
