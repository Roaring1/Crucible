"""Authenticated downloads without credentials in URLs or cross-host redirects.

CurseForge credentials are attached only to the explicit API/CDN allowlist.
No response caching, shared application key, or URL reconstruction lives here.
"""
from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request

# Expand only after confirming a host is an official CurseForge download endpoint.
_CURSEFORGE_HOSTS = frozenset({
    'api.curseforge.com', 'edge.forgecdn.net', 'mediafilez.forgecdn.net',
    'media.forgecdn.net',
})


class DownloadRequestError(ValueError):
    """An actionable, credential-free request error."""


def is_curseforge_url(url: str) -> bool:
    return urllib.parse.urlsplit(url).hostname in _CURSEFORGE_HOSTS


def _validate_url(url: str):
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443))
        query_names = {name.lower() for name, _ in urllib.parse.parse_qsl(parsed.query)}
    except ValueError as exc:
        raise DownloadRequestError('Invalid download address.') from exc
    if not valid:
        raise DownloadRequestError('Only HTTPS download addresses without embedded credentials are allowed.')
    if query_names & {'api-key', 'api_key', 'x-api-key'}:
        raise DownloadRequestError('API keys in download URLs are not allowed; use the private key setting.')


def make_request(url: str, *, headers=None) -> urllib.request.Request:
    _validate_url(url)
    safe_headers = {k: v for k, v in (headers or {}).items() if k.lower() != 'x-api-key'}
    if is_curseforge_url(url):
        from .mods.curseforge import ensure_online_integration_enabled, get_api_key
        from .mods.modrinth import ModrinthError
        try:
            ensure_online_integration_enabled()
        except ModrinthError as exc:
            raise DownloadRequestError(str(exc)) from exc
        key = get_api_key()
        if not key:
            raise DownloadRequestError('CurseForge requires an approved API key for browsing and downloads. Configure your own key, or download on the website and use Add .jar / import an installed Prism instance.')
        safe_headers['x-api-key'] = key
    return urllib.request.Request(url, headers=safe_headers)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        authenticated = any(k.lower() == 'x-api-key' for k in req.headers)
        if (authenticated or is_curseforge_url(req.full_url)) and not is_curseforge_url(newurl):
            raise DownloadRequestError('Blocked a CurseForge redirect outside the approved API/CDN hosts. No credentials were forwarded.')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # Rebuild rather than trusting urllib to propagate sensitive headers.
            redirected = make_request(redirected.full_url, headers=redirected.headers)
        return redirected


def build_opener():
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        SafeRedirectHandler(),
    )


def open_url(request, *, timeout):
    return build_opener().open(request, timeout=timeout)


def http_error_message(code: int, *, curseforge: bool = False) -> str:
    service = 'CurseForge' if curseforge else 'The download service'
    if code == 401:
        return service + ' rejected authentication (HTTP 401). Check that your own API key is valid and approved.'
    if code == 403:
        return service + ' denied this request (HTTP 403). Access or author distribution may be restricted; use the project website for permitted manual downloads.'
    if code == 429:
        return service + ' is rate-limiting requests (HTTP 429). Wait before trying again.'
    if code >= 500:
        return service + f' is temporarily unavailable (HTTP {code}). Try again later.'
    return service + f' returned HTTP {code}. No replacement was installed.'
