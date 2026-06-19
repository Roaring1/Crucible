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
from dataclasses import dataclass, field
from pathlib import Path

_API = "https://api.modrinth.com/v2"
_UA = "Crucible/0.4.6 (Minecraft server manager)"


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
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ModrinthError(f"Modrinth returned HTTP {e.code}.") from e
    except urllib.error.URLError as e:
        raise ModrinthError(
            f"Could not reach Modrinth (no internet?). Details: {e.reason}") from e
    except (TimeoutError, ValueError) as e:
        raise ModrinthError(f"Modrinth request failed: {e}") from e


def search(query: str, *, loader: str = "", mc_version: str = "",
           limit: int = 20, index: str = "relevance") -> list:
    query = (query or "").strip()
    # With no search text, show the most popular compatible mods instead of an
    # empty list -- gives the browser content the moment it opens.
    if not query and index == "relevance":
        index = "downloads"
    facets = [["project_type:mod"]]
    if loader and loader != "vanilla":
        facets.append([f"categories:{loader}"])
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    params = {"query": query, "limit": str(limit),
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
                   limit: int = 30) -> list:
    """Most-downloaded mods compatible with this server (no query)."""
    return search("", loader=loader, mc_version=mc_version,
                  limit=limit, index="downloads")


def fetch_bytes(url: str, timeout: float = 15.0) -> bytes:
    """Fetch raw bytes (used for mod icons). Raises ModrinthError on failure."""
    if not url:
        raise ModrinthError("No URL.")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
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
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise ModrinthError(f"Download failed: {e}") from e
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return len(data)


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    h.update(path.read_bytes())
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
