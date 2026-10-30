"""Safe, bounded fetches for provider-hosted mod icons.

Icon URLs are untrusted API metadata. This module deliberately uses a
provider-specific HTTPS host allowlist, validates every redirect, never sends
provider credentials, and enforces socket timeouts and a strict response-byte cap.
It is separate from mod downloads and CurseForge's authenticated request path.
"""
from __future__ import annotations

import http.client
import math
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from crucible import __version__

_USER_AGENT = f"Crucible/{__version__} (mod icon)"
_MAX_ICON_BYTES = 8 * 1024 * 1024
_DEFAULT_TIMEOUT = 8.0
_MAX_ICON_TIMEOUT = 8.0
_READ_CHUNK_BYTES = 64 * 1024

# CurseForge media/icon authorization and retention terms remain unresolved.
# Keep this path disabled in code (not just hidden in the UI) until approved.
# Modrinth icon previews remain independently enabled.
_PROVIDER_HOSTS = {
    "modrinth": frozenset({"cdn.modrinth.com"}),
}
_DISABLED_PROVIDERS = frozenset({"curseforge"})


class IconFetchError(RuntimeError):
    """An icon URL or response could not be fetched safely."""


def icon_provider_enabled(provider: str) -> bool:
    """Whether remote icon fetching is currently approved for this provider."""
    return isinstance(provider, str) and provider.lower() in _PROVIDER_HOSTS


def _allowed_hosts(provider: str) -> frozenset[str]:
    if isinstance(provider, str) and provider.lower() in _DISABLED_PROVIDERS:
        raise IconFetchError(
            "CurseForge icon previews are disabled pending provider authorization."
        )
    try:
        return _PROVIDER_HOSTS[provider.lower()]
    except (AttributeError, KeyError) as exc:
        raise IconFetchError("Unknown icon provider.") from exc


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> None:
    if not isinstance(url, str) or not url or url != url.strip():
        raise IconFetchError("Invalid icon URL.")

    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise IconFetchError("Invalid icon URL.") from exc

    host = parts.hostname.lower() if parts.hostname else ""
    if (
        parts.scheme.lower() != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or host not in allowed_hosts
        or port not in (None, 443)
        or any(ord(ch) < 0x20 or ch.isspace() for ch in url)
    ):
        raise IconFetchError("Icon URL is outside the approved HTTPS hosts.")


class _SafeIconRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep icon redirects inside the provider's approved HTTPS host set."""

    def __init__(self, allowed_hosts: frozenset[str]):
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, self._allowed_hosts)
        # Rebuild the request rather than copying arbitrary headers from the
        # response path. Icon requests intentionally carry no API credentials.
        return urllib.request.Request(
            newurl,
            headers={"User-Agent": _USER_AGENT, "Accept": "image/*"},
            method="GET",
        )


def fetch_icon_bytes(url: str, *, provider: str, timeout: float = _DEFAULT_TIMEOUT) -> bytes:
    """Fetch bounded icon bytes from an approved provider CDN.

    Socket waits are capped at eight seconds. The body reader also checks a
    monotonic deadline between non-filling reads. urllib parses response
    headers internally, so that phase is bounded per socket wait rather than
    by a strict end-to-end wall-clock deadline. Errors never include the URL,
    response headers, or credentials in user-visible text.
    """
    allowed_hosts = _allowed_hosts(provider)
    _validate_url(url, allowed_hosts)

    if (
        not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise IconFetchError("Invalid icon request timeout.")
    timeout = min(float(timeout), _MAX_ICON_TIMEOUT)
    deadline = time.monotonic() + timeout

    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "image/*"},
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _SafeIconRedirectHandler(allowed_hosts),
    )

    try:
        with opener.open(request, timeout=timeout) as response:
            declared_length = response.headers.get("Content-Length", "")
            try:
                declared_length = int(declared_length)
            except (TypeError, ValueError):
                declared_length = None
            if declared_length is not None and (
                declared_length < 0 or declared_length > _MAX_ICON_BYTES
            ):
                raise IconFetchError("Icon response is too large.")

            # read1 returns currently available bytes rather than waiting to
            # fill a chunk. Recompute the deadline so a slow-drip response
            # cannot extend the total transfer time.
            reader = getattr(response, "read1", None) or response.read
            sock = getattr(
                getattr(getattr(response, "fp", None), "raw", None),
                "_sock",
                None,
            )
            chunks = []
            total = 0
            while total <= _MAX_ICON_BYTES:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise IconFetchError("Icon request timed out.")
                # urllib can expose a SocketIO wrapper whose SSLSocket is
                # already marked closed after headers are parsed. In that
                # case read1 still owns a readable buffer, but settimeout on
                # the stale socket wrapper raises EBADF; opener's socket
                # timeout plus the monotonic checks remain in force.
                if sock is not None and not getattr(sock, "_closed", False):
                    sock.settimeout(remaining_time)

                chunk = reader(min(_READ_CHUNK_BYTES, _MAX_ICON_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_ICON_BYTES:
                    raise IconFetchError("Icon response is too large.")
                if time.monotonic() >= deadline:
                    raise IconFetchError("Icon request timed out.")

            data = b"".join(chunks)
            if not data:
                raise IconFetchError("Icon response was empty.")
            return data
    except IconFetchError:
        raise
    except TimeoutError as exc:
        raise IconFetchError("Icon request timed out.") from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise IconFetchError("Icon request failed.") from exc
