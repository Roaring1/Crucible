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

_API = "https://api.modrinth.com/v2"
_UA = "Crucible/0.6.2 (Minecraft server manager)"
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
        return "https://modrinth.com/mod/" + (self.slug or self.project_id)

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
    hits = []
    for h in data.get("hits", []):
        hits.append(ModHit(
            project_id=h.get("project_id", ""), slug=h.get("slug", ""),
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
    params = {}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    if loader and loader != "vanilla":
        params["loaders"] = json.dumps([loader])
    url = f"{_API}/project/{project_id}/version"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    versions = _get(url)
    if not versions:
        raise ModrinthError("No matching modpack version found.")
    v = versions[0]
    files = v.get("files", []) or []
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
    return ModpackFile(
        project_id=project_id, title=v.get("name", project_id),
        version_id=v.get("id", ""), version_number=v.get("version_number", ""),
        filename=mrpack.get("filename", "modpack.mrpack"),
        url=mrpack.get("url", ""),
        sha1=(mrpack.get("hashes", {}) or {}).get("sha1", ""),
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
    """Find the newest matching version file for a project."""
    params = {}
    if loader and loader != "vanilla":
        params["loaders"] = json.dumps([loader])
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    url = f"{_API}/project/{project_id}/version"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    versions = _get(url)
    if not versions:
        raise ModrinthError(
            "No file matches this server's Minecraft version/loader.")
    v = versions[0]
    pf = _pick_primary(v.get("files", []))
    if not pf:
        raise ModrinthError("Selected version has no downloadable file.")
    deps = [d.get("project_id") for d in v.get("dependencies", [])
            if d.get("dependency_type") == "required" and d.get("project_id")]
    return ModFile(
        project_id=project_id, title=v.get("name", project_id),
        version_id=v.get("id", ""), version_number=v.get("version_number", ""),
        filename=pf.get("filename", "mod.jar"), url=pf.get("url", ""),
        sha1=(pf.get("hashes", {}) or {}).get("sha1", ""),
        size=int(pf.get("size", 0) or 0), dependencies=deps,
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
        except ModrinthError:
            continue
    return out


def _download(url: str, dest: Path, timeout: float = 60.0) -> int:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ModrinthError("Refusing a non-HTTPS download URL.")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    tmp = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
        dest = mods / mf.filename
        if dest.exists():
            res.skipped.append(mf.filename)
            continue
        if not mf.url:
            res.failed.append(mf.filename)
            continue
        try:
            _download(mf.url, dest)
            if verify and mf.sha1 and _sha1(dest).lower() != mf.sha1.lower():
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
        return json.loads(path.read_text(encoding="utf-8"))
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
    rec = added_mods(server_path)
    base = filename[:-9] if filename.endswith(".disabled") else filename
    meta = rec.get(base) or rec.get(filename)
    if not meta or not meta.get("project_id"):
        return None
    newest = resolve_file(meta["project_id"], loader=loader, mc_version=mc_version)
    if newest.version_id and newest.version_id == meta.get("version_id", ""):
        return None
    return newest


def apply_update(server_path, old_filename: str, new_file, *,
                 verify: bool = True):
    """Install new_file, then remove the old jar it replaces (and its record)."""
    server = Path(server_path)
    mods = server / "mods"
    res = install_files(server_path, [new_file], verify=verify)
    record_path = server / ".crucible" / "added-mods.json"
    if new_file.filename != old_filename:
        base = old_filename[:-9] if old_filename.endswith(".disabled") else old_filename
        for cand in {old_filename, base, base + ".disabled"}:
            if cand == new_file.filename:
                continue
            p = mods / cand
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            rec = _load_record(record_path)
            if rec.pop(cand, None) is not None:
                _save_record(record_path, rec)
    return res


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
            if p.is_file():
                present.add(p.name)
                if p.name.endswith(".disabled"):
                    present.add(p.name[:-9])
    rec = added_mods(server_path)
    missing = {}
    notes = []
    for fname, meta in rec.items():
        pid = meta.get("project_id")
        if not pid:
            continue
        try:
            files = resolve_with_deps(pid, loader=loader, mc_version=mc_version)
        except ModrinthError as e:
            notes.append(f"{fname}: {e}")
            continue
        for mf in files[1:]:
            if mf.filename not in present and mf.filename not in missing:
                missing[mf.filename] = mf
    return list(missing.values()), notes
