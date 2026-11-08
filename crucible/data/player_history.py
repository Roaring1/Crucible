"""Lightweight recent-player history capture without constructing the Players UI.

The selected server's Console owns live join/leave parsing. Until the Players
page is opened, this small recorder preserves the existing ``players_seen.json``
format and active-session timestamps. The UI takes over the same events after
loading that file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


_MAX_PLAYER_JSON_BYTES = 16 * 1024 * 1024


class PlayerHistoryTracker:
    """Persist recent-player history while the Players page remains unopened."""

    def __init__(self, server_path: str | Path) -> None:
        self._path = Path(server_path) / ".crucible" / "players_seen.json"
        self._seen: dict[str, dict[str, Any]] | None = None
        self._join_times: dict[str, float] = {}

    @property
    def join_times(self) -> dict[str, float]:
        """Return a snapshot of current sessions and their join timestamps."""
        return dict(self._join_times)

    def record_joined(self, name: str) -> None:
        self._record(name, joined=True)

    def record_left(self, name: str) -> None:
        self._record(name, joined=False)

    def record_server_stopped(self) -> None:
        if not self._join_times:
            return
        self._ensure_loaded()
        assert self._seen is not None
        now = time.time()
        for name, joined_at in tuple(self._join_times.items()):
            record = self._seen.get(name) or {}
            record.setdefault("first_seen", now)
            record["last_seen"] = now
            record["seconds_played"] = float(
                record.get("seconds_played", 0.0)
            ) + max(0.0, now - joined_at)
            self._seen[name] = record
        self._join_times.clear()
        self._save()

    def _ensure_loaded(self) -> None:
        if self._seen is not None:
            return
        seen: dict[str, dict[str, Any]] = {}
        try:
            if self._path.stat().st_size > _MAX_PLAYER_JSON_BYTES:
                raise OSError("Player history exceeds 16 MiB safety limit")
            with self._path.open("rb") as stream:
                raw = stream.read(_MAX_PLAYER_JSON_BYTES + 1)
            if len(raw) > _MAX_PLAYER_JSON_BYTES:
                raise OSError("Player history exceeds 16 MiB safety limit")
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                seen = {
                    name: record
                    for name, record in payload.items()
                    if isinstance(name, str) and isinstance(record, dict)
                }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        self._seen = seen

    def _record(self, name: str, *, joined: bool) -> None:
        self._ensure_loaded()
        assert self._seen is not None
        now = time.time()
        record = self._seen.get(name) or {}
        record.setdefault("first_seen", now)
        record["last_seen"] = now
        if joined:
            record["sessions"] = int(record.get("sessions", 0)) + 1
            self._join_times[name] = now
        else:
            joined_at = self._join_times.pop(name, None)
            if joined_at is not None:
                record["seconds_played"] = float(
                    record.get("seconds_played", 0.0)
                ) + max(0.0, now - joined_at)
        self._seen[name] = record
        self._save()

    def _save(self) -> None:
        assert self._seen is not None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self._seen, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except OSError:
            # Match PlayersTab's best-effort history persistence behavior.
            pass
