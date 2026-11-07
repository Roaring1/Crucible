"""Native Qt system-tray controller for Crucible.

The tray is a view/controller only. It never starts, stops, or probes tmux
itself; status comes from MainWindow's shared health result and lifecycle
requests are delegated back to MainWindow.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from ..data.instance_model import ServerInstance
from ..data.server_icons import resolve_icon_path
from ..process.machine_resources import MachineUsage, sample_machine_usage
from .server_tint import icon_with_accent

_STATUS_LABELS = {
    "running": "Running",
    "stopped": "Stopped",
    "starting": "Starting",
    "stopping": "Stopping",
    "unknown": "Unknown",
    "tmux_missing": "tmux unavailable",
    "missing": "Files missing",
    "unmanaged": "Unmanaged",
    "checking": "Checking",
}


class _UsageThread(QThread):
    usage_ready = pyqtSignal(object)

    def __init__(self, instances: list[ServerInstance], gui_pid: int, parent=None):
        super().__init__(parent)
        self._instances = list(instances)
        self._gui_pid = gui_pid

    def run(self) -> None:
        try:
            self.usage_ready.emit(sample_machine_usage(self._instances, self._gui_pid))
        except Exception as exc:  # noqa: BLE001 - failed optional telemetry is displayed as Unavailable
            self.usage_ready.emit(exc)


class SystemTrayController(QObject):
    """One tray icon with live registered-server actions and lazy usage details."""

    def __init__(
        self,
        parent,
        *,
        instances: Callable[[], list[ServerInstance]],
        statuses: Callable[[], dict[str, str]],
        open_ui: Callable[[], None],
        open_server: Callable[[ServerInstance], None],
        lifecycle: Callable[[ServerInstance, str], None],
        quit_app: Callable[[], None],
        gui_pid: int,
    ) -> None:
        super().__init__(parent)
        self._parent = parent
        self._instances = instances
        self._statuses = statuses
        self._open_ui = open_ui
        self._open_server = open_server
        self._lifecycle = lifecycle
        self._quit_app = quit_app
        self._gui_pid = gui_pid
        self._usage_thread: _UsageThread | None = None
        self._usage_cache: MachineUsage | None = None
        self._usage_cached_at = 0.0
        self._usage_error: str | None = None
        self._server_menus: list[QMenu] = []
        self._server_icon_cache: dict[tuple[str, int], QIcon] = {}
        self._server_menu_signature: tuple | None = None

        icon_path = Path(__file__).resolve().parent / "assets" / "tray" / "crucible.svg"
        self._tray = QSystemTrayIcon(QIcon(str(icon_path)), self)
        self._tray.setToolTip("Crucible")
        self._menu = QMenu(parent)
        self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()
        self.refresh_server_menu()

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    @property
    def visible(self) -> bool:
        return self._tray.isVisible()

    def hide(self) -> None:
        self._tray.hide()

    def show(self) -> None:
        self._tray.show()

    def show_message(self, title: str, message: str, *, warning: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning else QSystemTrayIcon.MessageIcon.Information
        )
        self._tray.showMessage(title, message, icon, 5000)

    def shutdown(self) -> None:
        """Retire the short-lived telemetry worker before the owner window exits."""
        thread = self._usage_thread
        if thread is not None:
            if not thread.wait(3000):
                # Resource sampling is bounded by a 350ms sample gap and a
                # 1.5s GPU timeout. Still retain the wrapper until native
                # completion rather than destroy a live QThread.
                thread.wait()
            thread.deleteLater()
            self._usage_thread = None
        self._tray.setContextMenu(None)
        self._tray.hide()
        self._menu.close()
        self._menu.deleteLater()

    def _build_menu(self) -> None:
        heading = self._menu.addAction("Crucible")
        heading.setEnabled(False)
        self._menu.addSeparator()

        self._usage_menu = self._menu.addMenu("System usage")
        self._usage_managed = self._usage_menu.addAction("Crucible + servers: measuring…")
        self._usage_system = self._usage_menu.addAction("System: measuring…")
        self._usage_gpu = self._usage_menu.addAction("GPU: measuring…")
        for action in (self._usage_managed, self._usage_system, self._usage_gpu):
            action.setEnabled(False)
        self._usage_menu.aboutToShow.connect(self._refresh_usage)

        self._servers_menu = self._menu.addMenu("Servers")
        self._menu.addSeparator()
        open_action = self._menu.addAction("Open Crucible")
        open_action.triggered.connect(self._open_ui)
        self._menu.addSeparator()
        quit_action = self._menu.addAction("Quit Crucible")
        quit_action.triggered.connect(self._quit_app)

        self._menu.aboutToShow.connect(lambda: self.refresh_server_menu(force=True))

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._open_ui()

    def _server_icon(self, instance: ServerInstance) -> QIcon:
        path = resolve_icon_path(instance)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        key = (str(path), mtime_ns)
        cached = self._server_icon_cache.get(key)
        if cached is not None:
            return cached
        result = icon_with_accent(QIcon(str(path)))
        if len(self._server_icon_cache) >= 256:
            self._server_icon_cache.clear()
        self._server_icon_cache[key] = result
        return result

    def refresh_server_menu(self, *, force: bool = False) -> None:
        # Do not rebuild an open native menu while the user is navigating it.
        if not force and (self._menu.isVisible() or self._servers_menu.isVisible()):
            return
        instances = self._instances()
        status_map = self._statuses()
        signature = tuple(
            (
                instance.id,
                instance.name,
                instance.path,
                status_map.get(instance.id, "unknown"),
            )
            for instance in instances
        )
        if not force and signature == self._server_menu_signature:
            return
        self._server_menu_signature = signature

        for old_menu in self._server_menus:
            old_menu.deleteLater()
        self._server_menus.clear()
        self._servers_menu.clear()
        self._tray.setToolTip(f"Crucible · {len(instances)} registered server(s)")
        if not instances:
            empty = self._servers_menu.addAction("No registered servers")
            empty.setEnabled(False)
            return

        for instance in instances:
            status = status_map.get(instance.id, "unknown")
            label = _STATUS_LABELS.get(status, "Unknown")
            server_menu = QMenu(f"{instance.name} · {label}", self._servers_menu)
            server_menu.setIcon(self._server_icon(instance))
            self._server_menus.append(server_menu)
            self._servers_menu.addMenu(server_menu)

            state_action = server_menu.addAction(f"Status: {label}")
            state_action.setEnabled(False)
            server_menu.addSeparator()

            if status == "stopped":
                action = server_menu.addAction(
                    QIcon(str(Path(__file__).resolve().parent / "assets" / "menu-icons" / "play.svg")),
                    "Start server",
                )
                action.triggered.connect(
                    lambda _checked=False, item=instance: self._lifecycle(item, "start")
                )
            elif status == "running":
                action = server_menu.addAction(
                    QIcon(str(Path(__file__).resolve().parent / "assets" / "menu-icons" / "stop.svg")),
                    "Stop server",
                )
                action.triggered.connect(
                    lambda _checked=False, item=instance: self._lifecycle(item, "stop")
                )
            else:
                pending = server_menu.addAction(
                    "Starting…" if status == "starting"
                    else "Stopping…" if status == "stopping"
                    else "Lifecycle action unavailable"
                )
                pending.setEnabled(False)

            server_menu.addSeparator()
            open_action = server_menu.addAction("Open in Crucible")
            open_action.triggered.connect(
                lambda _checked=False, item=instance: self._open_server(item)
            )
            folder_action = server_menu.addAction("Open folder")
            folder_action.setEnabled(Path(instance.path).is_dir())
            folder_action.triggered.connect(
                lambda _checked=False, item=instance: self._open_server_folder(item)
            )

    def _open_server_folder(self, instance: ServerInstance) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        path = Path(instance.path)
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    @staticmethod
    def _fmt_memory(mb: float) -> str:
        return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"

    def _refresh_usage(self) -> None:
        if self._usage_thread is not None:
            self._set_usage_labels("Measuring…", "Measuring…", "Measuring…")
            return
        if self._usage_cache is not None and time.monotonic() - self._usage_cached_at < 10:
            self._show_usage(self._usage_cache)
            return

        self._set_usage_labels("Measuring…", "Measuring…", "Measuring…")
        thread = _UsageThread(self._instances(), self._gui_pid, self._parent)
        self._usage_thread = thread
        thread.usage_ready.connect(self._on_usage_ready)
        thread.finished.connect(self._on_usage_thread_finished)
        thread.start()

    @pyqtSlot(object)
    def _on_usage_ready(self, result: object) -> None:
        if isinstance(result, MachineUsage):
            self._usage_cache = result
            self._usage_cached_at = time.monotonic()
            self._usage_error = None
            self._show_usage(result)
        else:
            self._usage_error = str(result)
            self._set_usage_labels("Unavailable", "Unavailable", "Unavailable")

    @pyqtSlot()
    def _on_usage_thread_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, _UsageThread):
            if self._usage_thread is thread:
                self._usage_thread = None
            thread.deleteLater()

    def _show_usage(self, usage: MachineUsage) -> None:
        managed_cpu = (
            f"{usage.managed_cpu_pct:.1f}%"
            if usage.managed_cpu_pct is not None else "Unavailable"
        )
        managed_ram = (
            self._fmt_memory(usage.managed_memory_mb)
            if usage.managed_memory_mb is not None else "Unavailable"
        )
        self._usage_managed.setText(f"Crucible + servers: CPU {managed_cpu} · RAM {managed_ram}")

        system_cpu = (
            f"{usage.system_cpu_pct:.0f}%"
            if usage.system_cpu_pct is not None else "Unavailable"
        )
        if usage.system_memory_total_mb > 0:
            system_ram = (
                f"{self._fmt_memory(usage.system_memory_used_mb)} / "
                f"{self._fmt_memory(usage.system_memory_total_mb)}"
            )
        else:
            system_ram = "Unavailable"
        self._usage_system.setText(f"System: CPU {system_cpu} · RAM {system_ram}")

        if (
            usage.gpu_pct is not None
            and usage.gpu_memory_used_mb is not None
            and usage.gpu_memory_total_mb is not None
        ):
            gpu_text = (
                f"GPU: {usage.gpu_pct:.0f}% · VRAM "
                f"{self._fmt_memory(usage.gpu_memory_used_mb)} / "
                f"{self._fmt_memory(usage.gpu_memory_total_mb)}"
            )
        else:
            gpu_text = "GPU: Unavailable"
        self._usage_gpu.setText(gpu_text)

    def _set_usage_labels(self, managed: str, system: str, gpu: str) -> None:
        self._usage_managed.setText(f"Crucible + servers: {managed}")
        self._usage_system.setText(f"System: {system}")
        self._usage_gpu.setText(gpu)
