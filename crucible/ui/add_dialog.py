"""
crucible/ui/add_dialog.py

Dialog for adding a new server instance.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QDialogButtonBox, QMessageBox,
)

from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from . import theme


class AddInstanceDialog(QDialog):
    """
    Modal dialog that lets the user register a server directory.

    On accept(), the instance is added to the manager and available
    via .result_instance.
    """

    def __init__(self, manager: InstanceManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.result_instance: ServerInstance | None = None
        self.setWindowTitle("Add Server Instance")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 16, 20, 16)

        title = QLabel("Register a GTNH Server Directory")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT};"
        )
        layout.addWidget(title)

        sub = QLabel(
            "Point Crucible at an existing server folder. "
            "Files on disk are never modified by this operation."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 12px;")
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addLayout(form)

        # ── Path field ──
        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("/home/roaring/GTNH-Server-TEST")
        self._path_edit.textChanged.connect(self._auto_fill_name)
        path_row.addWidget(self._path_edit, stretch=1)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)

        form.addRow("Server path:", path_row)

        # ── Name field ──
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("My GTNH Server")
        form.addRow("Display name:", self._name_edit)

        # ── Version field ──
        self._ver_edit = QLineEdit("2.8.4")
        form.addRow("GTNH version:", self._ver_edit)

        # ── Session field ──
        self._session_edit = QLineEdit()
        self._session_edit.setPlaceholderText("auto-derived from name  (e.g. gtnh-my-server)")
        form.addRow("tmux session:", self._session_edit)

        hint = QLabel(
            "Leave session blank to auto-derive from the name.  "
            "If a tmux session already exists (e.g. 'gtnh'), enter it here to match."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        layout.addWidget(hint)

        # ── Validation warning area ──
        self._warn_label = QLabel("")
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet(
            f"color: {theme.YELLOW}; font-size: 12px; background: {theme.SURFACE0}; "
            f"border-radius: 4px; padding: 6px;"
        )
        self._warn_label.hide()
        layout.addWidget(self._warn_label)

        # ── Buttons ──
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select GTNH Server Directory", str(Path.home())
        )
        if path:
            self._path_edit.setText(path)

    def _auto_fill_name(self, text: str) -> None:
        """If name is empty/auto, fill it with the directory name."""
        if not self._name_edit.text():
            self._name_edit.setText(Path(text).name)

    def _on_accept(self) -> None:
        path    = self._path_edit.text().strip()
        name    = self._name_edit.text().strip() or Path(path).name
        version = self._ver_edit.text().strip() or "2.8.4"
        session = self._session_edit.text().strip()

        if not path:
            QMessageBox.warning(self, "Missing Path", "Please enter the server directory path.")
            return

        try:
            inst = self._manager.add_instance(path, name, version, tmux_session=session)
        except ValueError as exc:
            QMessageBox.warning(self, "Already Registered", str(exc))
            return

        # Show validation warnings but don't block
        problems = inst.validate()
        if problems:
            self._warn_label.setText(
                "⚠  Registered with warnings:\n• " + "\n• ".join(problems)
            )
            self._warn_label.show()
            # Let the user see them; clicking OK a second time closes
            self.result_instance = inst
            # Don't call accept() yet — allow user to read warnings
            # They can click OK again or Cancel
            return

        self.result_instance = inst
        self.accept()
