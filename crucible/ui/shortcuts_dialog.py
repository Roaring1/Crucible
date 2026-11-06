"""Compact dialog for creating or removing per-server desktop shortcuts."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..desktop_shortcuts import ShortcutError, create_shortcuts, remove_shortcuts


class ShortcutsDialog(QDialog):
    def __init__(
        self,
        instance,
        *,
        saved_desktop_directory: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.instance = instance
        self.created_desktop_directory: str | None = None
        self.setWindowTitle(f"Server shortcuts · {instance.name}")
        self.setMinimumWidth(480)
        self._build_ui(saved_desktop_directory)

    def _build_ui(self, saved_desktop_directory: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        heading = QLabel(f"Create shortcuts for {self.instance.name}")
        heading.setStyleSheet("font-size: 15px; font-weight: 700;")
        outer.addWidget(heading)

        hint = QLabel(
            "Create separate Start and graceful Stop launchers. They use this "
            "server's stable Crucible ID, so renaming the server will not break them."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a7b0ba;")
        outer.addWidget(hint)

        actions = QHBoxLayout()
        self.start_check = QCheckBox("Start server")
        self.start_check.setChecked(True)
        self.stop_check = QCheckBox("Stop server")
        self.stop_check.setChecked(True)
        actions.addWidget(self.start_check)
        actions.addWidget(self.stop_check)
        actions.addStretch()
        outer.addLayout(actions)

        destination_box = QWidget()
        destination_layout = QVBoxLayout(destination_box)
        destination_layout.setContentsMargins(0, 0, 0, 0)
        destination_layout.setSpacing(5)
        self.desktop_radio = QRadioButton("Desktop")
        self.apps_radio = QRadioButton("Applications menu")
        self.both_radio = QRadioButton("Both")
        self.desktop_radio.setChecked(True)
        for radio in (self.desktop_radio, self.apps_radio, self.both_radio):
            destination_layout.addWidget(radio)
            radio.toggled.connect(self._update_desktop_controls)
        outer.addWidget(QLabel("Shortcut location"))
        outer.addWidget(destination_box)

        desktop_default = (
            saved_desktop_directory.strip()
            or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
            or str(Path.home() / "Desktop")
        )
        self.desktop_path = QLineEdit(desktop_default)
        self.desktop_path.setPlaceholderText("/home/you/Desktop")
        self.desktop_path.setClearButtonEnabled(True)
        self.desktop_path.setAccessibleName("Desktop shortcut directory")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.desktop_path, 1)
        path_row.addWidget(self.browse_button)
        outer.addLayout(path_row)

        self._update_desktop_controls()

        footnote = QLabel(
            "Desktop folders can be pasted or selected. Applications-menu shortcuts "
            "are installed in your user account only; nothing is written system-wide."
        )
        footnote.setWordWrap(True)
        footnote.setStyleSheet("color: #a7b0ba; font-size: 11px;")
        outer.addWidget(footnote)

        buttons = QHBoxLayout()
        self.remove_button = QPushButton("Remove shortcuts")
        self.remove_button.clicked.connect(self._remove)
        self.remove_button.setToolTip(
            "Remove only shortcuts previously created by Crucible for this server."
        )
        buttons.addWidget(self.remove_button)
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.create_button = QPushButton("Create / Update")
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self._create)
        buttons.addWidget(cancel)
        buttons.addWidget(self.create_button)
        outer.addLayout(buttons)

    def _destination(self) -> str:
        if self.apps_radio.isChecked():
            return "applications"
        if self.both_radio.isChecked():
            return "both"
        return "desktop"

    def _update_desktop_controls(self) -> None:
        visible = self.desktop_radio.isChecked() or self.both_radio.isChecked()
        self.desktop_path.setEnabled(visible)
        self.browse_button.setEnabled(visible)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose desktop shortcut folder",
            self.desktop_path.text() or str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self.desktop_path.setText(path)

    def _create(self) -> None:
        try:
            result = create_shortcuts(
                self.instance,
                destination=self._destination(),
                desktop_directory=self.desktop_path.text(),
                include_start=self.start_check.isChecked(),
                include_stop=self.stop_check.isChecked(),
            )
        except (ShortcutError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not create shortcuts", str(exc))
            return
        self.created_desktop_directory = (
            str(result.created[0].parent)
            if self._destination() in {"desktop", "both"} and result.created
            else None
        )
        lines = [f"Created or updated {len(result.created)} shortcut(s):"]
        lines.extend(f"• {path}" for path in result.created)
        QMessageBox.information(self, "Shortcuts ready", "\n".join(lines))
        self.accept()

    def _remove(self) -> None:
        directories = ()
        if self.desktop_path.text().strip():
            directories = (self.desktop_path.text().strip(),)
        try:
            result = remove_shortcuts(
                self.instance.id,
                desktop_directories=directories,
                include_applications=True,
            )
        except (ShortcutError, OSError) as exc:
            QMessageBox.warning(self, "Could not remove shortcuts", str(exc))
            return
        QMessageBox.information(
            self,
            "Shortcuts removed",
            f"Removed {len(result.removed)} Crucible shortcut(s). The server and its files were not changed.",
        )
        self.accept()
