"""
crucible/ui/tabs/notes_tab.py

Per-instance notes with 500ms debounce auto-save.
Plain text — no markdown rendering needed at this stage.
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QLabel, QHBoxLayout

from ...data.instance_manager import InstanceManager
from ...data.instance_model import ServerInstance
from .. import theme

DEBOUNCE_MS = 500


class NotesTab(QWidget):
    """Auto-saving plain-text notes for one server instance."""

    def __init__(self, manager: InstanceManager, parent=None):
        super().__init__(parent)
        self._manager:  InstanceManager       = manager
        self._instance: ServerInstance | None = None

        self._dirty       = False
        self._save_timer  = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save)

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 8)
        layout.setSpacing(4)

        header = QLabel("Notes")
        header.setObjectName("SectionLabel")
        layout.addWidget(header)

        self._editor = QTextEdit()
        self._editor.setPlaceholderText(
            "Free-form notes about this server instance.\n"
            "Saved automatically as you type."
        )
        self._editor.textChanged.connect(self._on_changed)
        layout.addWidget(self._editor, stretch=1)

        # Footer: last-saved status
        footer = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
        footer.addStretch()
        footer.addWidget(self._status)
        layout.addLayout(footer)

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, instance: ServerInstance) -> None:
        """Load notes for a new instance, flushing any pending save first."""
        if self._dirty:
            self._save()

        self._instance = instance
        self._editor.blockSignals(True)
        self._editor.setPlainText(instance.notes or "")
        self._editor.blockSignals(False)
        self._dirty = False
        self._status.setText("")

    def flush(self) -> None:
        """Force an immediate save if dirty (call before switching instances)."""
        if self._dirty:
            self._save_timer.stop()
            self._save()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _on_changed(self) -> None:
        self._dirty = True
        self._status.setText("Unsaved…")
        self._save_timer.start(DEBOUNCE_MS)

    def _save(self) -> None:
        if self._instance is None:
            return
        self._instance.notes = self._editor.toPlainText()
        try:
            self._manager.update_instance(self._instance)
            self._status.setText("Saved")
        except Exception as exc:
            self._status.setText(f"Save failed: {exc}")
        self._dirty = False
