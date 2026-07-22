"""
crucible/ui/tabs/setup_tab.py

"Setup" tab — an easy-mode, plain-language checklist for non-technical server
owners. Shows a green/yellow/red readiness list with one-click fix buttons:

  * Accept the Minecraft EULA
  * Open the server folder in the file manager
  * Copy the connection address (IP:port) for friends
  * Download mods listed in the pack index (best-effort)

Everything is wrapped in try/except so a quirky environment never crashes the
app; problems surface as friendly inline messages instead.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QUrl, QTimer, QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QScrollArea, QApplication, QMessageBox,
    QSpinBox, QComboBox,
)

from ...data.instance_manager import InstanceManager
from ...data.instance_model import ServerInstance
from ...process.resource_monitor import system_memory_mb
from .. import theme


def _suggest_memory_mb(total_mb: float | None) -> int:
    """Heuristic suggested -Xms/-Xmx (in MB) for a dedicated Minecraft
    server on this machine, leaving headroom for the OS, Crucible itself,
    and anything else running.

    Reserves at least 2 GB or 25% of total RAM (whichever is larger) for
    everything other than the server JVM, then clamps the remainder to a
    sane 1-16 GB range and rounds to the nearest 512 MB.
    """
    if not total_mb or total_mb <= 0:
        return 4096
    reserve_mb = max(2048.0, total_mb * 0.25)
    target_mb = total_mb - reserve_mb
    target_mb = max(1024.0, min(target_mb, 16384.0))
    return int(round(target_mb / 512) * 512)


class _ServerInstallWorker(QObject):
    finished = pyqtSignal(object, str)

    def __init__(self, path, mc, loader, loader_version):
        super().__init__()
        self._args = (path, mc, loader, loader_version)

    def run(self) -> None:
        try:
            from ...importers import serverloader as sl
            path, mc, loader, loader_version = self._args
            result = sl.install_server_loader(
                path,
                minecraft_version=mc,
                loader=loader,
                loader_version=loader_version,
            )
            self.finished.emit(result, "")
        except Exception as exc:
            self.finished.emit(None, str(exc))


class SetupTab(QWidget):
    """Friendly readiness checklist + quick actions for one instance."""

    def __init__(self, manager: InstanceManager | None = None, parent=None):
        super().__init__(parent)
        self._manager: InstanceManager | None = manager
        self._instance: ServerInstance | None = None
        self._install_thread: QThread | None = None
        self._install_worker: _ServerInstallWorker | None = None
        self._ip_request_generation = 0
        self._xms_spin: QSpinBox | None = None
        self._xmx_spin: QSpinBox | None = None
        self._xms_unit: QComboBox | None = None
        self._xmx_unit: QComboBox | None = None
        self._mem_status: QLabel | None = None
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
        self._layout.setSpacing(14)

        self._placeholder = QLabel("No instance selected.")
        self._placeholder.setStyleSheet(f"color: {theme.SURFACE2};")
        self._layout.addWidget(self._placeholder)
        self._layout.addStretch()

    # Public API

    def load(self, instance: ServerInstance) -> None:
        self._ip_request_generation += 1
        self._instance = instance
        self._rebuild()

    def refresh(self) -> None:
        if self._instance is not None:
            self._rebuild()

    # Rendering

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _rebuild(self) -> None:
        self._clear()
        inst = self._instance
        if inst is None:
            self._layout.addWidget(QLabel("No instance selected."))
            self._layout.addStretch()
            return

        # Heading
        title = QLabel("Get your server ready")
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {theme.TEXT};")
        self._layout.addWidget(title)

        sub = QLabel(
            "A quick checklist for getting friends online. Green means good to go; "
            "yellow/red items have a button to help you fix them."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        self._layout.addWidget(sub)

        # Quick actions row (always available)
        actions = QHBoxLayout()
        actions.setSpacing(8)

        open_btn = QPushButton("📁  Open server folder")
        open_btn.clicked.connect(self._open_folder)
        actions.addWidget(open_btn)

        ip_btn = QPushButton("⧉  Copy connection address")
        ip_btn.setToolTip("Copies your public IP and port so friends can join")
        ip_btn.clicked.connect(self._copy_address)
        self._ip_btn = ip_btn
        actions.addWidget(ip_btn)

        refresh_btn = QPushButton("↻  Re-check")
        refresh_btn.clicked.connect(self._rebuild)
        actions.addWidget(refresh_btn)
        actions.addStretch()
        self._layout.addLayout(actions)

        # Readiness checklist
        try:
            items = inst.readiness()
        except Exception as exc:
            err = QLabel(f"Could not run readiness checks: {exc}")
            err.setStyleSheet(f"color: {theme.RED};")
            err.setWordWrap(True)
            self._layout.addWidget(err)
            items = []

        card = QFrame()
        card.setStyleSheet(
            f"background: {theme.SURFACE0}; border-radius: 6px;"
        )
        grid = QGridLayout(card)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(1, 1)

        for row, item in enumerate(items):
            ok = item.get("ok")
            if ok is True:
                icon, color = "✓", theme.GREEN
            elif ok is False:
                icon, color = "✗", theme.RED
            else:
                icon, color = "—", theme.YELLOW

            dot = QLabel(icon)
            dot.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: 700;")
            dot.setFixedWidth(18)
            dot.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(dot, row, 0, Qt.AlignmentFlag.AlignTop)

            text_col = QVBoxLayout()
            text_col.setSpacing(1)
            lbl = QLabel(item.get("label", ""))
            lbl.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
            text_col.addWidget(lbl)
            detail = QLabel(str(item.get("detail", "")))
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            detail.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
            text_col.addWidget(detail)
            grid.addLayout(text_col, row, 1)

            fix = item.get("fix")
            if fix:
                fix_btn = self._make_fix_button(fix)
                if fix_btn is not None:
                    grid.addWidget(fix_btn, row, 2, Qt.AlignmentFlag.AlignTop)

        self._layout.addWidget(card)

        # Server performance (memory) section
        self._add_memory_section(inst)

        # Download-from-pack section (only when an index exists)
        self._maybe_add_download_section()

        self._layout.addStretch()

    def _add_memory_section(self, inst: ServerInstance) -> None:
        """In-app editor for the server's -Xms/-Xmx JVM memory flags.

        Reads/writes ServerInstance.java_args via get_memory_mb()/set_memory_mb(),
        which preserve every other flag (IPv4 stack flags, @java9args.txt, GC
        tuning, etc.) exactly. Changing this only affects the NEXT server start
        -- it never touches a currently-running JVM's actual heap size.
        """
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.SURFACE1};")
        self._layout.addWidget(sep)

        head = QLabel("🧠  Server memory (Java heap)")
        head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        self._layout.addWidget(head)

        _, total_mb = system_memory_mb()
        total_gb_text = f"{total_mb / 1024:.0f} GB" if total_mb else "unknown"
        info = QLabel(
            f"This machine has {total_gb_text} of RAM. Minimum (-Xms) and maximum "
            "(-Xmx) memory the server's Java process may use. Takes effect the "
            "NEXT time you start this server -- it does not resize memory for an "
            "already-running server."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        self._layout.addWidget(info)

        xms_mb, xmx_mb = inst.get_memory_mb()
        row = QHBoxLayout()
        row.setSpacing(10)

        row.addWidget(QLabel("Min (Xms):"))
        self._xms_spin = QSpinBox()
        self._xms_unit = QComboBox()
        self._xms_unit.addItems(["MB", "GB"])
        self._configure_memory_unit(
            self._xms_spin, self._xms_unit, xms_mb if xms_mb else 2048, initial=True,
        )
        self._xms_unit.currentTextChanged.connect(
            lambda text: self._on_memory_unit_changed(self._xms_spin, text)
        )
        row.addWidget(self._xms_spin)
        row.addWidget(self._xms_unit)

        row.addWidget(QLabel("Max (Xmx):"))
        self._xmx_spin = QSpinBox()
        self._xmx_unit = QComboBox()
        self._xmx_unit.addItems(["MB", "GB"])
        self._configure_memory_unit(
            self._xmx_spin, self._xmx_unit, xmx_mb if xmx_mb else 4096, initial=True,
        )
        self._xmx_unit.currentTextChanged.connect(
            lambda text: self._on_memory_unit_changed(self._xmx_spin, text)
        )
        row.addWidget(self._xmx_spin)
        row.addWidget(self._xmx_unit)

        save_btn = QPushButton("Save memory settings")
        save_btn.clicked.connect(self._save_memory)
        row.addWidget(save_btn)
        row.addStretch()
        self._layout.addLayout(row)

        # Suggested amount -- a plain heuristic based on this machine's total
        # RAM, so non-technical owners don't have to guess a value out of thin
        # air. Xms is suggested equal to Xmx, which is the common recommendation
        # for dedicated Minecraft servers (it avoids in-game heap-resize pauses
        # from the JVM growing the heap mid-session).
        suggested_mb = _suggest_memory_mb(total_mb)
        suggest_row = QHBoxLayout()
        suggest_label = QLabel(
            f"Suggested for this machine: {suggested_mb / 1024:.1f} GB "
            f"(leaves headroom for the OS, Crucible, and other programs)"
        )
        suggest_label.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        suggest_label.setWordWrap(True)
        apply_suggested_btn = QPushButton("Apply suggested")
        apply_suggested_btn.clicked.connect(lambda: self._apply_suggested_memory(suggested_mb))
        suggest_row.addWidget(suggest_label, stretch=1)
        suggest_row.addWidget(apply_suggested_btn)
        self._layout.addLayout(suggest_row)

        self._mem_status = QLabel("")
        self._mem_status.setWordWrap(True)
        self._mem_status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        self._layout.addWidget(self._mem_status)

        if xms_mb is None and xmx_mb is None:
            self._mem_status.setText(
                "⚠ Could not find -Xms/-Xmx in this server's Java arguments -- "
                "showing defaults. Saving will add them."
            )
            self._mem_status.setStyleSheet(f"color: {theme.YELLOW}; font-size: 11px;")

    @staticmethod
    def _configure_memory_unit(
        spin: QSpinBox, unit_combo: QComboBox, mb_value: int, initial: bool = False,
    ) -> None:
        """Configure a memory QSpinBox's range/suffix/value for whichever unit
        is selected in unit_combo, given an underlying value in MB.

        On initial setup, defaults to GB display when the value is a clean
        multiple of 1024 (the common case), otherwise MB so odd values (e.g.
        1536 MB) aren't misleadingly rounded.
        """
        if initial:
            use_gb = mb_value >= 1024 and mb_value % 1024 == 0
            unit_combo.setCurrentText("GB" if use_gb else "MB")
        unit = unit_combo.currentText()
        if unit == "GB":
            spin.setRange(1, 1024)
            spin.setSingleStep(1)
            spin.setSuffix(" GB")
            spin.setValue(max(1, round(mb_value / 1024)))
        else:
            spin.setRange(512, 1_048_576)
            spin.setSingleStep(512)
            spin.setSuffix(" MB")
            spin.setValue(mb_value)

    def _on_memory_unit_changed(self, spin: QSpinBox, new_unit: str) -> None:
        """Convert a memory spinbox's current value when its unit dropdown
        changes, so switching GB<->MB never silently changes the underlying
        amount (beyond GB's whole-number rounding)."""
        old_suffix = spin.suffix().strip()
        old_mb = spin.value() * 1024 if old_suffix == "GB" else spin.value()
        if new_unit == "GB":
            spin.setRange(1, 1024)
            spin.setSingleStep(1)
            spin.setSuffix(" GB")
            spin.setValue(max(1, round(old_mb / 1024)))
        else:
            spin.setRange(512, 1_048_576)
            spin.setSingleStep(512)
            spin.setSuffix(" MB")
            spin.setValue(max(512, old_mb))

    @staticmethod
    def _spin_value_mb(spin: QSpinBox) -> int:
        """Read a memory spinbox's current value in MB, regardless of
        whichever unit (MB/GB) its suffix currently displays."""
        return spin.value() * 1024 if spin.suffix().strip() == "GB" else spin.value()

    def _apply_suggested_memory(self, suggested_mb: int) -> None:
        if self._xms_spin is None or self._xmx_spin is None:
            return
        if self._xms_unit is not None:
            self._xms_unit.setCurrentText("GB")
        if self._xmx_unit is not None:
            self._xmx_unit.setCurrentText("GB")
        self._configure_memory_unit(self._xms_spin, self._xms_unit, suggested_mb)
        self._configure_memory_unit(self._xmx_spin, self._xmx_unit, suggested_mb)

    def _save_memory(self) -> None:
        inst = self._instance
        if inst is None or self._xms_spin is None or self._xmx_spin is None:
            return
        xms_mb = self._spin_value_mb(self._xms_spin)
        xmx_mb = self._spin_value_mb(self._xmx_spin)

        _, total_mb = system_memory_mb()
        if total_mb and xmx_mb > total_mb:
            resp = QMessageBox.warning(
                self, "Memory exceeds installed RAM",
                f"-Xmx {xmx_mb} MB is more than the {total_mb:.0f} MB of RAM this "
                "machine has. The server would likely fail to start or the OS "
                "would heavily swap.\n\nSave anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        elif total_mb and xmx_mb > total_mb * 0.85:
            QMessageBox.information(
                self, "High memory allocation",
                f"-Xmx {xmx_mb} MB uses most of this machine's {total_mb:.0f} MB "
                "of RAM, leaving little for the OS, Crucible itself, and other "
                "programs. Consider leaving at least 10-15% of RAM free.",
            )

        try:
            inst.set_memory_mb(xms_mb, xmx_mb)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid memory settings", str(exc))
            return

        if self._manager is not None:
            try:
                self._manager.update_instance(inst)
            except Exception as exc:
                QMessageBox.warning(
                    self, "Memory settings",
                    f"Updated in memory, but could not save to disk:\n{exc}",
                )
                return

        if self._mem_status is not None:
            self._mem_status.setText(
                f"✓ Saved. Will apply next time this server starts "
                f"(-Xms{xms_mb}M -Xmx{xmx_mb}M)."
            )
            self._mem_status.setStyleSheet(f"color: {theme.GREEN}; font-size: 11px;")

    def _make_fix_button(self, fix: str) -> QPushButton | None:
        if fix == "accept_eula":
            btn = QPushButton("Accept EULA")
            btn.setToolTip("Writes eula=true (you agree to the Minecraft EULA)")
            btn.clicked.connect(self._accept_eula)
            return btn
        if fix == "install_server":
            btn = QPushButton("Install server now")
            btn.setToolTip("Download the matching dedicated server program (needs internet)")
            btn.clicked.connect(self._install_server)
            return btn
        if fix == "start_once":
            btn = QPushButton("Open folder")
            btn.clicked.connect(self._open_folder)
            return btn
        if fix == "fix_properties":
            btn = QPushButton("Check settings")
            btn.setToolTip("Find and fix invalid server.properties values")
            btn.clicked.connect(self._fix_properties)
            return btn
        return None

    def _maybe_add_download_section(self) -> None:
        inst = self._instance
        if inst is None:
            return
        try:
            from ...importers.downloader import has_downloadable_index
            available = has_downloadable_index(inst.path)
        except Exception:
            available = False
        if not available:
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.SURFACE1};")
        self._layout.addWidget(sep)

        head = QLabel("⬇  Download mods from pack")
        head.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        self._layout.addWidget(head)

        info = QLabel(
            "This pack shipped a download list instead of the actual mod files. "
            "Crucible can try to fetch them (needs internet; CurseForge packs may "
            "need an API key). Importing a fully-installed Prism instance is the "
            "most reliable option."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        self._layout.addWidget(info)

        dl_btn = QPushButton("Download server mods…")
        dl_btn.clicked.connect(self._download_mods)
        self._layout.addWidget(dl_btn, alignment=Qt.AlignmentFlag.AlignLeft)

    # Actions

    def _open_folder(self) -> None:
        if self._instance is None:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._instance.path)))
        except Exception as exc:
            QMessageBox.warning(self, "Open folder", f"Could not open the folder:\n{exc}")

    def _accept_eula(self) -> None:
        if self._instance is None:
            return
        resp = QMessageBox.question(
            self, "Accept Minecraft EULA",
            "By accepting, you agree to the Minecraft EULA "
            "(https://aka.ms/MinecraftEULA).\n\nWrite eula=true now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            self._instance.set_eula_accepted(True)
        except Exception as exc:
            QMessageBox.warning(self, "Accept EULA", f"Could not write eula.txt:\n{exc}")
            return
        self._rebuild()

    def _fix_properties(self) -> None:
        inst = self._instance
        if inst is None:
            return
        try:
            from ..properties_dialog import PropertiesFixDialog
            dlg = PropertiesFixDialog(inst.path_obj / "server.properties", parent=self)
            dlg.exec()
        except Exception as exc:
            QMessageBox.warning(self, "Server settings", f"Could not open settings check:\n{exc}")
        self._rebuild()

    def _install_server(self) -> None:
        inst = self._instance
        if inst is None:
            return
        mc = (inst.minecraft_version or "").strip()
        if not mc:
            self._explain_install()
            return
        try:
            from ...importers import serverloader as sl
        except Exception:
            self._explain_install()
            return
        loader = sl.normalize_loader(inst.loader or "vanilla")
        if sl.requires_java(loader):
            try:
                java_ok, _ = ServerInstance.java_info()
            except Exception:
                java_ok = False
            if not java_ok:
                QMessageBox.warning(
                    self, "Java required",
                    f"Installing a {loader} server runs an installer that needs Java "
                    "on your PATH, which wasn't found. Vanilla and Fabric don't need it.",
                )
                return
        resp = QMessageBox.question(
            self, "Install server program",
            f"Download the {loader} dedicated server for Minecraft {mc} into this "
            "folder? This needs an internet connection.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        self.setEnabled(False)
        self._install_thread = QThread()
        self._install_worker = _ServerInstallWorker(
            inst.path, mc, loader, inst.loader_version or ""
        )
        self._install_worker.moveToThread(self._install_thread)
        self._install_thread.started.connect(self._install_worker.run)
        self._install_worker.finished.connect(self._on_server_install_finished)
        self._install_worker.finished.connect(self._install_thread.quit)
        self._install_worker.finished.connect(self._install_worker.deleteLater)
        self._install_thread.finished.connect(self._install_thread.deleteLater)
        self._install_thread.finished.connect(self._install_thread_finished)
        self._install_thread.start()

    @pyqtSlot(object, str)
    def _on_server_install_finished(self, result, error: str) -> None:
        self.setEnabled(True)
        if error:
            QMessageBox.warning(self, "Install server", f"Install failed:\n{error}")
        elif result is not None and result.ok:
            QMessageBox.information(self, "Install server", "Success: " + result.summary())
        else:
            reason = result.failed_reason if result is not None else "unknown error"
            QMessageBox.warning(
                self, "Install server",
                f"Could not install automatically:\n{reason}\n\n"
                "You can also import a fully-installed Prism instance, or drop the "
                "matching server jar into this folder, then press Re-check.",
            )
        self._rebuild()

    def _install_thread_finished(self) -> None:
        self._install_thread = None
        self._install_worker = None

    def has_active_operation(self) -> bool:
        return bool(self._install_thread and self._install_thread.isRunning())

    def _explain_install(self) -> None:
        QMessageBox.information(
            self, "Install the server program",
            "Crucible found pack files but no dedicated-server program to run them.\n\n"
            "The most reliable fix is to import a fully-installed Prism instance "
            "(File → Add → Import Prism Instance), which includes the server jar/loader.\n\n"
            "Alternatively, place the matching server jar (e.g. the Forge/Fabric/NeoForge "
            "server installer output, or server.jar) into this folder, then press Re-check.",
        )

    def _copy_address(self) -> None:
        """Fetch public IP off-thread and copy IP:port to the clipboard."""
        inst = self._instance
        if inst is None:
            return
        self._ip_request_generation += 1
        generation = self._ip_request_generation
        instance_id = inst.id
        try:
            port = inst.server_port()
        except Exception:
            port = "25565"
        self._ip_btn.setEnabled(False)
        self._ip_btn.setText("…")

        def _worker():
            try:
                from ...data.netinfo import public_host
                ip = public_host(timeout=5)
            except Exception:
                ip = ""
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(
                self, "_on_address_ready", Qt.ConnectionType.QueuedConnection,
                Q_ARG(int, generation), Q_ARG(str, instance_id),
                Q_ARG(str, ip), Q_ARG(str, port),
            )
        threading.Thread(target=_worker, daemon=True).start()

    @pyqtSlot(int, str, str, str)
    def _on_address_ready(self, generation: int, instance_id: str,
                          ip: str, port: str) -> None:
        if (
            generation != self._ip_request_generation
            or self._instance is None
            or self._instance.id != instance_id
        ):
            return
        self._ip_btn.setEnabled(True)
        if ip:
            addr = f"{ip}:{port}"
            try:
                QApplication.clipboard().setText(addr)
            except Exception:
                pass
            self._ip_btn.setText(f"✓  Copied {addr}")
        else:
            self._ip_btn.setText("✗  No internet — use local IP")
        QTimer.singleShot(
            2500, lambda: self._reset_ip_button(generation, instance_id)
        )

    def _reset_ip_button(self, generation: int, instance_id: str) -> None:
        if (
            generation == self._ip_request_generation
            and self._instance is not None
            and self._instance.id == instance_id
        ):
            self._ip_btn.setText("⧉  Copy connection address")

    def _download_mods(self) -> None:
        inst = self._instance
        if inst is None:
            return
        try:
            from ..download_dialog import DownloadModsDialog
            dlg = DownloadModsDialog(inst.path, inst.name, parent=self)
            dlg.start()
            dlg.exec()
        except Exception as exc:
            QMessageBox.warning(self, "Download mods", f"Could not start download:\n{exc}")
        # Refresh mod counts etc. afterward.
        self._rebuild()
