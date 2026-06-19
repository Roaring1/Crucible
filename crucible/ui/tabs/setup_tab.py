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

from PyQt6.QtCore import Qt, QUrl, QTimer, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QScrollArea, QApplication, QMessageBox,
)

from ...data.instance_model import ServerInstance
from .. import theme


class SetupTab(QWidget):
    """Friendly readiness checklist + quick actions for one instance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: ServerInstance | None = None
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

        # Download-from-pack section (only when an index exists)
        self._maybe_add_download_section()

        self._layout.addStretch()

    def _make_fix_button(self, fix: str) -> QPushButton | None:
        if fix == "accept_eula":
            btn = QPushButton("Accept EULA")
            btn.setToolTip("Writes eula=true (you agree to the Minecraft EULA)")
            btn.clicked.connect(self._accept_eula)
            return btn
        if fix == "install_server":
            btn = QPushButton("How to install")
            btn.clicked.connect(self._explain_install)
            return btn
        if fix == "start_once":
            btn = QPushButton("Open folder")
            btn.clicked.connect(self._open_folder)
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
        self._ip_btn.setEnabled(False)
        self._ip_btn.setText("…")

        def _worker():
            ip = ""
            try:
                import urllib.request
                with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
                    ip = r.read().decode().strip()
            except Exception:
                ip = ""
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(
                self, "_on_address_ready",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, ip))
        threading.Thread(target=_worker, daemon=True).start()

    @pyqtSlot(str)
    def _on_address_ready(self, ip: str) -> None:
        self._ip_btn.setEnabled(True)
        inst = self._instance
        port = "25565"
        try:
            if inst is not None:
                port = inst.server_port()
        except Exception:
            port = "25565"
        if ip:
            addr = f"{ip}:{port}"
            try:
                QApplication.clipboard().setText(addr)
            except Exception:
                pass
            self._ip_btn.setText(f"✓  Copied {addr}")
        else:
            self._ip_btn.setText("✗  No internet — use local IP")
        QTimer.singleShot(2500, lambda: self._ip_btn.setText("⧉  Copy connection address"))

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
