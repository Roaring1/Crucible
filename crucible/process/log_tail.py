"""Bounded, rotation-aware binary tailing for Minecraft server logs.

This module is deliberately independent of Qt so its file-position and partial-line
rules can be tested directly. Positions are byte offsets, never text-stream
cookies. Only newline-terminated records are returned; an incomplete record is
held for the next poll within a strict memory bound.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

DEFAULT_MAX_READ_BYTES = 4 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 256 * 1024
DEFAULT_MAX_PARTIAL_BYTES = 1024 * 1024
_TRUNCATED_PREFIX = b"[Crucible: beginning of oversized log line omitted] "


@dataclass(frozen=True)
class TailRead:
    lines: list[str]
    rotated: bool
    bytes_read: int
    backlog: bool


class LogTailReader:
    """Incrementally read complete lines while bounding I/O and buffered data."""

    def __init__(self, *, max_partial_bytes: int = DEFAULT_MAX_PARTIAL_BYTES):
        if max_partial_bytes < len(_TRUNCATED_PREFIX) + 1:
            raise ValueError("max_partial_bytes is too small")
        self._max_partial_bytes = max_partial_bytes
        self._position = 0
        self._identity: tuple[int, int] | None = None
        self._partial = b""
        self._partial_was_truncated = False

    @property
    def position(self) -> int:
        return self._position

    @property
    def buffered_bytes(self) -> int:
        return len(self._partial)

    def reset(self) -> None:
        self._position = 0
        self._identity = None
        self._partial = b""
        self._partial_was_truncated = False

    def prime_tail(self, path: str | Path, *, max_bytes: int = 256 * 1024) -> None:
        """Start near the end of an existing log instead of replaying all history.

        The first visible record begins at a real newline boundary. This is
        specifically for GUI attachment: a large modpack latest.log can be
        hundreds of MB, and replaying it from byte zero floods the Qt event
        queue with formatted text and makes the whole window appear hung.
        """
        self.reset()
        path = Path(path)
        try:
            with path.open("rb") as fh:
                st = os.fstat(fh.fileno())
                self._identity = (st.st_dev, st.st_ino)
                start = max(0, st.st_size - max(0, max_bytes))
                if start:
                    fh.seek(start)
                    fh.readline()  # discard a partial first record
                    start = fh.tell()
                self._position = start
        except OSError:
            self.reset()

    def read(
        self,
        path: str | Path,
        *,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> TailRead:
        if max_read_bytes <= 0 or chunk_bytes <= 0:
            raise ValueError("read limits must be positive")

        path = Path(path)
        rotated = False
        chunks: list[bytes] = []
        bytes_read = 0

        # Opening first and fstat'ing the descriptor avoids mixing metadata from
        # one inode with bytes from a replacement inode.
        with path.open("rb") as fh:
            st = os.fstat(fh.fileno())
            identity = (st.st_dev, st.st_ino)
            replaced = self._identity is not None and identity != self._identity
            truncated = not replaced and st.st_size < self._position
            if replaced or truncated:
                rotated = True
                self._position = 0
                self._partial = b""
                self._partial_was_truncated = False
            self._identity = identity

            if self._position > st.st_size:
                # Defensive fallback for unusual filesystems with incoherent
                # metadata. It is safer to restart than to seek beyond the log.
                rotated = True
                self._position = 0
                self._partial = b""
                self._partial_was_truncated = False

            fh.seek(self._position)
            remaining = min(max_read_bytes, max(0, st.st_size - self._position))
            while remaining:
                block = fh.read(min(chunk_bytes, remaining))
                if not block:
                    break
                chunks.append(block)
                got = len(block)
                bytes_read += got
                remaining -= got
            self._position = fh.tell()
            backlog = self._position < st.st_size

        if not chunks:
            return TailRead([], rotated, 0, backlog)

        data = self._partial + b"".join(chunks)
        records = data.split(b"\n")
        self._partial = records.pop()

        # A malicious/corrupt log line without a newline must not grow memory
        # forever. Retain only its tail and make the omission explicit if it is
        # eventually displayed.
        if len(self._partial) > self._max_partial_bytes:
            keep = self._max_partial_bytes - len(_TRUNCATED_PREFIX)
            self._partial = _TRUNCATED_PREFIX + self._partial[-keep:]
            self._partial_was_truncated = True

        lines: list[str] = []
        for record in records:
            if record.endswith(b"\r"):
                record = record[:-1]
            if not record:
                continue
            lines.append(record.decode("utf-8", errors="replace"))

        # Once a truncated partial receives its newline, the prefix is already
        # part of the completed record and the state can be cleared.
        if records and self._partial_was_truncated and not self._partial:
            self._partial_was_truncated = False

        return TailRead(lines, rotated, bytes_read, backlog)
