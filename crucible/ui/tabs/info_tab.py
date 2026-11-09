"""
crucible/ui/tabs/info_tab.py

Info tab: read-only summary of the selected server instance.
"""

from __future__ import annotations

import weakref

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...data.instance_model import ServerInstance
from .. import theme
from ..thread_lifecycle import connect_thread_cleanup
from .system_tab import SystemTab


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


class _DiagnosticsInventoryWorker(QObject):
    """Build the read-only Diagnostics snapshot outside the GUI thread."""

    completed = pyqtSignal(int, object)

    def __init__(self, generation: int, instance: ServerInstance):
        super().__init__()
        self._generation = generation
        self._instance = instance

    def run(self) -> None:
        try:
            instance = self._instance
            bundled = instance.get_bundled_jars()
            payload = {
                "bundled": bundled,
                "problems": instance.validate(bundled_jars=bundled),
                "script": instance.get_startscript(),
                "log": instance.get_log_path(),
                "worlds": instance.get_world_names(),
                "mod_count": instance.get_mod_count(),
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001 - worker boundary reports filesystem errors
            payload = {"error": str(exc)}
        self.completed.emit(self._generation, payload)
        self.thread().quit()


_ACTIVE_DIAGNOSTICS_WORKERS: dict[QThread, _DiagnosticsInventoryWorker] = {}


class InfoTab(QWidget):
    """Server diagnostics, metadata, and live performance for one instance."""

    fix_loading_requested = pyqtSignal(object)
    inspect_logs_requested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
        self._status = "stopped"
        self._generation = 0
        self._thread: QThread | None = None
        self._worker: _DiagnosticsInventoryWorker | None = None
        self._refresh_pending = False
        self._lifecycle_state = {"destroyed": False}
        lifecycle_state = self._lifecycle_state
        self.destroyed.connect(
            lambda _obj=None, state=lifecycle_state: state.__setitem__("destroyed", True)
        )
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 0)
        outer.setSpacing(8)

        heading = QLabel("Diagnostics")
        heading.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;"
        )
        outer.addWidget(heading)

        health_group = QGroupBox("Health")
        health_layout = QHBoxLayout(health_group)
        health_layout.setContentsMargins(12, 22, 12, 10)
        self._health_label = QLabel("Select a server to inspect its current state.")
        self._health_label.setWordWrap(True)
        self._health_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 600;"
        )
        health_layout.addWidget(self._health_label)
        outer.addWidget(health_group)

        action_groups = QHBoxLayout()
        action_groups.setSpacing(10)

        logs_group = QGroupBox("Logs and crashes")
        logs_layout = QVBoxLayout(logs_group)
        logs_layout.setContentsMargins(12, 22, 12, 10)
        logs_hint = QLabel(
            "Read the latest server log or crash report. This is inspection only."
        )
        logs_hint.setWordWrap(True)
        logs_hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        logs_layout.addWidget(logs_hint)

        self._inspect_logs_btn = QPushButton("Inspect server logs and crashes")
        self._inspect_logs_btn.setToolTip(
            "Review the latest available server log and crash report. Does not change server files."
        )
        self._inspect_logs_btn.setAccessibleName("Inspect server logs and crash reports")
        self._inspect_logs_btn.setEnabled(False)
        self._inspect_logs_btn.clicked.connect(
            lambda: self.inspect_logs_requested.emit(self._instance)
            if self._instance is not None else None
        )
        logs_layout.addWidget(self._inspect_logs_btn)
        action_groups.addWidget(logs_group, 1)

        repair_group = QGroupBox("Fix loading errors")
        repair_layout = QVBoxLayout(repair_group)
        repair_layout.setContentsMargins(12, 22, 12, 10)
        repair_hint = QLabel(
            "Check a startup failure. Crucible shows the proposed mod quarantine "
            "and asks before changing any files."
        )
        repair_hint.setWordWrap(True)
        repair_hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        repair_layout.addWidget(repair_hint)

        self._fix_loading_btn = QPushButton("Check loading repair…")
        self._fix_loading_btn.setToolTip(
            "Diagnose startup failures. Any proposed mod quarantine is shown for approval before files change."
        )
        self._fix_loading_btn.setAccessibleName("Check for safe server loading repairs")
        self._fix_loading_btn.setEnabled(False)
        self._fix_loading_btn.clicked.connect(
            lambda: self.fix_loading_requested.emit(self._instance)
            if self._instance is not None else None
        )
        repair_layout.addWidget(self._fix_loading_btn)
        action_groups.addWidget(repair_group, 1)
        outer.addLayout(action_groups)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # QScrollArea's viewport and child QWidget otherwise use the platform
        # palette (white on some KDE/Qt styles), producing a bright rectangle
        # inside Crucible's graphite Diagnostics page.
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.BASE}; border: none; }} "
            f"QScrollArea > QWidget > QWidget {{ background: {theme.BASE}; }}"
        )
        scroll.viewport().setStyleSheet(f"background: {theme.BASE};")
        outer.addWidget(scroll)

        container = QWidget()
        container.setAutoFillBackground(True)
        container.setStyleSheet(f"background: {theme.BASE};")
        scroll.setWidget(container)

        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(20, 16, 20, 16)
        self._layout.setSpacing(20)

        # Placeholder until load() is called
        self._placeholder = QLabel("No instance selected.")
        self._placeholder.setStyleSheet(f"color: {theme.SURFACE2};")
        self._layout.addWidget(self._placeholder)
        self._layout.addStretch()

        # Live CPU/memory performance section, merged in from the old
        # standalone "System" tab so instance health/config/performance all
        # live in one place. Created once here (not in load()) so its
        # QTimer/ResourceSampler state survives repeated Info tab reloads --
        # load() only detaches and re-attaches this widget, never deletes it.
        self._system = SystemTab()

    def load(self, instance: ServerInstance, status: str = "stopped") -> None:
        """Schedule a read-only Diagnostics snapshot for the selected server."""
        self._instance = instance
        self._status = status
        self._generation += 1
        self._inspect_logs_btn.setEnabled(True)
        self._fix_loading_btn.setEnabled(True)
        self._health_label.setText("Checking registered server folder…")
        self._system.load(instance)
        self._show_loading_state()
        self._refresh_pending = True
        if self.isVisible():
            self._start_inventory_worker()

    def showEvent(self, event):
        super().showEvent(event)
        if self._refresh_pending:
            self._start_inventory_worker()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._thread is not None:
            # The result handler intentionally ignores hidden pages. Mark the
            # snapshot stale so the next show cannot remain on a loading view.
            self._generation += 1
            self._refresh_pending = True

    def _clear_dynamic_content(self) -> None:
        # Detach, but never delete, the persistent System usage widget.
        self._layout.removeWidget(self._system)
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_loading_state(self) -> None:
        self._clear_dynamic_content()
        self._layout.addWidget(self._system)
        status = QLabel("Checking server files and nested mod inventory…")
        status.setWordWrap(True)
        status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        self._layout.addWidget(status)
        self._layout.addStretch()

    def _start_inventory_worker(self) -> None:
        if (
            not self._refresh_pending
            or not self.isVisible()
            or self._thread is not None
            or self._instance is None
        ):
            return
        self._refresh_pending = False
        generation = self._generation
        thread = QThread()
        thread.setObjectName("CrucibleDiagnosticsInventoryThread")
        worker = _DiagnosticsInventoryWorker(
            generation, self._instance
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_inventory_loaded)
        thread.finished.connect(worker.deleteLater)

        tab_ref = weakref.ref(self)
        lifecycle_state = self._lifecycle_state

        def _cleanup() -> None:
            _ACTIVE_DIAGNOSTICS_WORKERS.pop(thread, None)
            tab = tab_ref()
            if tab is None:
                return
            if tab._thread is thread:
                tab._thread = None
                tab._worker = None
            if lifecycle_state["destroyed"]:
                return
            if tab._refresh_pending and tab.isVisible():
                tab._start_inventory_worker()

        connect_thread_cleanup(thread, _cleanup)
        self._thread = thread
        self._worker = worker
        _ACTIVE_DIAGNOSTICS_WORKERS[thread] = worker
        thread.start()

    def _on_inventory_loaded(self, generation: int, payload: object) -> None:
        if (
            generation != self._generation
            or not self.isVisible()
            or not isinstance(payload, dict)
        ):
            return
        error = str(payload.get("error") or "")
        if error:
            self._health_label.setText(
                f"Diagnostics validation unavailable: {error}"
            )
            self._clear_dynamic_content()
            self._layout.addWidget(self._system)
            message = QLabel(f"Diagnostics could not read this server folder: {error}")
            message.setWordWrap(True)
            message.setStyleSheet(f"color: {theme.YELLOW}; font-size: 12px;")
            self._layout.addWidget(message)
            self._layout.addStretch()
            return
        self._render_loaded(self._instance, self._status, payload)

    def _render_loaded(
        self,
        instance: ServerInstance,
        status: str,
        payload: dict,
    ) -> None:
        self._inspect_logs_btn.setEnabled(True)
        self._fix_loading_btn.setEnabled(True)
        self._clear_dynamic_content()
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
            "starting":     theme.PROGRESS,
            "stopping":     theme.PROGRESS,
            "tmux_missing": theme.YELLOW,
            "missing":      theme.RED,
            "unmanaged":    theme.YELLOW,
        }.get(status, theme.YELLOW)

        bundled = payload["bundled"]
        problems = payload["problems"]
        script = payload["script"]
        log = payload["log"]
        worlds = payload["worlds"]
        mod_count = payload["mod_count"]
        state_label = {
            "running": "Running",
            "stopped": "Stopped",
            "starting": "Starting",
            "stopping": "Stopping",
            "tmux_missing": "tmux unavailable",
            "missing": "Server files missing",
            "unmanaged": "Unmanaged process",
            "unknown": "Status unavailable",
        }.get(status, status.replace("_", " ").title())
        health_parts = [f"Server state: {state_label}."]
        if problems:
            health_parts.append(
                f"{len(problems)} readiness/validation issue(s) need review."
            )
        else:
            health_parts.append("The registered server folder passes local validation.")
        health_parts.append(
            "A status check never starts or stops the server."
        )
        self._health_label.setText(" ".join(health_parts))

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
        if getattr(instance, "pack_source", "") or getattr(instance, "minecraft_version", "") or getattr(instance, "loader", ""):
            add_row(13, "PACK SOURCE", getattr(instance, "pack_source", "") or "manual")
            add_row(14, "MINECRAFT", getattr(instance, "minecraft_version", ""))
            loader = getattr(instance, "loader", "")
            loader_ver = getattr(instance, "loader_version", "")
            add_row(15, "LOADER", (loader + (" " + loader_ver if loader_ver else "")) if loader else "")
        if getattr(instance, "prism_source", ""):
            add_row(16, "PRISM SOURCE", getattr(instance, "prism_source", ""), theme.SURFACE2)

        self._layout.addWidget(self._system)
        self._system.load(instance)
        self._layout.addLayout(grid)

        # Validation warnings
        if problems:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"color: {theme.SURFACE1};")
            self._layout.addWidget(sep)

            warn_title = QLabel("Validation issues")
            warn_title.setStyleSheet(
                f"color: {theme.YELLOW}; font-size: 12px; font-weight: 600;"
            )
            self._layout.addWidget(warn_title)

            for p in problems:
                w = QLabel(f"- {p}")
                w.setStyleSheet(f"color: {theme.YELLOW}; font-size: 12px;")
                w.setWordWrap(True)
                self._layout.addWidget(w)

        self._layout.addStretch()
