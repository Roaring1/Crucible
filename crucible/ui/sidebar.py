"""
crucible/ui/sidebar.py

Left sidebar: lists registered server instances with explicit text statuses.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from PyQt6.QtCore import (
    QEasingCurve,
    QMimeData,
    QSize,
    Qt,
    QUrl,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QDesktopServices, QDrag, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from crucible import __version__

from ..data.backup_paths import DEFAULT_FLEET_BACKUP_DIR
from ..data.instance_model import ServerInstance
from . import theme


class InstanceItem(QListWidgetItem):
    """A single entry in the sidebar list."""

    STATUS_COLORS = {
        "running":      theme.GREEN,
        "stopped":      theme.SUBTEXT,
        "tmux_missing": theme.YELLOW,
        "starting":     theme.PROGRESS,
        "stopping":     theme.PROGRESS,
        "missing":      theme.RED,
        "unmanaged":    theme.YELLOW,
        "unknown":      theme.YELLOW,
        "checking":     theme.PROGRESS,
    }

    def __init__(self, instance: ServerInstance, status: str = "stopped"):
        super().__init__()
        self.instance = instance
        self._status  = status
        self._refresh()

    def _refresh(self) -> None:
        color = self.STATUS_COLORS.get(self._status, theme.SURFACE2)
        # The item widget renders the visible icon/name/status. Keep the
        # underlying QListWidgetItem visually empty so status refreshes cannot
        # resurrect a second text/icon layer underneath that widget.
        self.setText("")
        self.setIcon(QIcon())
        label = InstanceRowWidget.STATUS_LABELS.get(
            self._status, self._status.replace("_", " ").title()
        )
        self.setToolTip(
            f"{self.instance.name}\n"
            f"Status: {label}\n"
            f"Path: {self.instance.path}\n"
            f"Session: {self.instance.tmux_session}"
        )
        self.setData(
            Qt.ItemDataRole.AccessibleTextRole,
            f"{self.instance.name}, status {label}",
        )
        self.setData(
            Qt.ItemDataRole.AccessibleDescriptionRole,
            f"Server folder: {self.instance.path}. Session: {self.instance.tmux_session}.",
        )
        # Status is painted as readable text below the name. Color reinforces
        # the state but is never the only status cue.
        self.setData(Qt.ItemDataRole.UserRole, color)
        self.setData(Qt.ItemDataRole.UserRole + 1, self._status)
        self.setSizeHint(QSize(0, 64))

    def update_status(self, status: str) -> None:
        if self._status != status:
            self._status = status
            self._refresh()


class ServerIconLabel(QLabel):
    """A server emblem with a restrained one-shot selection motion."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scale = 1.0
        self._selection_animation: QVariantAnimation | None = None
        self.setFixedSize(44, 44)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_motion_enabled(self, enabled: bool) -> None:
        if enabled:
            return
        if self._selection_animation is not None:
            self._selection_animation.stop()
            self._selection_animation = None
        self._scale = 1.0
        self.update()

    def animate_selection(self) -> None:
        if not theme.MOTION_ENABLED or not self.isVisible():
            self._scale = 1.0
            self.update()
            return
        if self._selection_animation is not None:
            self._selection_animation.stop()
        animation = QVariantAnimation(self)
        animation.setDuration(175)
        animation.setStartValue(0.96)
        animation.setKeyValueAt(0.62, 1.055)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(self._set_scale)
        animation.finished.connect(
            lambda current=animation: self._finish_selection_animation(current)
        )
        self._selection_animation = animation
        animation.start()

    def _set_scale(self, value) -> None:
        self._scale = float(value)
        self.update()

    def _finish_selection_animation(self, animation) -> None:
        if animation is self._selection_animation:
            self._selection_animation = None
        self._scale = 1.0
        self.update()

    def paintEvent(self, event) -> None:
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        base_size = min(40, self.width(), self.height())
        width = base_size * self._scale
        height = base_size * self._scale
        # The tiny horizontal settle gives the icon a sense of arrival without
        # moving its row or making neighboring content jump. The 44px label
        # provides breathing room for the 40px artwork at the scale peak.
        offset_x = (1.0 - min(1.0, self._scale)) * 18.0
        x = (self.width() - width) / 2.0 + offset_x
        y = (self.height() - height) / 2.0
        from PyQt6.QtCore import QRectF
        painter.drawPixmap(
            QRectF(x, y, width, height), pixmap, QRectF(pixmap.rect())
        )


class InstanceRowWidget(QWidget):
    """Visible server identity row: image, name, and a text status chip."""

    STATUS_LABELS = MappingProxyType({
        "running": "Running",
        "stopped": "Stopped",
        "starting": "Starting",
        "stopping": "Stopping",
        "tmux_missing": "tmux unavailable",
        "missing": "Server files missing",
        "unmanaged": "Unmanaged process",
        "unknown": "Status unavailable",
        "checking": "Checking",
    })

    def __init__(self, instance: ServerInstance, status: str, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout

        from ..data.server_icons import resolve_icon_path

        self._instance = instance
        self._status = status
        self._selected = False
        self._selection_progress = 0.0
        self._selection_animation: QVariantAnimation | None = None
        self.setObjectName("ServerRailRow")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet(
            "QWidget#ServerRailRow { background: transparent; border: none; } "
            "QLabel { background: transparent; border: none; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 8, 7)
        layout.setSpacing(10)

        self._icon = ServerIconLabel()
        self._icon.setAccessibleName(f"{instance.name} server artwork")
        self._icon.setAccessibleDescription(
            "The selected server's configured artwork."
        )
        self._icon.setPixmap(QIcon(str(resolve_icon_path(instance))).pixmap(40, 40))
        layout.addWidget(self._icon)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(3)
        self._name = QLabel(instance.name)
        self._name.setObjectName("ServerRailName")
        self._name.setWordWrap(False)
        self._name.setMinimumWidth(0)
        self._name.setStyleSheet(
            f"background: transparent; border: none; "
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 600;"
        )
        self._name.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        text.addWidget(self._name)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(0)

        self._status_label = QLabel()
        self._status_label.setObjectName("ServerRailStatus")
        self._status_label.setWordWrap(False)
        self._status_label.setMinimumWidth(0)
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        text.addLayout(status_row)
        text.addStretch()
        layout.addLayout(text, 1)
        self.setMinimumHeight(60)
        self.set_status(status)

    def set_selected(self, selected: bool) -> None:
        selected = bool(selected)
        if self._selected == selected:
            return
        self._selected = selected
        target = 1.0 if selected else 0.0
        if not theme.MOTION_ENABLED or not self.isVisible():
            self._selection_progress = target
            self.update()
            if selected:
                self._icon.animate_selection()
            return
        if self._selection_animation is not None:
            self._selection_animation.stop()
        animation = QVariantAnimation(self)
        animation.setDuration(155)
        animation.setStartValue(self._selection_progress)
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(self._set_selection_progress)
        animation.finished.connect(
            lambda current=animation: self._finish_selection_animation(current)
        )
        self._selection_animation = animation
        animation.start()
        if selected:
            self._icon.animate_selection()

    def set_motion_enabled(self, enabled: bool) -> None:
        self._icon.set_motion_enabled(enabled)
        if enabled:
            return
        if self._selection_animation is not None:
            self._selection_animation.stop()
            self._selection_animation = None
        self._selection_progress = 1.0 if self._selected else 0.0
        self.update()

    def _set_selection_progress(self, value) -> None:
        self._selection_progress = float(value)
        self.update()

    def _finish_selection_animation(self, animation) -> None:
        if animation is self._selection_animation:
            self._selection_animation = None
        self._selection_progress = 1.0 if self._selected else 0.0
        self.update()

    def paintEvent(self, event) -> None:
        if self._selection_progress <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(theme.SELECTED)
        fill.setAlpha(round(250 * self._selection_progress))
        edge = QColor(theme.ACCENT)
        edge.setAlpha(round(58 * self._selection_progress))
        painter.setBrush(fill)
        painter.setPen(QPen(edge, 1.0))
        from PyQt6.QtCore import QRectF
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(2, 1, -2, -1), 7, 7
        )

    def _fit_text(self) -> None:
        from PyQt6.QtGui import QFontMetrics

        available = max(36, self.width() - 74)
        self._name.setText(
            QFontMetrics(self._name.font()).elidedText(
                self._instance.name, Qt.TextElideMode.ElideRight, available
            )
        )
        status_text = self.STATUS_LABELS.get(
            self._status, self._status.replace("_", " ").title()
        )
        self._status_label.setText(
            QFontMetrics(self._status_label.font()).elidedText(
                status_text, Qt.TextElideMode.ElideRight, available
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_text()

    def set_status(self, status: str) -> None:
        self._status = status
        color = InstanceItem.STATUS_COLORS.get(status, theme.SURFACE2)
        label = self.STATUS_LABELS.get(status, status.replace("_", " ").title())
        self._status_label.setText(label)
        self._status_label.setStyleSheet(
            f"background: transparent; border: none; "
            f"color: {color}; font-size: 11px; font-weight: 500;"
        )
        self._status_label.setAccessibleName(f"Server status: {label}")
        self.setAccessibleName(f"{self._instance.name}, status {label}")
        self.setAccessibleDescription(
            f"Server folder: {self._instance.path}. "
            f"Status: {label}. Select this row to open the server workspace."
        )
        if self.width() > 0:
            self._fit_text()
        self.setToolTip(f"{self._instance.name}\nStatus: {label}\nPath: {self._instance.path}")

    def refresh(self) -> None:
        from ..data.server_icons import resolve_icon_path

        self._icon.setPixmap(QIcon(str(resolve_icon_path(self._instance))).pixmap(40, 40))
        self.set_status(self._status)


class SidebarList(QListWidget):
    """List of server rows with drag/drop ordering and external import support.

    Each row is a normal QListWidgetItem with a transparent, non-interactive
    widget. This keeps selection, keyboard navigation, dragging and reorder
    behavior native while making the two-line identity/status layout reliable.
    """

    # Emitted after an internal reorder, with the new top-to-bottom id order.
    order_changed = pyqtSignal(list)   # list[str] of instance ids
    # Emitted when external file paths are dropped onto the list.
    paths_dropped = pyqtSignal(list)   # list[str] of local paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUniformItemSizes(True)
        self.setSpacing(2)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        mime.setData("application/x-crucible-instance", str(inst.id).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)

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
        if self._is_internal(event):
            super().dropEvent(event)
            order = []
            for row in range(self.count()):
                inst = getattr(self.item(row), "instance", None)
                if inst is not None:
                    order.append(inst.id)
            self.order_changed.emit(order)
            return
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
            if paths:
                event.acceptProposedAction()
                self.paths_dropped.emit(paths)
                return
        event.ignore()


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
    appearance_requested = pyqtSignal(object)  # ServerInstance
    shortcuts_requested = pyqtSignal(object)  # ServerInstance
    # Context menu actions
    start_requested   = pyqtSignal(object)  # ServerInstance
    stop_requested    = pyqtSignal(object)  # ServerInstance
    remove_requested  = pyqtSignal(object)  # ServerInstance
    rename_requested = pyqtSignal(object)
    inspect_logs_requested = pyqtSignal(object)  # ServerInstance
    fix_loading_requested = pyqtSignal(object)  # ServerInstance
    # Drag & drop
    order_changed     = pyqtSignal(list)    # list[str] of instance ids (new order)
    paths_dropped     = pyqtSignal(list)    # list[str] of dropped local paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumWidth(160)
        # No setMaximumWidth -- let the splitter handle sizing freely

        self._items: dict[str, InstanceItem] = {}  # id → item
        self._row_widgets: dict[str, InstanceRowWidget] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Title bar
        title = QLabel("Servers")
        title.setObjectName("SidebarTitle")
        layout.addWidget(title)

        # Instance list
        self._list = SidebarList()
        self._list.setAccessibleName("Registered servers")
        self._list.setAccessibleDescription(
            "Select a server to open its Console workspace. Use F2 to rename the selected server."
        )
        self._list.currentItemChanged.connect(self._on_selection_changed)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        from PyQt6.QtGui import QKeySequence, QShortcut
        self._rename_shortcut = QShortcut(QKeySequence("F2"), self)
        self._rename_shortcut.activated.connect(lambda: self.rename_requested.emit(self.selected_instance()) if self.selected_instance() else None)
        # Forward drag & drop signals upward.
        self._list.order_changed.connect(self.order_changed)
        self._list.paths_dropped.connect(self.paths_dropped)
        self._list.setToolTip(
            "Drag to reorder. Drop a Prism/.mrpack/.zip here to import. "
            "Drag a server out to copy its folder.")
        layout.addWidget(self._list, stretch=1)

        # Appearance is a first-class action, not an item hidden in RMB.
        self._appearance_btn = QPushButton("Server appearance…")
        self._appearance_btn.setAccessibleName("Change selected server appearance")
        self._appearance_btn.setMinimumHeight(34)
        self._appearance_btn.setEnabled(False)
        self._appearance_btn.setToolTip(
            "Choose the image shown beside the selected server. This changes Crucible's list only."
        )
        self._appearance_btn.clicked.connect(
            lambda: self.appearance_requested.emit(self.selected_instance())
            if self.selected_instance() is not None else None
        )
        layout.addWidget(self._appearance_btn)

        # Add button
        self._add_btn = QPushButton("Add server")
        self._add_btn.setObjectName("SidebarAddButton")
        self._add_btn.setAccessibleName("Add server")
        self._add_btn.setMinimumHeight(36)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setToolTip("Create a server, install a modpack, or import existing files")
        self._add_btn.clicked.connect(self.add_requested)
        layout.addWidget(self._add_btn)

        # Renaming remains available from the server context menu and F2.
        # Keep the main sidebar focused on the server list and its primary action.

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
        self._row_widgets.clear()

        for inst in instances:
            status = status_map.get(inst.id, "stopped")
            self._add_row(inst, status)

        self._list.blockSignals(False)

        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _add_row(self, instance: ServerInstance, status: str) -> InstanceItem:
        item = InstanceItem(instance, status)
        self._list.addItem(item)
        # QListWidget still paints the QListWidgetItem beneath a transparent
        # item widget. Clear the item's own icon/text so there is only one
        # visual identity row rather than two overlapping renderers.
        item.setText("")
        item.setIcon(QIcon())
        row_widget = InstanceRowWidget(instance, status)
        self._row_widgets[instance.id] = row_widget
        self._list.setItemWidget(item, row_widget)
        self._items[instance.id] = item
        return item

    def update_status(self, instance_id: str, status: str) -> None:
        """Update the readable status label for one instance without rebuilding the list."""
        if item := self._items.get(instance_id):
            item.update_status(status)
            widget = self._row_widgets.get(instance_id)
            if widget is not None:
                widget.set_status(status)
            self._list.update()

    def refresh_instance_icon(self, instance_id: str) -> None:
        """Re-resolve a row's image after a custom/pack/default choice changes."""
        if item := self._items.get(instance_id):
            item._refresh()
            widget = self._row_widgets.get(instance_id)
            if widget is not None:
                widget.refresh()
            self._list.update()

    def set_motion_enabled(self, enabled: bool) -> None:
        for row in self._row_widgets.values():
            row.set_motion_enabled(enabled)

    def update_all_statuses(self, status_map: dict[str, str]) -> None:
        for iid, status in status_map.items():
            self.update_status(iid, status)

    def add_instance(self, inst: ServerInstance, status: str = "stopped") -> None:
        self._add_row(inst, status)

    def remove_instance(self, instance_id: str) -> None:
        self._row_widgets.pop(instance_id, None)
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
        selected = current.instance if isinstance(current, InstanceItem) else None
        previous = _prev.instance if isinstance(_prev, InstanceItem) else None
        if previous is not None:
            if row := self._row_widgets.get(previous.id):
                row.set_selected(False)
        if selected is not None:
            if row := self._row_widgets.get(selected.id):
                row.set_selected(True)
        self._appearance_btn.setEnabled(selected is not None)
        if selected is not None:
            self.instance_selected.emit(selected)

    def _on_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if not isinstance(item, InstanceItem):
            return
        inst   = item.instance
        status = item.data(Qt.ItemDataRole.UserRole + 1)

        menu = QMenu(self)

        title_act = menu.addAction(inst.name)
        title_act.setEnabled(False)
        menu.addSeparator()

        icon_root = Path(__file__).resolve().parent / "assets" / "menu-icons"

        def action_icon(name: str) -> QIcon:
            return QIcon(str(icon_root / f"{name}.svg"))

        rename_act = menu.addAction(action_icon("edit"), "Rename server…")
        menu.addSection("LIFECYCLE")
        start_act = menu.addAction(action_icon("play"), "Start server")
        stop_act = menu.addAction(action_icon("stop"), "Stop server")
        menu.addSection("FILES & LOCATION")
        open_folder_act = menu.addAction(action_icon("folder"), "Open server folder")
        open_backups_act = menu.addAction(action_icon("archive"), "Open fleet backups folder")
        copy_path_act = menu.addAction(action_icon("copy"), "Copy server path")
        shortcut_act = menu.addAction("Create shortcuts…")
        menu.addSection("DIAGNOSTICS")
        inspect_act = menu.addAction(action_icon("search-log"), "Inspect server logs…")
        repair_act = menu.addAction(action_icon("repair"), "Check loading repair…")
        menu.addSection("REMOVE")
        remove_act = menu.addAction(action_icon("remove"), "Remove server…")
        remove_act.setToolTip(
            "Remove this instance from Crucible or permanently delete its server files."
        )

        running = status in ("running", "starting")
        stopped = status == "stopped"
        start_act.setEnabled(stopped)
        stop_act.setEnabled(running)

        chosen = menu.exec(self._list.mapToGlobal(pos))
        if chosen == rename_act:
            self.rename_requested.emit(inst)
        elif chosen == start_act:
            self.start_requested.emit(inst)
        elif chosen == stop_act:
            self.stop_requested.emit(inst)
        elif chosen == open_folder_act:
            self._open_folder(inst.path)
        elif chosen == open_backups_act:
            self._open_backups_folder(inst)
        elif chosen == copy_path_act:
            QApplication.clipboard().setText(inst.path)
        elif chosen == shortcut_act:
            self.shortcuts_requested.emit(inst)
        elif chosen == inspect_act:
            self.inspect_logs_requested.emit(inst)
        elif chosen == repair_act:
            self.fix_loading_requested.emit(inst)
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

    def _open_backups_folder(self, _inst: ServerInstance) -> None:
        # All server snapshots share one global fleet store and two-generation
        # retention policy. Do not send owners back to the retired per-server
        # ZIP folders.
        DEFAULT_FLEET_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self._open_folder(str(DEFAULT_FLEET_BACKUP_DIR))
