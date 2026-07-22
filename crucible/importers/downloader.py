"""
crucible/importers/downloader.py

Best-effort mod downloader for Modrinth (.mrpack) and CurseForge (manifest.json)
packs that ship only a *download index* instead of the actual mod jars.

Why this is "best-effort":
  * Modrinth indexes contain direct CDN URLs + hashes, so downloads usually work
    when the machine has internet access.
  * CurseForge indexes contain only numeric projectID/fileID pairs.  Resolving
    those to a real download URL needs either the official CurseForge API
    (an API key in the CURSEFORGE_API_KEY env var) or a public redirect that
    CurseForge may rate-limit or block.  Many third-party authors also disable
    API distribution entirely, so some files simply cannot be fetched
    automatically.  Every failure is caught and reported instead of crashing.

Design goals:
  * NEVER raise out of the public API — return a structured DownloadResult.
  * Verify Modrinth hashes (sha512 preferred, sha1 fallback) and delete
    corrupt downloads.
  * Skip client-only files (env.server == "unsupported").
  * Stream to a temp file then atomically rename, so a half-written jar never
    looks complete.
  * Report progress and per-file status through optional callbacks so a GUI
    progress dialog or the CLI can show what is happening.
  * Be cancellable via a threading.Event.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Public, polite identification.  Some CDNs reject the default urllib UA.
_USER_AGENT = "Crucible-ServerManager/0.4.1 (+https://github.com/; minecraft server hoster)"
_DEFAULT_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 2.0
_CHUNK = 64 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024

ProgressCb = Callable[[int, int], None]      # (completed_files, total_files)
LogCb = Callable[[str], None]                # human-readable status line


@dataclass
class DownloadItem:
    """One file we intend to fetch."""
    name: str                       # display name / target filename
    rel_path: str                   # path relative to the server folder (e.g. mods/foo.jar)
    urls: list[str] = field(default_factory=list)
    sha512: str = ""
    sha1: str = ""
    size: int = 0
    source: str = ""                # modrinth / curseforge
    # CurseForge-only resolution hints
    project_id: int = 0
    file_id: int = 0


@dataclass
class DownloadResult:
    downloaded: list[str] = field(default_factory=list)
    skipped_client: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (name, reason)
    messages: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total_attempted(self) -> int:
        return len(self.downloaded) + len(self.failed)

    def summary(self) -> str:
        parts = [f"{len(self.downloaded)} downloaded"]
        if self.already_present:
            parts.append(f"{len(self.already_present)} already present")
        if self.skipped_client:
            parts.append(f"{len(self.skipped_client)} client-only skipped")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.cancelled:
            parts.append("cancelled")
        return ", ".join(parts)


class _Cancelled(Exception):
    pass


def _is_cancelled(cancel) -> bool:
    try:
        return bool(cancel is not None and cancel.is_set())
    except Exception:
        return False


def _opener() -> urllib.request.OpenerDirector:
    # Always verify TLS. A missing/broken CA store must fail closed rather than
    # silently accepting a forged Modrinth/CurseForge response.
    ctx = ssl.create_default_context()
    handler = urllib.request.HTTPSHandler(context=ctx)
    opener = urllib.request.build_opener(handler)
    opener.addheaders = [("User-Agent", _USER_AGENT), ("Accept", "*/*")]
    return opener


def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(path: Path, item: DownloadItem) -> bool:
    """Return True if the file matches a known hash, or if no hash is available."""
    try:
        if item.sha512:
            return _hash_file(path, "sha512").lower() == item.sha512.lower()
        if item.sha1:
            return _hash_file(path, "sha1").lower() == item.sha1.lower()
    except OSError:
        return False
    # No hash to check against -- accept a non-empty file.
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _download_one(item: DownloadItem, dest: Path, opener, timeout: int,
                  log: LogCb | None, cancel) -> None:
    """Download a single item to dest.  Raises on unrecoverable failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    urls = []
    for url in item.urls:
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            continue
        if parsed.scheme == "https" and parsed.hostname:
            urls.append(url)
    if not urls:
        raise RuntimeError("no safe HTTPS download URL available")

    last_err: str = ""
    for url in urls:
        for attempt in range(1, _MAX_RETRIES + 1):
            if _is_cancelled(cancel):
                raise _Cancelled()
            tmp_fd, tmp_name = tempfile.mkstemp(prefix=".crucible-dl-", dir=str(dest.parent))
            tmp_path = Path(tmp_name)
            try:
                os.close(tmp_fd)
                req = urllib.request.Request(url)
                with opener.open(req, timeout=timeout) as resp, tmp_path.open("wb") as out:
                    limit = min(item.size, _MAX_FILE_BYTES) if item.size > 0 else _MAX_FILE_BYTES
                    declared = resp.headers.get("Content-Length")
                    if declared and int(declared) > limit:
                        raise RuntimeError("download exceeds safe size limit")
                    received = 0
                    while True:
                        if _is_cancelled(cancel):
                            raise _Cancelled()
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > limit:
                            raise RuntimeError("download exceeds safe size limit")
                        out.write(chunk)
                if not _verify(tmp_path, item):
                    last_err = "hash/size verification failed"
                    tmp_path.unlink(missing_ok=True)
                    if log:
                        log(f"   ! {item.name}: verification failed (attempt {attempt})")
                    time.sleep(_RETRY_BACKOFF_S * attempt)
                    continue
                # Success -- move into place atomically.
                tmp_path.replace(dest)
                return
            except _Cancelled:
                tmp_path.unlink(missing_ok=True)
                raise
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
                tmp_path.unlink(missing_ok=True)
                last_err = f"{type(exc).__name__}: {exc}"
                if log:
                    log(f"   ! {item.name}: {last_err} (attempt {attempt}/{_MAX_RETRIES})")
                time.sleep(_RETRY_BACKOFF_S * attempt)
            except Exception as exc:  # pragma: no cover - defensive catch-all
                tmp_path.unlink(missing_ok=True)
                last_err = f"unexpected {type(exc).__name__}: {exc}"
                if log:
                    log(f"   ! {item.name}: {last_err}")
                time.sleep(_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(last_err or "all download URLs failed")


# Index parsing

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


def _modrinth_items(index: dict) -> list[DownloadItem]:
    items: list[DownloadItem] = []
    for entry in index.get("files", []) or []:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("path", "")).replace("\\", "/").lstrip("/")
        if not rel:
            continue
        env = entry.get("env", {}) if isinstance(entry.get("env"), dict) else {}
        hashes = entry.get("hashes", {}) if isinstance(entry.get("hashes"), dict) else {}
        item = DownloadItem(
            name=Path(rel).name,
            rel_path=rel,
            urls=[u for u in (entry.get("downloads") or []) if isinstance(u, str)],
            sha512=str(hashes.get("sha512", "")),
            sha1=str(hashes.get("sha1", "")),
            size=int(entry.get("fileSize", 0) or 0),
            source="modrinth",
        )
        # Tag client-only via a sentinel URL list emptiness check later.
        item_env_server = str(env.get("server", "")).lower()
        if item_env_server == "unsupported":
            item.urls = []   # mark as skip-client downstream
            item.source = "modrinth-client"
        items.append(item)
    return items


def _curseforge_items(manifest: dict, api_key: str) -> tuple[list[DownloadItem], list[str]]:
    """Resolve CurseForge file IDs to download URLs.

    Returns (items, notes).  Resolution strategy, in order:
      1. Official API (needs CURSEFORGE_API_KEY) -> exact downloadUrl + fileName.
      2. Public v1 redirect endpoint (no key, may be blocked/rate-limited).
    """
    notes: list[str] = []
    items: list[DownloadItem] = []
    opener = _opener()

    raw_files = manifest.get("files", []) or []
    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        try:
            project_id = int(entry.get("projectID", 0))
            file_id = int(entry.get("fileID", 0))
        except (TypeError, ValueError):
            continue
        if not project_id or not file_id:
            continue

        name = f"curseforge-{project_id}-{file_id}.jar"
        urls: list[str] = []

        if api_key:
            try:
                req = urllib.request.Request(
                    "https://api.curseforge.com/v1/mods/%d/files/%d" % (project_id, file_id),
                    headers={"x-api-key": api_key, "Accept": "application/json",
                             "User-Agent": _USER_AGENT},
                )
                with opener.open(req, timeout=_DEFAULT_TIMEOUT) as resp:
                    data = json.loads(resp.read(16 * 1024 * 1024 + 1).decode("utf-8", errors="replace"))
                file_data = data.get("data", {}) if isinstance(data, dict) else {}
                file_name = str(file_data.get("fileName", "")) or name
                download_url = file_data.get("downloadUrl")
                name = file_name
                if download_url:
                    urls.append(str(download_url))
                else:
                    # downloadUrl can be null when the author blocks API distribution;
                    # reconstruct the well-known edge CDN path from the numeric id.
                    fid = str(file_id)
                    urls.append(
                        "https://edge.forgecdn.net/files/%s/%s/%s" % (fid[:4], fid[4:], file_name)
                    )
            except Exception as exc:
                notes.append(f"CurseForge API lookup failed for {project_id}/{file_id}: {exc}")

        # Public redirect fallback (works without an API key surprisingly often,
        # but CurseForge may return 403/Cloudflare challenges).
        urls.append(
            "https://www.curseforge.com/api/v1/mods/%d/files/%d/download" % (project_id, file_id)
        )

        items.append(DownloadItem(
            name=name,
            rel_path=f"mods/{name}",
            urls=urls,
            source="curseforge",
            project_id=project_id,
            file_id=file_id,
        ))

    if not api_key and items:
        notes.append(
            "No CURSEFORGE_API_KEY set — using the public download redirect, which "
            "CurseForge often rate-limits or blocks. For reliable CurseForge downloads, "
            "set a free API key in the CURSEFORGE_API_KEY environment variable, or import "
            "a fully installed Prism instance instead."
        )
    return items, notes


def find_pack_index(target: str | Path) -> Path | None:
    """Locate the stored pack index for a previously imported server folder."""
    target = Path(target).expanduser()
    candidates = [
        target / ".crucible" / "source-pack" / "modrinth.index.json",
        target / ".crucible" / "source-pack" / "manifest.json",
        target / "modrinth.index.json",
        target / "manifest.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def has_downloadable_index(target: str | Path) -> bool:
    return find_pack_index(target) is not None


def build_items(index_path: Path) -> tuple[list[DownloadItem], list[str]]:
    """Parse an index file into download items.  Returns (items, notes)."""
    notes: list[str] = []
    data = _read_json(index_path)
    if not data:
        return [], [f"Could not parse pack index: {index_path}"]

    is_modrinth = index_path.name.startswith("modrinth") or "formatVersion" in data
    if is_modrinth and data.get("files") is not None:
        return _modrinth_items(data), notes

    if data.get("manifestType") == "minecraftModpack" or index_path.name == "manifest.json":
        api_key = os.environ.get("CURSEFORGE_API_KEY", "").strip()
        items, cf_notes = _curseforge_items(data, api_key)
        return items, cf_notes

    # Fallback: try modrinth shape.
    if data.get("files"):
        return _modrinth_items(data), notes
    return [], [f"Unrecognized pack index format: {index_path.name}"]


def _safe_destination(root: Path, rel_path: str) -> Path | None:
    """Resolve an index path below root; reject traversal and symlink escapes."""
    rel = rel_path.replace("\\", "/").lstrip("/")
    if not rel or "\x00" in rel:
        return None
    root = root.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def download_pack_mods(
    target: str | Path,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    progress_cb: ProgressCb | None = None,
    log_cb: LogCb | None = None,
    cancel=None,
    overwrite: bool = False,
) -> DownloadResult:
    """Download the mods referenced by a server folder's stored pack index.

    This never raises; all problems are captured in the returned DownloadResult.
    """
    result = DownloadResult()
    target = Path(target).expanduser().resolve()

    def log(msg: str) -> None:
        result.messages.append(msg)
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    index_path = find_pack_index(target)
    if index_path is None:
        log("No pack index found for this server. Nothing to download.")
        return result

    try:
        items, notes = build_items(index_path)
    except Exception as exc:  # defensive — build_items should not raise
        log(f"Failed to read pack index: {exc}")
        return result
    for n in notes:
        log(n)

    if not items:
        log("Pack index contained no downloadable files.")
        return result

    opener = _opener()
    total = len(items)
    log(f"Found {total} file(s) in {index_path.name}.")

    for i, item in enumerate(items, start=1):
        if _is_cancelled(cancel):
            result.cancelled = True
            log("Download cancelled by user.")
            break

        # Client-only Modrinth files are flagged with an empty url list + sentinel.
        if item.source == "modrinth-client":
            result.skipped_client.append(item.name)
            log(f" - {item.name}: client-only, skipped")
            if progress_cb:
                progress_cb(i, total)
            continue

        dest = _safe_destination(target, item.rel_path)
        if dest is None:
            result.failed.append((item.name, "unsafe path outside server folder"))
            log(f" ✗ {item.name}: unsafe path rejected")
            if progress_cb:
                progress_cb(i, total)
            continue
        if dest.exists() and not overwrite:
            result.already_present.append(item.name)
            log(f" = {item.name}: already present")
            if progress_cb:
                progress_cb(i, total)
            continue

        try:
            log(f" ↓ {item.name} …")
            _download_one(item, dest, opener, timeout, log, cancel)
            result.downloaded.append(item.name)
            log(f" ✓ {item.name}")
        except _Cancelled:
            result.cancelled = True
            log("Download cancelled by user.")
            break
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            result.failed.append((item.name, reason))
            log(f" ✗ {item.name}: {reason}")
        finally:
            if progress_cb:
                try:
                    progress_cb(i, total)
                except Exception:
                    pass

    log("Done: " + result.summary())
    return result
