"""Gofile upload adapter.

Uses Gofile's public upload endpoint in guest mode: no API token is needed for
a one-off upload. This keeps provider credentials out of Crucible and the ZIP.
The endpoint is beta (https://gofile.io/api); responses are validated and all
downloads remain local.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import mimetypes
import os
import re
import secrets
import ssl
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit


_UPLOAD_HOST = "upload.gofile.io"
_UPLOAD_PATH = "/uploadfile"
_USER_AGENT = "Crucible/0.8.0 (client-pack sharing)"
_CHUNK_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_DOWNLOAD_HOSTS = frozenset({"gofile.io", "www.gofile.io"})


class GofileUploadError(RuntimeError):
    """An upload could not be completed or validated."""


class UploadCancelled(GofileUploadError):
    """The user requested cancellation."""


@dataclass(frozen=True)
class GofileUploadResult:
    download_url: str
    file_id: str
    file_name: str
    file_size: int
    local_sha256: str
    remote_md5: str
    size_verified: bool
    md5_verified: bool


def sha256_file(
    path: str | Path,
    *,
    cancel_event: threading.Event | None = None,
    chunk_size: int = _CHUNK_BYTES,
) -> str:
    """Hash a file in bounded memory, optionally cancellable."""
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while True:
            if cancel_event and cancel_event.is_set():
                raise UploadCancelled("Upload cancelled before it started.")
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_download_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise GofileUploadError("Gofile returned an invalid download link.")
    if value != value.strip() or any(not 0x21 <= ord(char) <= 0x7E for char in value):
        raise GofileUploadError("Gofile returned a download link containing whitespace or control characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise GofileUploadError("Gofile returned a malformed download link.") from exc
    path_parts = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DOWNLOAD_HOSTS
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or len(path_parts) != 3
        or path_parts[1] != "d"
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", path_parts[2])
        or parsed.query
        or parsed.fragment
    ):
        raise GofileUploadError("Gofile returned a download link outside its expected HTTPS download-page format.")
    return value


def _decode_response(raw: bytes, status_code: int, expected_size: int,
                     local_md5: str, local_sha256: str) -> GofileUploadResult:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise GofileUploadError("Gofile returned an unexpectedly large response.")
    if status_code < 200 or status_code >= 300:
        if status_code == 413:
            raise GofileUploadError(
                "Gofile rejected the file size. Its current upload limit may differ from published plan summaries; the local ZIP is preserved."
            )
        if status_code == 429:
            raise GofileUploadError("Gofile rate-limited the upload. Keep the local ZIP and retry later.")
        if status_code in (401, 403):
            raise GofileUploadError(
                "Gofile declined the upload. Check the service policy; if your account requires authentication, enable the optional owner API token and retry."
            )
        raise GofileUploadError(f"Gofile upload failed with HTTP {status_code}.")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GofileUploadError(
            f"Gofile returned an unreadable response (HTTP {status_code})."
        ) from exc

    if not isinstance(payload, dict) or payload.get("status") != "ok":
        # Treat response fields as untrusted. Do not echo arbitrary provider text
        # into the UI, where it could include reflected request credentials.
        raise GofileUploadError("Gofile did not confirm the upload (response status was not 'ok').")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise GofileUploadError("Gofile's response did not contain file information.")

    download_url = _safe_download_url(data.get("downloadPage"))
    file_id = data.get("id") or data.get("fileId") or ""
    file_name = data.get("name") or data.get("fileName") or ""
    if not isinstance(file_id, str) or not file_id or len(file_id) > 256:
        raise GofileUploadError("Gofile did not return a valid file ID.")
    if not isinstance(file_name, str) or not file_name or len(file_name) > 255:
        raise GofileUploadError("Gofile did not return a valid file name.")

    remote_size = data.get("size")
    size_verified = False
    if remote_size is not None:
        if isinstance(remote_size, bool) or not isinstance(remote_size, int) or remote_size < 0:
            raise GofileUploadError("Gofile returned an invalid file-size value.")
        size_verified = True
        if remote_size != expected_size:
            raise GofileUploadError(
                "Gofile's reported file size differs from the local ZIP. The uploaded link was not accepted as verified."
            )

    remote_md5_value = data.get("md5")
    if remote_md5_value is None or remote_md5_value == "":
        remote_md5 = ""
        md5_verified = False
    else:
        if not isinstance(remote_md5_value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", remote_md5_value):
            raise GofileUploadError("Gofile returned an invalid MD5 value.")
        remote_md5 = remote_md5_value
        md5_verified = True
        if remote_md5.lower() != local_md5.lower():
            raise GofileUploadError(
                "Gofile's reported MD5 differs from the locally uploaded file. Keep the local ZIP and do not share this link yet."
            )

    return GofileUploadResult(
        download_url=download_url,
        file_id=file_id,
        file_name=file_name,
        file_size=expected_size,
        local_sha256=local_sha256,
        remote_md5=remote_md5,
        size_verified=size_verified,
        md5_verified=md5_verified,
    )


def _file_signature(info) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def upload_file(
    path: str | Path,
    *,
    expected_sha256: str,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    timeout: float = 30.0,
    api_token: str = "",
) -> GofileUploadResult:
    """Stream one reviewed file to Gofile and validate its response.

    The same open file descriptor is hashed and then streamed, so replacing the
    path between preflight and transfer does not silently switch the artifact.
    Guest mode intentionally omits Authorization and discards any guest token
    in the response. api_token is optional and is never persisted or returned.
    Progress callbacks receive (uploaded_file_bytes, total_file_bytes).
    """
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise GofileUploadError("The local ZIP is missing or is a symbolic link.")
    if not expected_sha256 or len(expected_sha256) != 64:
        raise GofileUploadError("The expected archive SHA-256 is missing or invalid.")
    token = api_token.strip()
    if len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise GofileUploadError("The optional Gofile API token contains invalid characters.")
    if cancel_event and cancel_event.is_set():
        raise UploadCancelled("Upload cancelled before it started.")

    connection = None
    try:
        # Use O_NOFOLLOW where supported and compare the opened inode with lstat
        # to protect against replacing the selected local artifact with a link.
        path_info = source.lstat()
        if not stat.S_ISREG(path_info.st_mode):
            raise GofileUploadError("The local ZIP is not a regular file.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or _file_signature(path_info) != _file_signature(before):
                raise GofileUploadError("The local ZIP changed while it was being opened. Review it before uploading.")
            total = before.st_size
            if total <= 0:
                raise GofileUploadError("The local ZIP is empty.")

            digest = hashlib.sha256()
            while True:
                if cancel_event and cancel_event.is_set():
                    raise UploadCancelled("Upload cancelled before it started.")
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
            local_sha256 = digest.hexdigest()
            after_hash = os.fstat(stream.fileno())
            if _file_signature(before) != _file_signature(after_hash):
                raise GofileUploadError("The local ZIP changed while its checksum was being checked. Review it before uploading.")
            if local_sha256.lower() != expected_sha256.lower():
                raise GofileUploadError("The local ZIP changed after it was built. Rebuild and review it before uploading.")
            if cancel_event and cancel_event.is_set():
                raise UploadCancelled("Upload cancelled before it started.")
            stream.seek(0)

            filename = source.name.replace("\r", "").replace("\n", "")
            if not filename:
                filename = "player-pack.zip"
            ascii_fallback = "".join(
                ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\", ";"} else "_"
                for ch in filename
            )
            encoded_name = quote(filename, safe="")
            boundary = "----CrucibleUpload" + secrets.token_hex(16)
            file_type = mimetypes.guess_type(filename)[0] or "application/zip"
            part_head = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{encoded_name}\r\n"
                f"Content-Type: {file_type}\r\n\r\n"
            ).encode("utf-8")
            part_tail = f"\r\n--{boundary}--\r\n".encode("ascii")
            content_length = len(part_head) + total + len(part_tail)

            connection = http.client.HTTPSConnection(
                _UPLOAD_HOST,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
            local_md5 = hashlib.md5(usedforsecurity=False)
            streamed_sha256 = hashlib.sha256()
            uploaded = 0
            connection.connect()
            if connection.sock:
                connection.sock.settimeout(timeout)
            # putrequest() emits the single Host header automatically.
            connection.putrequest("POST", _UPLOAD_PATH)
            connection.putheader("User-Agent", _USER_AGENT)
            connection.putheader("Accept", "application/json")
            if token:
                connection.putheader("Authorization", f"Bearer {token}")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(content_length))
            connection.putheader("Connection", "close")
            connection.endheaders()
            connection.send(part_head)

            if progress_cb:
                progress_cb(0, total)
            while True:
                if cancel_event and cancel_event.is_set():
                    raise UploadCancelled("Upload cancelled. The local ZIP is preserved.")
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                connection.send(chunk)
                local_md5.update(chunk)
                streamed_sha256.update(chunk)
                uploaded += len(chunk)
                if progress_cb:
                    progress_cb(uploaded, total)

            if uploaded != total:
                raise GofileUploadError("The local ZIP changed size during upload. Keep the local file and review it before retrying.")
            if streamed_sha256.hexdigest().lower() != expected_sha256.lower():
                raise GofileUploadError(
                    "The bytes sent no longer match the reviewed ZIP SHA-256. Do not share this upload; rebuild and review a stable archive."
                )
            after_upload = os.fstat(stream.fileno())
            if _file_signature(before) != _file_signature(after_upload):
                raise GofileUploadError("The local ZIP changed during upload. Keep it locally and do not share this upload.")
            # Check cancellation before completing the multipart request. Once
            # the final boundary is sent, Gofile may have accepted the public
            # upload and a local cancellation cannot retract it.
            if cancel_event and cancel_event.is_set():
                raise UploadCancelled("Upload cancelled. The local ZIP is preserved.")
            connection.send(part_tail)

            response = connection.getresponse()
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise GofileUploadError("Gofile returned an unexpectedly large response.")
            return _decode_response(
                raw,
                response.status,
                expected_size=total,
                local_md5=local_md5.hexdigest(),
                local_sha256=local_sha256,
            )
    except (UploadCancelled, GofileUploadError):
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError, UnicodeError) as exc:
        raise GofileUploadError(
            "Could not complete the Gofile upload. The local ZIP is preserved; retry the same file after checking your connection."
        ) from exc
    finally:
        if connection is not None:
            connection.close()


class GofileProvider:
    """Gofile implementation of the shared upload-provider contract."""

    name = "Gofile"

    def __init__(self, timeout: float = 30.0, api_token: str = ""):
        self._timeout = timeout
        self._api_token = api_token.strip()

    def upload_file(
        self,
        path: str | Path,
        *,
        expected_sha256: str,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> GofileUploadResult:
        return upload_file(
            path,
            expected_sha256=expected_sha256,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            timeout=self._timeout,
            api_token=self._api_token,
        )
