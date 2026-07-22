"""
crucible/importers/serverloader.py

Automatic *dedicated server* installer.

Prism Launcher (and .mrpack / CurseForge exports) only describe a **client**
instance — they never ship the server program needed to actually host the pack.
This module fills that gap: given a Minecraft version + mod loader, it downloads
and sets up the matching dedicated server so the pack becomes runnable with no
manual jar-hunting.

Everything is best-effort and **never raises** — all failures are returned as a
structured ``ServerInstallResult`` with friendly messages. Network access and a
working ``java`` are required for real installs; offline calls fail gracefully.

Supported loaders
-----------------
* vanilla  — Mojang piston-meta -> ``server.jar``           (direct jar, no java)
* fabric   — Fabric Meta -> ``fabric-server-launch.jar``     (direct jar, no java)
* quilt    — Quilt installer jar  (runs ``java -jar ... install server``)
* forge    — Forge installer jar  (runs ``java -jar ... --installServer``)
* neoforge — NeoForge installer jar (runs ``java -jar ... --installServer``)

All public URLs, no API keys.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_USER_AGENT = "Crucible/0.6.2 (Minecraft server manager)"
_TIMEOUT = 30
_MAX_RETRIES = 3
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024

# Public metadata / maven endpoints (no keys required).
_MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
_FABRIC_META = "https://meta.fabricmc.net/v2"
_NEOFORGE_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
_NEOFORGE_JAR = "https://maven.neoforged.net/releases/net/neoforged/neoforge/%s/neoforge-%s-installer.jar"
_FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
_FORGE_JAR = "https://maven.minecraftforge.net/net/minecraftforge/forge/%s/forge-%s-installer.jar"
_QUILT_META = "https://meta.quiltmc.org/v3"
_QUILT_JAR = "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/%s/quilt-installer-%s.jar"

LogCb = Optional[Callable[[str], None]]

_LOADER_ALIASES = {
    "": "vanilla", "vanilla": "vanilla", "none": "vanilla",
    "fabric": "fabric", "fabricmc": "fabric", "fabric-loader": "fabric",
    "quilt": "quilt", "quiltmc": "quilt", "quilt-loader": "quilt",
    "forge": "forge", "minecraftforge": "forge",
    "neoforge": "neoforge", "neoforged": "neoforge",
}


@dataclass
class ServerInstallResult:
    ok: bool = False
    loader: str = ""
    minecraft_version: str = ""
    loader_version: str = ""
    launcher: str = ""          # produced jar/script (relative or absolute)
    messages: list[str] = field(default_factory=list)
    failed_reason: str = ""
    cancelled: bool = False

    def summary(self) -> str:
        if self.cancelled:
            return "server install cancelled"
        if self.ok:
            base = f"installed {self.loader or 'vanilla'} server"
            if self.launcher:
                base += f" ({self.launcher})"
            return base
        return f"server install failed: {self.failed_reason or 'unknown error'}"


def normalize_loader(loader: str) -> str:
    return _LOADER_ALIASES.get((loader or "").strip().lower(), (loader or "").strip().lower() or "vanilla")


def _log(cb: LogCb, msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _http_bytes(url: str, *, cb: LogCb = None, timeout: int = _TIMEOUT) -> bytes:
    """GET bytes with retries. Raises urllib errors on final failure."""
    ctx = _ssl_ctx()
    last: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                data = r.read(_MAX_METADATA_BYTES + 1)
                if len(data) > _MAX_METADATA_BYTES:
                    raise RuntimeError("metadata response exceeds safe size limit")
                return data
        except Exception as exc:  # noqa: BLE001 - retry any transient error
            last = exc
            _log(cb, f"  ! {type(exc).__name__}: {exc} (attempt {attempt}/{_MAX_RETRIES})")
            if attempt < _MAX_RETRIES:
                time.sleep(min(2 ** attempt, 6))
    raise last if last else RuntimeError("request failed")


def _http_json(url: str, *, cb: LogCb = None) -> object:
    return json.loads(_http_bytes(url, cb=cb).decode("utf-8", "replace"))


def _download_to(url: str, dest: Path, *, cb: LogCb = None,
                 cancel=None, timeout: int = _TIMEOUT) -> None:
    """Stream-download a URL to dest atomically. Raises on failure."""
    if cancel is not None and cancel.is_set():
        raise RuntimeError("cancelled")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("refusing non-HTTPS download URL")
    ctx = _ssl_ctx()
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("cancelled")
        tmp = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                declared = r.headers.get("Content-Length")
                if declared and int(declared) > _MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("download exceeds safe size limit")
                fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
                tmp = Path(tmp_name)
                with os.fdopen(fd, "wb") as out:
                    received = 0
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise RuntimeError("cancelled")
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > _MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("download exceeds safe size limit")
                        out.write(chunk)
            os.replace(tmp, dest)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            if tmp and tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            _log(cb, f"  ! {type(exc).__name__}: {exc} (attempt {attempt}/{_MAX_RETRIES})")
            if str(exc) == "cancelled":
                raise
            if attempt < _MAX_RETRIES:
                time.sleep(min(2 ** attempt, 6))
    raise last if last else RuntimeError("download failed")


def _java_exe() -> str | None:
    return shutil.which("java")


# Per-loader installers (each returns launcher filename on success, raises on failure)

def _install_vanilla(target: Path, mc: str, *, cb: LogCb, cancel) -> str:
    if not mc:
        raise ValueError("no Minecraft version known; cannot pick a vanilla server")
    _log(cb, f"Looking up vanilla server for Minecraft {mc}…")
    manifest = _http_json(_MOJANG_MANIFEST, cb=cb)
    versions = manifest.get("versions", []) if isinstance(manifest, dict) else []
    entry = next((v for v in versions if v.get("id") == mc), None)
    if not entry:
        raise ValueError(f"Minecraft {mc} not found in Mojang manifest")
    pkg = _http_json(entry["url"], cb=cb)
    server = (pkg.get("downloads", {}) or {}).get("server") if isinstance(pkg, dict) else None
    if not server or not server.get("url"):
        raise ValueError(f"Mojang has no dedicated server download for {mc}")
    _log(cb, "Downloading server.jar…")
    _download_to(server["url"], target / "server.jar", cb=cb, cancel=cancel)
    return "server.jar"


def _install_fabric(target: Path, mc: str, loader_version: str, *, cb: LogCb, cancel) -> str:
    if not mc:
        raise ValueError("no Minecraft version known; cannot install Fabric server")
    # Resolve loader version (latest stable) if not given.
    lv = loader_version
    if not lv:
        loaders = _http_json(f"{_FABRIC_META}/versions/loader/{mc}", cb=cb)
        stable = [x for x in loaders if isinstance(x, dict) and x.get("loader", {}).get("stable")]
        pick = (stable or loaders)[0] if loaders else None
        lv = pick["loader"]["version"] if pick else ""
    if not lv:
        raise ValueError(f"no Fabric loader available for {mc}")
    # Resolve installer version (latest stable).
    installers = _http_json(f"{_FABRIC_META}/versions/installer", cb=cb)
    istable = [x for x in installers if isinstance(x, dict) and x.get("stable")]
    iv = (istable or installers)[0]["version"] if installers else ""
    if not iv:
        raise ValueError("no Fabric installer version available")
    url = f"{_FABRIC_META}/versions/loader/{mc}/{lv}/{iv}/server/jar"
    _log(cb, f"Downloading Fabric server launcher (loader {lv}, installer {iv})…")
    _download_to(url, target / "fabric-server-launch.jar", cb=cb, cancel=cancel)
    return "fabric-server-launch.jar"


def _run_installer(cmd: list[str], cwd: Path, *, cb: LogCb, timeout: int = 900) -> None:
    _log(cb, "Running: " + " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"installer timed out after {timeout}s")
    if proc.stdout:
        for line in proc.stdout.strip().splitlines()[-12:]:
            _log(cb, "  " + line)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("installer exited %d: %s" % (proc.returncode, " | ".join(tail)))


def _install_neoforge(target: Path, mc: str, loader_version: str, *, cb: LogCb, cancel) -> str:
    java = _java_exe()
    if not java:
        raise RuntimeError("java not found on PATH (required to run the NeoForge installer)")
    ver = loader_version
    if not ver:
        data = _http_json(_NEOFORGE_VERSIONS, cb=cb)
        all_versions = data.get("versions", []) if isinstance(data, dict) else []
        # NeoForge versions look like 21.1.66 where 21.1 ~ MC 1.21.1.
        mc_parts = mc.split(".")
        prefix = ""
        if len(mc_parts) >= 2:
            prefix = f"{mc_parts[1]}.{mc_parts[2]}" if len(mc_parts) >= 3 else f"{mc_parts[1]}.0"
        matches = [v for v in all_versions if prefix and v.startswith(prefix)]
        ver = (matches or all_versions)[-1] if (matches or all_versions) else ""
    if not ver:
        raise ValueError(f"no NeoForge version found for {mc}")
    jar = target / f"neoforge-{ver}-installer.jar"
    _log(cb, f"Downloading NeoForge installer {ver}…")
    _download_to(_NEOFORGE_JAR % (ver, ver), jar, cb=cb, cancel=cancel)
    _run_installer([java, "-jar", jar.name, "--installServer"], target, cb=cb)
    try:
        jar.unlink()
    except OSError:
        pass
    return "run.sh" if (target / "run.sh").exists() else "libraries/ (NeoForge unix_args)"


def _install_forge(target: Path, mc: str, loader_version: str, *, cb: LogCb, cancel) -> str:
    java = _java_exe()
    if not java:
        raise RuntimeError("java not found on PATH (required to run the Forge installer)")
    ver = loader_version
    if not ver:
        promos = _http_json(_FORGE_PROMOS, cb=cb)
        pr = promos.get("promos", {}) if isinstance(promos, dict) else {}
        ver = pr.get(f"{mc}-recommended") or pr.get(f"{mc}-latest") or ""
    if not ver:
        raise ValueError(f"no Forge version found for {mc}")
    full = f"{mc}-{ver}"
    jar = target / f"forge-{full}-installer.jar"
    _log(cb, f"Downloading Forge installer {full}…")
    _download_to(_FORGE_JAR % (full, full), jar, cb=cb, cancel=cancel)
    _run_installer([java, "-jar", jar.name, "--installServer"], target, cb=cb)
    try:
        jar.unlink()
    except OSError:
        pass
    return "run.sh" if (target / "run.sh").exists() else "forge server libraries"


def _install_quilt(target: Path, mc: str, loader_version: str, *, cb: LogCb, cancel) -> str:
    java = _java_exe()
    if not java:
        raise RuntimeError("java not found on PATH (required to run the Quilt installer)")
    # Resolve latest installer version from Quilt maven metadata.
    iv = ""
    try:
        meta = _http_bytes(
            "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/maven-metadata.xml",
            cb=cb,
        ).decode("utf-8", "replace")
        import re
        rel = re.search(r"<release>([^<]+)</release>", meta)
        iv = rel.group(1) if rel else ""
    except Exception:
        iv = ""
    if not iv:
        raise ValueError("could not resolve Quilt installer version")
    jar = target / f"quilt-installer-{iv}.jar"
    _log(cb, f"Downloading Quilt installer {iv}…")
    _download_to(_QUILT_JAR % (iv, iv), jar, cb=cb, cancel=cancel)
    cmd = [java, "-jar", jar.name, "install", "server", mc,
           "--download-server", "--install-dir=."]
    if loader_version:
        cmd.insert(5, loader_version)
    _run_installer(cmd, target, cb=cb)
    try:
        jar.unlink()
    except OSError:
        pass
    if (target / "quilt-server-launch.jar").exists():
        return "quilt-server-launch.jar"
    return "quilt server files"


_INSTALLERS = {
    "vanilla": lambda t, mc, lv, cb, cancel: _install_vanilla(t, mc, cb=cb, cancel=cancel),
    "fabric": lambda t, mc, lv, cb, cancel: _install_fabric(t, mc, lv, cb=cb, cancel=cancel),
    "neoforge": lambda t, mc, lv, cb, cancel: _install_neoforge(t, mc, lv, cb=cb, cancel=cancel),
    "forge": lambda t, mc, lv, cb, cancel: _install_forge(t, mc, lv, cb=cb, cancel=cancel),
    "quilt": lambda t, mc, lv, cb, cancel: _install_quilt(t, mc, lv, cb=cb, cancel=cancel),
}


def install_server_loader(
    target: str | Path,
    *,
    minecraft_version: str,
    loader: str = "",
    loader_version: str = "",
    log_cb: LogCb = None,
    cancel=None,
) -> ServerInstallResult:
    """Install the dedicated server program into ``target``. Never raises.

    Returns a ``ServerInstallResult`` describing what happened.
    """
    target = Path(target).expanduser()
    norm = normalize_loader(loader)
    result = ServerInstallResult(
        loader=norm, minecraft_version=minecraft_version or "", loader_version=loader_version or "",
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        result.failed_reason = f"cannot create target folder: {exc}"
        return result

    fn = _INSTALLERS.get(norm)
    if fn is None:
        result.failed_reason = f"unsupported loader: {loader!r}"
        result.messages.append(result.failed_reason)
        return result

    _log(log_cb, f"Installing {norm} server for Minecraft {minecraft_version or '?'}…")
    try:
        launcher = fn(target, minecraft_version, loader_version, log_cb, cancel)
        result.launcher = launcher
        result.ok = True
        result.messages.append(result.summary())
        _log(log_cb, "✓ " + result.summary())
        _write_run_hint(target, norm, launcher)
    except Exception as exc:  # noqa: BLE001 - report, never crash the app
        if str(exc) == "cancelled" or (cancel is not None and cancel.is_set()):
            result.cancelled = True
            result.failed_reason = "cancelled"
        else:
            result.failed_reason = f"{type(exc).__name__}: {exc}"
        result.messages.append(result.summary())
        _log(log_cb, "✗ " + result.summary())
    return result


def _write_run_hint(target: Path, loader: str, launcher: str) -> None:
    """Drop a small note recording how the server was set up."""
    try:
        meta = target / ".crucible"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "server-install.json").write_text(
            json.dumps({"loader": loader, "launcher": launcher}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# Convenience used by the GUI/CLI to decide whether an install is even possible.

def can_attempt(loader: str) -> bool:
    return normalize_loader(loader) in _INSTALLERS


def requires_java(loader: str) -> bool:
    return normalize_loader(loader) in ("forge", "neoforge", "quilt")


# ---------------------------------------------------------------------------
# Vanilla version listing + one-click fresh-server creation
# ---------------------------------------------------------------------------

@dataclass
class McVersion:
    id: str
    type: str            # "release" | "snapshot" | "old_beta" | "old_alpha"
    release_time: str = ""


def list_versions(
    *,
    include_snapshots: bool = False,
    include_old: bool = False,
    cb: LogCb = None,
) -> list[McVersion]:
    """Return available Minecraft versions (newest first). Never raises.

    On any failure (e.g. offline) returns an empty list; callers should fall
    back to letting the user type a version manually.
    """
    try:
        manifest = _http_json(_MOJANG_MANIFEST, cb=cb)
    except Exception as exc:  # noqa: BLE001
        _log(cb, f"Could not fetch version list: {exc}")
        return []
    out: list[McVersion] = []
    for v in (manifest.get("versions", []) if isinstance(manifest, dict) else []):
        vtype = v.get("type", "")
        if vtype == "snapshot" and not include_snapshots:
            continue
        if vtype in ("old_beta", "old_alpha") and not include_old:
            continue
        out.append(McVersion(id=v.get("id", ""), type=vtype, release_time=v.get("releaseTime", "")))
    return out


def latest_release(*, cb: LogCb = None) -> str:
    """Return the latest stable release id, or '' if unavailable."""
    try:
        manifest = _http_json(_MOJANG_MANIFEST, cb=cb)
        if isinstance(manifest, dict):
            return str(manifest.get("latest", {}).get("release", ""))
    except Exception as exc:  # noqa: BLE001
        _log(cb, f"Could not fetch latest release: {exc}")
    return ""


@dataclass
class FreshServerResult:
    ok: bool = False
    path: str = ""
    install: Optional[ServerInstallResult] = None
    messages: list[str] = field(default_factory=list)
    failed_reason: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"created server at {self.path}"
        return f"could not create server: {self.failed_reason or 'unknown error'}"


def create_fresh_server(
    target: str | Path,
    *,
    minecraft_version: str,
    loader: str = "vanilla",
    loader_version: str = "",
    accept_eula: bool = False,
    overwrite: bool = False,
    log_cb: LogCb = None,
    cancel=None,
) -> FreshServerResult:
    """Create a brand-new, runnable server folder from scratch. Never raises.

    Steps: make the folder, install the dedicated server program for the chosen
    loader, then drop a start script, default ``server.properties`` and
    ``eula.txt``. The result is immediately runnable (once the EULA is accepted)
    via ``start.sh`` — no manual jar hunting.
    """
    target = Path(target).expanduser()
    res = FreshServerResult(path=str(target))
    try:
        if target.exists() and any(target.iterdir()) and not overwrite:
            res.failed_reason = f"folder {target} already exists and is not empty"
            return res
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        res.failed_reason = f"cannot create folder: {exc}"
        return res

    install = install_server_loader(
        target,
        minecraft_version=minecraft_version,
        loader=loader,
        loader_version=loader_version,
        log_cb=log_cb,
        cancel=cancel,
    )
    res.install = install
    res.messages.extend(install.messages)

    # Always lay down the support files so the folder is well-formed even if the
    # download failed (the user can drop a jar in later and it will just work).
    try:
        from . import prism as _prism
        (target / "start.sh").write_text(_prism._START_SH, encoding="utf-8")
        os.chmod(target / "start.sh", 0o755)
        _prism._write_default_server_properties(target)
        _prism._write_eula(target, accept_eula)
    except Exception as exc:  # noqa: BLE001
        _log(log_cb, f"Warning while writing support files: {exc}")
        res.messages.append(f"support-file warning: {exc}")

    res.ok = install.ok
    if not install.ok and not install.cancelled:
        res.failed_reason = install.failed_reason
    res.messages.append(res.summary())
    return res
