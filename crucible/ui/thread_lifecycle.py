"""Safe finalization for short-lived PyQt QThreads."""
from __future__ import annotations
from collections.abc import Callable
from PyQt6.QtCore import QThread, QTimer

def connect_thread_cleanup(thread: QThread, callback: Callable[[], None]) -> None:
    """Release feature state only after Qt's native thread is fully stopped."""
    def finish_when_stopped() -> None:
        try:
            if thread.isRunning():
                QTimer.singleShot(10, finish_when_stopped)
                return
            thread.wait()
        except RuntimeError:
            pass
        callback()
        try:
            thread.deleteLater()
        except RuntimeError:
            pass
    thread.finished.connect(lambda: QTimer.singleShot(0, finish_when_stopped))
