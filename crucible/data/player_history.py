"""Lightweight recent-player history capture without constructing the Players UI.

The selected server's Console owns live join/leave parsing. Until the Players
page is opened, this small recorder preserves the existing players_seen.json
format and active-session timestamps. Reads are bounded; writes are coalesced
onto a short-lived background timer and explicitly flushed on stop/handoff.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


_MAX_PLAYER_JSON_BYTES = 16 * 1024 * 1024
_SAVE_DEBOUNCE_SECONDS = 0.5


class PlayerHistoryTracker:
    """Persist recent-player history without blocking every Console event."""

    def __init__(self, server_path: str | Path) -> None:
        self._path = Path(server_path) / ".crucible" / "players_seen.json"
        self._seen: dict[str, dict[str, Any]] | None = None
        self._join_times: dict[str, float] = {}
        self._load_error: str | None = None
        self._last_save_error: str | None = None
        self._dirty = False
        self._dirty_generation = 0
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._closed = False

    @property
    def join_times(self) -> dict[str, float]:
        """Return a snapshot of current sessions and their join timestamps."""
        with self._lock:
            return dict(self._join_times)

    @property
    def seen_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return an in-memory snapshot, including changes not yet persisted."""
        with self._lock:
            return {
                name: dict(record)
                for name, record in (self._seen or {}).items()
            }

    @property
    def load_error(self) -> str | None:
        """Return why the existing history could not be loaded, if applicable."""
        with self._lock:
            return self._load_error

    @property
    def dirty(self) -> bool:
        """Whether in-memory history changes still need a successful write."""
        with self._lock:
            return self._dirty

    def record_joined(self, name: str) -> None:
        self._record(name, joined=True)

    def record_left(self, name: str) -> None:
        self._record(name, joined=False)

    def record_server_stopped(self) -> None:
        """Finalize active sessions and synchronously flush one snapshot."""
        with self._lock:
            self._ensure_loaded()
            assert self._seen is not None
            now = time.time()
            for name, joined_at in tuple(self._join_times.items()):
                record = dict(self._seen.get(name) or {})
                record.setdefault("first_seen", now)
                record["last_seen"] = now
                try:
                    played = float(record.get("seconds_played", 0.0))
                except (TypeError, ValueError, OverflowError):
                    played = 0.0
                record["seconds_played"] = played + max(0.0, now - joined_at)
                self._seen[name] = record
                self._dirty_generation += 1
                self._dirty = True
            self._join_times.clear()
            self._cancel_timer_locked()
        self.flush()

    def flush(self) -> bool:
        """Cancel the debounce and publish pending data; retain dirty state on failure."""
        with self._lock:
            self._cancel_timer_locked()
            if self._load_error is not None and not self._retry_load_locked():
                return False
            if not self._dirty:
                return True
        # A handoff disconnects the event source before calling flush, so this
        # loop normally runs once. If a concurrent event slips in during the
        # atomic write, publish that newer generation too.
        while True:
            if not self._save():
                return False
            with self._lock:
                if not self._dirty:
                    return True
                self._cancel_timer_locked()

    def close(self) -> bool:
        """Stop accepting events, then flush; reopen capture if publication fails."""
        with self._lock:
            if self._closed:
                return True
            # Close the event gate before flushing so a concurrent callback
            # cannot dirty the snapshot in the gap between flush() and close.
            self._closed = True
            self._cancel_timer_locked()
        flushed = self.flush()
        if not flushed:
            with self._lock:
                self._closed = False
                self._schedule_save_locked()
        return flushed

    def _ensure_loaded(self) -> None:
        if self._seen is not None:
            return

        seen: dict[str, dict[str, Any]] = {}
        try:
            try:
                stat = self._path.stat()
            except FileNotFoundError:
                self._seen = seen
                return
            if stat.st_size > _MAX_PLAYER_JSON_BYTES:
                raise OSError("Player history exceeds 16 MiB safety limit")

            with self._path.open("rb") as stream:
                raw = stream.read(_MAX_PLAYER_JSON_BYTES + 1)
            if len(raw) > _MAX_PLAYER_JSON_BYTES:
                raise OSError("Player history exceeds 16 MiB safety limit")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or any(
                not isinstance(record, dict) for record in payload.values()
            ):
                raise ValueError("Player history has an invalid JSON schema")
            seen = payload
        except FileNotFoundError:
            # The file may disappear between stat and open. That is a genuine
            # missing-file case, not an unreadable existing source.
            pass
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._load_error = str(exc)
        self._seen = seen

    def _retry_load_locked(self) -> bool:
        """Retry an unavailable source while preserving the in-memory overlay."""
        memory = {
            name: dict(record)
            for name, record in (self._seen or {}).items()
        }
        self._seen = None
        self._load_error = None
        self._ensure_loaded()
        if self._load_error is not None:
            self._seen = memory
            return False

        disk = self._seen or {}
        for name, record in memory.items():
            merged = dict(disk.get(name) or {})
            # The in-memory record contains only events observed while the
            # original file was unavailable, not a copy of its prior totals.
            # Add those deltas to the now-readable disk snapshot instead of
            # taking max(), which would silently lose a new session for an
            # already-known player.
            for field in ("sessions", "seconds_played"):
                if field in record:
                    try:
                        merged[field] = float(merged.get(field, 0)) + float(
                            record[field]
                        )
                        if field == "sessions":
                            merged[field] = int(merged[field])
                    except (TypeError, ValueError, OverflowError):
                        merged[field] = record[field]
            for field in ("first_seen", "last_seen"):
                if field not in record:
                    continue
                current = merged.get(field)
                incoming = record[field]
                try:
                    if current is None:
                        merged[field] = incoming
                    elif field == "first_seen":
                        merged[field] = min(float(current), float(incoming))
                    else:
                        merged[field] = max(float(current), float(incoming))
                except (TypeError, ValueError, OverflowError):
                    merged[field] = incoming
            disk[name] = merged
        self._seen = disk
        self._dirty = True
        self._dirty_generation += 1
        return True

    def _record(self, name: str, *, joined: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._ensure_loaded()
            assert self._seen is not None
            now = time.time()
            record = dict(self._seen.get(name) or {})
            record.setdefault("first_seen", now)
            record["last_seen"] = now
            if joined:
                try:
                    sessions = int(record.get("sessions", 0))
                except (TypeError, ValueError, OverflowError):
                    sessions = 0
                record["sessions"] = sessions + 1
                self._join_times[name] = now
            else:
                joined_at = self._join_times.pop(name, None)
                if joined_at is not None:
                    try:
                        played = float(record.get("seconds_played", 0.0))
                    except (TypeError, ValueError, OverflowError):
                        played = 0.0
                    record["seconds_played"] = played + max(0.0, now - joined_at)
            self._seen[name] = record
            self._dirty_generation += 1
            self._dirty = True
            self._schedule_save_locked()

    def _cancel_timer_locked(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_save_locked(self) -> None:
        if self._closed or self._load_error is not None:
            return
        self._cancel_timer_locked()
        timer = threading.Timer(_SAVE_DEBOUNCE_SECONDS, self._run_scheduled_save)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _run_scheduled_save(self) -> None:
        with self._lock:
            self._timer = None
            if self._closed or self._load_error is not None:
                return
        self._save()

    def _save(self) -> bool:
        """Atomically publish one snapshot; never replace an unavailable source."""
        with self._write_lock:
            with self._lock:
                self._ensure_loaded()
                if self._load_error is not None or self._seen is None:
                    return False
                if not self._dirty:
                    return True
                generation = self._dirty_generation
                payload = json.dumps(self._seen, indent=2, ensure_ascii=False)

            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._path.with_suffix(".tmp")
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(self._path)
            except OSError as exc:
                with self._lock:
                    self._last_save_error = str(exc)
                return False

            with self._lock:
                self._last_save_error = None
                if generation == self._dirty_generation:
                    self._dirty = False
                else:
                    # A Console event arrived while the snapshot was being
                    # written. Keep it dirty and schedule one coalesced follow-up.
                    self._schedule_save_locked()
            return True
