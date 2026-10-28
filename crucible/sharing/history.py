"""Bounded local event history for player-pack preparation and Gofile sharing.

The history schema is an explicit allowlist. Provider tokens, HTTP headers,
raw exceptions, request/response bodies, and arbitrary metadata are never stored.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
MAX_RECORDS = 50
MAX_FILE_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENTS = frozenset({
    "pack-prepared",
    "build-failed",
    "upload-started",
    "upload-succeeded",
    "upload-failed",
    "upload-cancelled",
})
_ERROR_CODES = frozenset({
    "",
    "build-failed",
    "upload-failed",
    "unexpected-upload-error",
    "cancelled",
})
_UPLOAD_STATUSES = frozenset({
    "not-attempted",
    "uploading",
    "succeeded",
    "failed",
    "cancelled",
})


class ShareHistoryError(ValueError):
    """History is malformed, unsafe to read, or cannot be stored safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value, *, max_length: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    value = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)
    value = value.strip()
    return value[:max_length]


def _sha256(value) -> str:
    if isinstance(value, str) and _SHA256_RE.fullmatch(value):
        return value
    return ""


def _safe_share_url(value) -> str:
    """Keep only the public Gofile download-page shape, never tokens or URL params."""
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip():
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    parts = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"gofile.io", "www.gofile.io"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or len(parts) != 3
        or parts[1] != "d"
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[2])
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        return ""
    return value


def _validate_record(value) -> dict:
    if not isinstance(value, dict):
        raise ShareHistoryError("history contains a non-object record")
    event_id = value.get("eventId")
    timestamp = value.get("timestampUtc")
    event = value.get("event")
    status = value.get("uploadStatus")
    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id):
        raise ShareHistoryError("history event identifier is invalid")
    if not isinstance(timestamp, str) or len(timestamp) > 40:
        raise ShareHistoryError("history timestamp is invalid")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShareHistoryError("history timestamp is invalid") from exc
    if (
        not isinstance(event, str)
        or event not in _EVENTS
        or not isinstance(status, str)
        or status not in _UPLOAD_STATUSES
    ):
        raise ShareHistoryError("history event/status is invalid")
    size = value.get("sizeBytes", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ShareHistoryError("history file size is invalid")
    for key in ("sizeVerified", "md5Verified"):
        if not isinstance(value.get(key, False), bool):
            raise ShareHistoryError(f"history field {key} is invalid")
    error_code = value.get("errorCode", "")
    if not isinstance(error_code, str) or error_code not in _ERROR_CODES:
        raise ShareHistoryError("history error code is invalid")
    link = value.get("shareUrl", "")
    if link and _safe_share_url(link) != link:
        raise ShareHistoryError("history contains an invalid share URL")
    archive_sha = value.get("archiveSha256", "")
    input_fingerprint = value.get("inputFingerprint", "")
    if archive_sha and not _sha256(archive_sha):
        raise ShareHistoryError("history archive checksum is invalid")
    if input_fingerprint and not _sha256(input_fingerprint):
        raise ShareHistoryError("history input fingerprint is invalid")

    # Rebuild the object from an allowlist. Unknown JSON keys (including a
    # maliciously added token field) never survive a read/write cycle.
    local_path = _safe_text(value.get("localPath"), max_length=4096)
    file_name = _safe_text(value.get("fileName"), max_length=255)
    if local_path and file_name and Path(local_path).name != file_name:
        raise ShareHistoryError("history filename does not match its local path")
    return {
        "eventId": event_id,
        "timestampUtc": timestamp,
        "instanceName": _safe_text(value.get("instanceName"), max_length=200, default="Unknown server"),
        "event": event,
        "archiveSha256": archive_sha,
        "inputFingerprint": input_fingerprint,
        "localPath": local_path,
        "fileName": file_name,
        "sizeBytes": size,
        "provider": "Gofile" if value.get("provider") == "Gofile" else "",
        "shareUrl": link,
        "uploadStatus": status,
        "sizeVerified": value.get("sizeVerified", False),
        "md5Verified": value.get("md5Verified", False),
        "errorCode": error_code,
    }


class ShareHistoryStore:
    """A bounded, atomic, owner-private event file. It never deletes ZIP files."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _ensure_directory(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise ShareHistoryError("share-history directory is a symbolic link")
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if parent.is_symlink() or not parent.is_dir():
                raise ShareHistoryError("share-history path is not a real directory")
            if os.name == "posix":
                parent.chmod(0o700)
        except OSError as exc:
            raise ShareHistoryError("share-history directory cannot be secured") from exc

    def load(self) -> list[dict]:
        if not self.path.exists():
            if self.path.is_symlink():
                raise ShareHistoryError("share-history file is a symbolic link")
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise ShareHistoryError("share-history path is not a regular file")
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ShareHistoryError("share-history file exceeds its size limit")
            if os.name == "posix":
                self.path.chmod(0o600)
            raw = self.path.read_bytes()
        except OSError as exc:
            raise ShareHistoryError("share-history file cannot be read safely") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise ShareHistoryError("share-history file exceeds its size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShareHistoryError("share-history file is malformed") from exc
        if (
            not isinstance(payload, dict)
            or isinstance(payload.get("schemaVersion"), bool)
            or payload.get("schemaVersion") != SCHEMA_VERSION
            or not isinstance(payload.get("records"), list)
            or len(payload["records"]) > MAX_RECORDS
        ):
            raise ShareHistoryError("share-history schema is unsupported or oversized")
        records = [_validate_record(item) for item in payload["records"]]
        # A corrupt file must not be able to spoof duplicate identifiers.
        ids = [item["eventId"] for item in records]
        if len(ids) != len(set(ids)):
            raise ShareHistoryError("share-history contains duplicate event identifiers")
        return records

    def _save(self, records: list[dict]) -> None:
        self._ensure_directory()
        clean_records = [_validate_record(item) for item in records[-MAX_RECORDS:]]
        payload = json.dumps(
            {"schemaVersion": SCHEMA_VERSION, "records": clean_records},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        if len(payload.encode("utf-8")) > MAX_FILE_BYTES:
            raise ShareHistoryError("share-history data exceeds its size limit")

        if self.path.is_symlink():
            raise ShareHistoryError("share-history file is a symbolic link")
        temporary_name = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".share-history-", suffix=".tmp", dir=str(self.path.parent)
            )
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
            temporary_name = None
        except OSError as exc:
            raise ShareHistoryError("share-history could not be stored safely") from exc
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    def append(
        self,
        *,
        event: str,
        instance_name: str,
        archive_sha256: str = "",
        input_fingerprint: str = "",
        local_path: str | Path = "",
        size_bytes: int = 0,
        provider: str = "",
        share_url: str = "",
        upload_status: str = "not-attempted",
        size_verified: bool = False,
        md5_verified: bool = False,
        error_code: str = "",
    ) -> dict:
        if not isinstance(event, str) or event not in _EVENTS:
            raise ShareHistoryError("unsupported share-history event")
        if (
            not isinstance(upload_status, str)
            or upload_status not in _UPLOAD_STATUSES
            or not isinstance(error_code, str)
            or error_code not in _ERROR_CODES
        ):
            raise ShareHistoryError("unsupported share-history status")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ShareHistoryError("invalid share-history file size")
        if not isinstance(size_verified, bool) or not isinstance(md5_verified, bool):
            raise ShareHistoryError("invalid remote-verification flags")
        safe_url = _safe_share_url(share_url) if share_url else ""
        if share_url and not safe_url:
            raise ShareHistoryError("refusing to persist an invalid Gofile share URL")
        if archive_sha256 and not _sha256(archive_sha256):
            raise ShareHistoryError("invalid archive SHA-256")
        if input_fingerprint and not _sha256(input_fingerprint):
            raise ShareHistoryError("invalid input fingerprint")
        raw_path = str(local_path) if local_path else ""
        if raw_path and any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_path):
            raw_path = ""

        record = _validate_record({
            "eventId": uuid.uuid4().hex,
            "timestampUtc": _now(),
            "instanceName": _safe_text(instance_name, max_length=200, default="Unknown server"),
            "event": event,
            "archiveSha256": archive_sha256,
            "inputFingerprint": input_fingerprint,
            "localPath": raw_path,
            "fileName": Path(raw_path).name if raw_path else "",
            "sizeBytes": size_bytes,
            "provider": provider,
            "shareUrl": safe_url,
            "uploadStatus": upload_status,
            "sizeVerified": size_verified,
            "md5Verified": md5_verified,
            "errorCode": error_code,
        })
        records = self.load()
        records.append(record)
        self._save(records)
        return record

    def clear(self) -> None:
        """Forget event records only; never deletes local ZIP files or remote links."""
        self._save([])
