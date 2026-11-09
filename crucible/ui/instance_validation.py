"""Read-only server-instance validation helpers for GUI worker threads."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from ..data.instance_model import ServerInstance


def validate_server_instance(candidate: ServerInstance) -> list[str]:
    """Return validation warnings, converting filesystem failures into visible issues."""
    try:
        return candidate.validate()
    except Exception as exc:  # noqa: BLE001 - filesystem failures cross worker boundaries
        return [f"Validation could not complete: {exc}"]


class ServerValidationWorker(QObject):
    """Run read-only instance validation away from the GUI thread."""

    finished = pyqtSignal(object, object)

    def __init__(self, candidate: ServerInstance):
        super().__init__()
        self._candidate = candidate

    def run(self) -> None:
        self.finished.emit(
            self._candidate, validate_server_instance(self._candidate)
        )
