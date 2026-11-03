"""
crucible/mods/modrinth.py

Dead-simple mod installation via the public Modrinth API (no API key needed).

Search by name, pick a result, and Crucible resolves the right file for the
server's Minecraft version + loader, pulls in required dependencies, downloads
everything into mods/, and records what it did in .crucible/added-mods.json.

Network calls are isolated here and always raise ModrinthError (never a raw
URLError) so the GUI/CLI can show a friendly message when offline.
"""

from __future__ import annotations

import json
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import tempfile
import os
from dataclasses import dataclass, field
from pathlib import Path

from crucible import __version__

_API = "https://api.modrinth.com/v2"
_UA = f"Crucible/{__version__} (Minecraft server manager)"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_ICON_BYTES = 8 * 1024 * 1024
_MAX_MOD_BYTES = 2 * 1024 * 1024 * 1024


def humanize_count(n: int) -> str:
    """Format a download/follow count like Modrinth/Prism: 754M, 134K, 1.2K."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B".replace(".0B", "B")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


class ModrinthError(Exception):
    pass


@dataclass
class ModHit:
    project_id: str
    slug: str
    title: str
    description: str
    downloads: int
    categories: list = field(default_factory=list)
    author: str = ""
    icon_url: str = ""
    follows: int = 0
    client_side: str = ""   # required / optional / unsupported / unknown
    server_side: str = ""   # required / optional / unsupported / unknown
    project_type: str = "mod"

    @property
    def is_client_only(self) -> bool:
        """True if this mod does nothing on a dedicated server."""
        return (self.server_side or "").lower() == "unsupported"

    def server_label(self) -> str:
        """Short server-compatibility tag for the UI."""
        s = (self.server_side or "").lower()
        if s == "unsupported":
            return "Client-only"
        if s == "required":
            return "Server-required"
        if s == "optional":
            return "Server-ready"
        return ""

    def page_url(self) -> str:
        # CurseForge requires its official slug; a numeric project ID is not a
        # trustworthy URL slug. Do not guess a URL if provider metadata lacks it.
        if not isinstance(self.project_id, str) or not self.project_id:
            return ""
        if self.project_id.startswith("cf:"):
            slug = self.slug.strip() if isinstance(self.slug, str) else ""
            if not slug or slug in (".", "..") or any(ord(ch) < 32 for ch in slug):
                return ""
            return "https://www.curseforge.com/minecraft/mc-mods/" + urllib.parse.quote(slug, safe="")
        slug = (self.slug if isinstance(self.slug, str) and self.slug else self.project_id).strip()
        if not slug or any(ord(ch) < 32 for ch in slug):
            return ""
        return "https://modrinth.com/mod/" + urllib.parse.quote(slug, safe="")

    def human_downloads(self) -> str:
        return humanize_count(self.downloads)


@dataclass
class ModFile:
    project_id: str
    title: str
    version_id: str
    version_number: str
    filename: str
    url: str
    sha1: str
    size: int
    dependencies: list = field(default_factory=list)  # required project ids


def _get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(_MAX_JSON_BYTES + 1)
            if len(data) > _MAX_JSON_BYTES:
                raise ModrinthError("Modrinth response was unexpectedly large.")
            return json.loads(data.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ModrinthError(f"Modrinth returned HTTP {e.code}.") from e
    except urllib.error.URLError as e:
        raise ModrinthError(
            f"Could not reach Modrinth (no internet?). Details: {e.reason}") from e
    except (TimeoutError, ValueError) as e:
        raise ModrinthError(f"Modrinth request failed: {e}") from e


def search(query: str, *, loader: str = "", mc_version: str = "",
           limit: int = 20, index: str = "relevance", offset: int = 0,
           project_type: str = "mod") -> list:
    query = (query or "").strip()
    # With no search text, show the most popular compatible projects instead of
    # an empty list -- gives the browser content the moment it opens.
    if not query and index == "relevance":
        index = "downloads"
    facets = [[f"project_type:{project_type}"]]
    if loader and loader != "vanilla":
        facets.append([f"categories:{loader}"])
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    params = {"query": query, "limit": str(limit),
              "offset": str(max(0, int(offset))),
              "facets": json.dumps(facets), "index": index}
    data = _get(f"{_API}/search?{urllib.parse.urlencode(params)}")
    if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
        raise ModrinthError("Modrinth returned an unexpected search-result format.")
    hits = []
    for h in data["hits"]:
        if not isinstance(h, dict):
            raise ModrinthError("Modrinth returned malformed search metadata.")
        project_id = h.get("project_id")
        if not isinstance(project_id, str) or not project_id or project_id.startswith("cf:"):
            raise ModrinthError("Modrinth returned a project ID from the wrong provider namespace.")
        hits.append(ModHit(
            project_id=project_id, slug=h.get("slug", ""),
            title=h.get("title", "?"), description=h.get("description", ""),
            downloads=int(h.get("downloads", 0) or 0),
            categories=list(h.get("display_categories")
                             or h.get("categories", []) or []),
            author=h.get("author", ""),
            icon_url=h.get("icon_url", "") or "",
            follows=int(h.get("follows", 0) or 0),
            client_side=h.get("client_side", "") or "",
            server_side=h.get("server_side", "") or "",
            project_type=h.get("project_type", "mod") or "mod",
        ))
    return hits


def browse_popular(*, loader: str = "", mc_version: str = "",
                   limit: int = 30, offset: int = 0,
                   project_type: str = "mod") -> list:
    """Most-downloaded mods compatible with this server (no query)."""
    return search("", loader=loader, mc_version=mc_version,
                  limit=limit, index="downloads", offset=offset,
                  project_type=project_type)


def search_modpacks(query: str = "", *, loader: str = "", mc_version: str = "",
                    limit: int = 30, offset: int = 0) -> list:
    """Search Modrinth modpacks (project_type:modpack). Empty query -> popular."""
    return search(query, loader=loader, mc_version=mc_version,
                  limit=limit, offset=offset, project_type="modpack")


@dataclass
class ModpackFile:
    project_id: str
    title: str
    version_id: str
    version_number: str
    filename: str
    url: str
    sha1: str
    size: int
    game_versions: list = field(default_factory=list)
    loaders: list = field(default_factory=list)


def resolve_modpack(project_id: str, *, mc_version: str = "",
                    loader: str = "") -> "ModpackFile":
    """Find the newest .mrpack file for a Modrinth modpack project."""
    if not isinstance(project_id, str) or not project_id or project_id.startswith("cf:"):
        raise ModrinthError("Modrinth modpack resolution requires a Modrinth project ID.")
    params = {}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    if loader and loader != "vanilla":
        params["loaders"] = json.dumps([loader])
    url = f"{_API}/project/{urllib.parse.quote(project_id, safe='')}/version"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    versions = _get(url)
    if not isinstance(versions, list):
        raise ModrinthError("Modrinth returned an unexpected modpack-version format.")
    if not versions:
        raise ModrinthError("No matching modpack version found.")
    v = versions[0]
    if not isinstance(v, dict) or v.get("project_id") != project_id or not v.get("id"):
        raise ModrinthError("Modrinth returned a modpack version for a different or unknown project.")
    files = v.get("files", []) or []
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ModrinthError("Modrinth returned malformed modpack file metadata.")
    mrpack = None
    for f in files:
        if str(f.get("filename", "")).lower().endswith(".mrpack"):
            mrpack = f
            if f.get("primary"):
                break
    if mrpack is None:
        mrpack = _pick_primary(files)
    if not mrpack:
        raise ModrinthError("Modpack version has no downloadable file.")
    hashes = mrpack.get("hashes", {}) or {}
    if not isinstance(hashes, dict):
        raise ModrinthError("Modrinth returned malformed modpack file hashes.")
    return ModpackFile(
        project_id=project_id, title=v.get("name", project_id),
        version_id=v.get("id", ""), version_number=v.get("version_number", ""),
        filename=mrpack.get("filename", "modpack.mrpack"),
        url=mrpack.get("url", ""),
        sha1=hashes.get("sha1", ""),
        size=int(mrpack.get("size", 0) or 0),
        game_versions=list(v.get("game_versions", []) or []),
        loaders=list(v.get("loaders", []) or []),
    )


def fetch_bytes(url: str, timeout: float = 15.0) -> bytes:
    """Fetch raw bytes (used for mod icons). Raises ModrinthError on failure."""
    if not url:
        raise ModrinthError("No URL.")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(_MAX_ICON_BYTES + 1)
            if len(data) > _MAX_ICON_BYTES:
                raise ModrinthError("Image response was unexpectedly large.")
            return data
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ModrinthError(f"Fetch failed: {e}") from e


def _pick_primary(files: list):
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


def resolve_file(project_id: str, *, loader: str, mc_version: str) -> ModFile:
    """Find the newest matching version file for a project.

    Refuses to guess. Modrinth's version endpoint treats a missing facet as
    "any", so asking with an unknown loader or Minecraft version returns the
    newest build for ANY platform -- which is how a NeoForge 1.21 jar can end
    up installed on a Forge 1.7.10 server. Better to stop and make the caller
    fill in the instance metadata.
    """
    if not isinstance(project_id, str) or not project_id or project_id.startswith("cf:"):
        raise ModrinthError("Modrinth release resolution requires a Modrinth project ID.")
    if not mc_version or not loader:
        missing = " and ".join(
            part for part, ok in (("Minecraft version", mc_version), ("mod loader", loader))
            if not ok
        )
        raise ModrinthError(
            f"This server's {missing} is unknown, so Modrinth cannot be asked for "
            "a compatible build -- it would return the newest file for any "
            "platform. Fill it in on the instance first (Setup tab, or "
            "'crucible edit --minecraft-version ... --loader ...')."
        )
    params = {}
    if loader and loader != "vanilla":
        params["loaders"] = json.dumps([loader])
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    url = f"{_API}/project/{urllib.parse.quote(project_id, safe='')}/version"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    versions = _get(url)
    if not isinstance(versions, list):
        raise ModrinthError("Modrinth returned an unexpected release-list format.")
    if not versions:
        raise ModrinthError(
            "No file matches this server's Minecraft version/loader.")
    v = versions[0]
    if not isinstance(v, dict) or v.get("project_id") != project_id or not isinstance(v.get("id"), str) or not v.get("id"):
        raise ModrinthError("Modrinth returned a release for a different or unknown project.")
    loaders = v.get("loaders", [])
    game_versions = v.get("game_versions", [])
    if not isinstance(loaders, list) or not isinstance(game_versions, list):
        raise ModrinthError("Modrinth returned malformed compatibility metadata.")
    if mc_version not in game_versions or (loader != "vanilla" and loader not in loaders):
        raise ModrinthError("Modrinth returned a release incompatible with this server's Minecraft version or loader.")
    version_files = v.get("files", [])
    if not isinstance(version_files, list) or any(not isinstance(item, dict) for item in version_files):
        raise ModrinthError("Modrinth returned malformed file metadata.")
    pf = _pick_primary(version_files)
    if not isinstance(pf, dict):
        raise ModrinthError("Selected version has no downloadable file.")
    filename = pf.get("filename")
    if not isinstance(filename, str) or Path(filename).name != filename or "\\" in filename or not filename.endswith(".jar"):
        raise ModrinthError("Modrinth returned an unsafe or non-JAR filename.")
    hashes = pf.get("hashes", {}) or {}
    if not isinstance(hashes, dict):
        raise ModrinthError("Modrinth returned malformed file hashes.")
    sha1 = hashes.get("sha1", "")
    if not isinstance(sha1, str) or len(sha1) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in sha1):
        raise ModrinthError("Modrinth did not provide a verifiable SHA-1 for the selected file.")
    raw_dependencies = v.get("dependencies", [])
    if not isinstance(raw_dependencies, list) or any(not isinstance(dep, dict) for dep in raw_dependencies):
        raise ModrinthError("Modrinth returned malformed dependency metadata.")
    deps = [d.get("project_id") for d in raw_dependencies
            if d.get("dependency_type") == "required" and d.get("project_id")]
    try:
        size = int(pf.get("size", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ModrinthError("Modrinth returned an invalid file size.") from exc
    if size < 0:
        raise ModrinthError("Modrinth returned an invalid file size.")
    url = pf.get("url", "") or ""
    if not isinstance(url, str):
        raise ModrinthError("Modrinth returned an invalid download URL.")
    return ModFile(
        project_id=project_id, title=v.get("name", project_id),
        version_id=v.get("id", ""), version_number=v.get("version_number", ""),
        filename=filename, url=url, sha1=sha1,
        size=size, dependencies=deps,
    )


def resolve_with_deps(project_id: str, *, loader: str, mc_version: str,
                      _seen=None) -> list:
    """Resolve a project plus its required dependencies (depth-first, dedup)."""
    seen = _seen if _seen is not None else set()
    if project_id in seen:
        return []
    seen.add(project_id)
    main = resolve_file(project_id, loader=loader, mc_version=mc_version)
    out = [main]
    for dep in main.dependencies:
        try:
            out.extend(resolve_with_deps(dep, loader=loader,
                                         mc_version=mc_version, _seen=seen))
        except ModrinthError as exc:
            raise ModrinthError(f"Required dependency {dep} could not be resolved: {exc}") from exc
    return out


def _download(url: str, dest: Path, timeout: float = 60.0) -> int:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ModrinthError("Refusing a non-HTTPS download URL.")
    from ..download_http import make_request, open_url, http_error_message, is_curseforge_url
    try:
        req = make_request(url, headers={"User-Agent": _UA})
    except ValueError as exc:
        raise ModrinthError(str(exc)) from exc
    tmp = None
    try:
        with open_url(req, timeout=timeout) as r:
            declared = r.headers.get("Content-Length")
            if declared and int(declared) > _MAX_MOD_BYTES:
                raise ModrinthError("Mod file exceeds safe size limit.")
            fd, tmp_name = tempfile.mkstemp(prefix=".crucible-mod-", dir=str(dest.parent))
            tmp = Path(tmp_name)
            received = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _MAX_MOD_BYTES:
                        raise ModrinthError("Mod file exceeds safe size limit.")
                    out.write(chunk)
        tmp.replace(dest)
        return received
    except urllib.error.HTTPError as exc:
        raise ModrinthError(http_error_message(exc.code, curseforge=is_curseforge_url(url))) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ModrinthError(f"Download failed: {e}") from e
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class InstallResult:
    installed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.installed:
            parts.append(f"{len(self.installed)} installed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} already present")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts) or "nothing to do"


def install_files(server_path, files: list, *, verify: bool = True) -> InstallResult:
    server = Path(server_path)
    mods = server / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    res = InstallResult()
    record_path = server / ".crucible" / "added-mods.json"
    record = _load_record(record_path)
    for mf in files:
        if (not mf.filename or Path(mf.filename).name != mf.filename
                or "\\" in mf.filename or not mf.filename.lower().endswith(".jar")):
            res.failed.append(f"{mf.filename} (unsafe mod filename)")
            continue
        # Every provider-backed install must carry a usable SHA-1. Without
        # this, the old implementation skipped the checksum comparison entirely
        # when the hash was empty, which is unsafe for the direct dependency-
        # install path as well as the transactional replacement path.
        sha1 = mf.sha1
        if (not isinstance(sha1, str) or len(sha1) != 40
                or any(ch not in "0123456789abcdefABCDEF" for ch in sha1)):
            res.failed.append(mf.filename + " (no valid SHA-1; download was not installed)")
            continue
        dest = mods / mf.filename
        if dest.is_symlink():
            res.failed.append(mf.filename + " (symlinked destination)")
            continue
        if dest.exists():
            if _sha1(dest).lower() != sha1.lower():
                res.failed.append(mf.filename + " (different file already exists; use Change version to replace safely)")
                continue
            res.skipped.append(mf.filename)
            record[mf.filename] = {"project_id": mf.project_id, "title": mf.title, "version": mf.version_number, "version_id": mf.version_id, "sha1": mf.sha1, "size": mf.size}
            continue
        if not mf.url:
            res.failed.append(mf.filename + " (direct download unavailable or author-restricted; download from the project page and use Add .jar)")
            continue
        try:
            _download(mf.url, dest)
            # Keep `verify` in the public signature for backwards compatibility,
            # but never permit an unchecked provider install.
            if _sha1(dest).lower() != sha1.lower():
                dest.unlink(missing_ok=True)
                res.failed.append(mf.filename + " (checksum mismatch)")
                continue
            res.installed.append(mf.filename)
            record[mf.filename] = {
                "project_id": mf.project_id, "title": mf.title,
                "version": mf.version_number, "version_id": mf.version_id,
                "sha1": mf.sha1, "size": mf.size,
            }
        except ModrinthError as e:
            res.failed.append(f"{mf.filename} ({e})")
    _save_record(record_path, record)
    return res


def _load_record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_record(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def added_mods(server_path) -> dict:
    return _load_record(Path(server_path) / ".crucible" / "added-mods.json")


# ----------------------- update / dependency checks ---------------------

def check_update(server_path, filename: str, *, loader: str,
                 mc_version: str):
    """Return a newer ModFile for a Crucible-installed mod, else None.

    Looks the file up in .crucible/added-mods.json (so we know the project and
    the exact version that was installed). Raises ModrinthError on network
    failure so the caller can show a friendly offline message.
    """
    from .versions import list_versions, verified_identity_for_file
    meta = verified_identity_for_file(
        server_path, filename, Path(server_path) / "mods" / filename
    )
    if not meta or not meta.get("project_id"):
        return None
    # Route through the provider-aware listing so a saved CurseForge ID is
    # never accidentally sent to the Modrinth project endpoint.
    options = list_versions(meta["project_id"], loader=loader, mc_version=mc_version)
    if not options:
        return None
    newest = options[0]
    if newest.version_id and newest.version_id == meta.get("version_id", ""):
        return None
    return newest


def apply_update(server_path, old_filename: str, new_file, *,
                 verify: bool = True):
    """Install new_file, then remove the old jar it replaces (and its record)."""
    from .versions import replace_files
    return replace_files(server_path, old_filename, [new_file])


def verify_dependencies(server_path, *, loader: str, mc_version: str):
    """Check that required dependencies of Crucible-installed mods are present.

    Returns (missing_files: list[ModFile], notes: list[str]). Network failures
    for individual mods are collected in notes rather than raised, so the action
    degrades gracefully when offline.
    """
    server = Path(server_path)
    mods = server / "mods"
    present = set()
    if mods.exists():
        for p in mods.iterdir():
            if p.is_file() and not p.name.endswith(".disabled"):
                present.add(p.name)
    rec = added_mods(server_path)
    missing = {}
    notes = []
    from .versions import verified_identity_for_file
    for fname in rec:
        if fname.endswith(".disabled") or fname not in present:
            continue
        meta = verified_identity_for_file(server_path, fname, mods / fname)
        if not meta:
            notes.append(f"{fname}: installed file no longer matches its recorded identity; identify online before trusting dependency metadata.")
            continue
        pid = meta.get("project_id")
        if not pid:
            continue
        try:
            from .versions import list_versions, resolve_selected, _mod_file
            if pid.startswith("cf:"):
                options = list_versions(pid, loader=loader, mc_version=mc_version)
                main = next((v for v in options if v.version_id == meta.get("version_id")), None)
                if main is None: raise ModrinthError("Installed CurseForge version was not found; refusing to verify a different release.")
            elif meta.get("version_id"):
                raw = _get(f"{_API}/version/{urllib.parse.quote(meta['version_id'], safe='')}")
                if raw.get("project_id") != pid:
                    raise ModrinthError("Recorded version does not belong to the recorded project; refusing to verify a different release.")
                main = _mod_file(raw)
            else:
                raise ModrinthError("Installed version is unknown; use Identify online first.")
            files = resolve_selected(main, loader=loader, mc_version=mc_version)
        except ModrinthError as e:
            notes.append(f"{fname}: {e}")
            continue
        for mf in files[1:]:
            if mf.filename not in present and mf.filename not in missing:
                missing[mf.filename] = mf
    return list(missing.values()), notes
