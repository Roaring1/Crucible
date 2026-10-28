"""Small interface for hosted file providers."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class UploadProvider(Protocol):
    """Provider contract consumed by the share dialog."""

    name: str

    def upload_file(
        self,
        path: str | Path,
        *,
        expected_sha256: str,
        progress_cb: Callable[[int, int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        """Upload one reviewed archive and return a validated share result."""
        ...
