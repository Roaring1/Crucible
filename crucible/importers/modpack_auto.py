"""Automated server-side modpack installation.

This module orchestrates the full "click and go" flow for turning a Modrinth
modpack (a ``.mrpack`` file or a project id/slug) into a ready-to-run dedicated
server instance:

    1. Read the ``modrinth.index.json`` to learn the Minecraft version and the
       mod loader (Fabric / Quilt / Forge / NeoForge) the pack targets.
    2. Install the matching dedicated *server* loader via ``serverloader``.
    3. Stage the index so ``downloader.download_pack_mods`` can fetch every
       server-side mod listed in the pack.
    4. Apply the pack's ``overrides/`` and ``server-overrides/`` (config, etc.).
    5. Drop a start script, default ``server.properties`` and ``eula.txt`` so the
       instance is immediately runnable.

Everything here is defensive: the public ``install_modpack_*`` functions never
raise. They always return a :class:`ModpackInstallResult` describing what
happened so the GUI/CLI can surface a friendly message.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import serverloader as sl
from . import prism as _prism
from . import downloader as dl
from ..mods import modrinth

LogCb = Optional[Callable[[str], None]]
ProgressCb = Optional[Callable[[int, int], None]]

_INDEX_NAME = "modrinth.index.json"

# Maps the loader dependency key found in modrinth.index.json -> our loader name.
_LOADER_DEP_KEYS = {
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
    "forge": "forge",
    "neoforge": "neoforge",
}


def _log(cb: LogCb, msg: str) -> None:
    if cb is not None:
        try:
            cb(msg)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Index parsing
# --------------------------------------------------------------------------- #
def loader_from_index(index: dict) -> tuple[str, str, str]:
    """Return ``(loader, loader_version, minecraft_version)`` from a pack index.

    ``loader`` is one of our normalized names (``fabric``/``quilt``/``forge``/
    ``neoforge``/``vanilla``). Missing pieces come back as empty strings.
    """
    deps = {}
    if isinstance(index, dict):
        raw = index.get("dependencies")
        if isinstance(raw, dict):
            deps = raw
    mc = str(deps.get("minecraft", "") or "")
    loader = "vanilla"
    loader_version = ""
    for key, name in _LOADER_DEP_KEYS.items():
        if key in deps and deps[key]:
            loader = name
            loader_version = str(deps[key])
            break
    return loader, loader_version, mc


def read_index_from_mrpack(mrpack: str | Path) -> dict:
    """Read and parse ``modrinth.index.json`` from a ``.mrpack`` archive.

    Raises ``ValueError`` if the archive is missing the index or it is invalid.
    """
    mrpack = Path(mrpack)
    if not mrpack.exists():
        raise ValueError(f"modpack file not found: {mrpack}")
    try:
        with zipfile.ZipFile(mrpack) as zf:
            try:
                raw = zf.read(_INDEX_NAME)
            except KeyError:
                raise ValueError(
                    f"{mrpack.name} is not a valid .mrpack (no {_INDEX_NAME})"
                )
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{mrpack.name} is not a valid zip archive: {exc}") from exc
    try:
        index = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{_INDEX_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(index, dict):
        raise ValueError(f"{_INDEX_NAME} did not contain a JSON object")
    return index


def _safe_join(base: Path, rel: str) -> Optional[Path]:
    """Resolve ``rel`` under ``base``, refusing path-traversal escapes."""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel:
        return None
    candidate = (base / rel).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        return None
    return candidate


def apply_overrides(mrpack: str | Path, target: str | Path, *, log_cb: LogCb = None) -> int:
    """Extract ``overrides/`` then ``server-overrides/`` from the pack.

    ``server-overrides`` is applied last so it wins over the shared overrides.
    Returns the number of files written. Never raises; problems are logged.
    """
    target = Path(target)
    written = 0
    try:
        with zipfile.ZipFile(Path(mrpack)) as zf:
            names = zf.namelist()
            for prefix in ("overrides/", "server-overrides/"):
                for name in names:
                    if not name.startswith(prefix) or name.endswith("/"):
                        continue
                    rel = name[len(prefix):]
                    if not rel:
                        continue
                    dest = _safe_join(target, rel)
                    if dest is None:
                        _log(log_cb, f"  ! skipped unsafe override path: {name}")
                        continue
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(dest, "wb") as out:
                            out.write(src.read())
                        written += 1
                    except Exception as exc:  # noqa: BLE001
                        _log(log_cb, f"  ! could not write override {rel}: {exc}")
    except Exception as exc:  # noqa: BLE001
        _log(log_cb, f"  ! could not read overrides: {exc}")
    if written:
        _log(log_cb, f"Applied {written} override file(s).")
    return written


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class ModpackInstallResult:
    ok: bool = False
    path: str = ""
    loader: str = ""
    minecraft_version: str = ""
    loader_version: str = ""
    server_install: Optional[sl.ServerInstallResult] = None
    download: Optional["dl.DownloadResult"] = None
    overrides_applied: int = 0
    messages: list[str] = field(default_factory=list)
    failed_reason: str = ""
    cancelled: bool = False

    def summary(self) -> str:
        if self.cancelled:
            return "Modpack install cancelled."
        if not self.ok:
            return f"Modpack install failed: {self.failed_reason or 'unknown error'}"
        bits = [f"Installed {self.loader or 'server'} modpack"]
        if self.minecraft_version:
            bits.append(f"for Minecraft {self.minecraft_version}")
        if self.download is not None:
            n_dl = len(self.download.downloaded)
            n_fail = len(self.download.failed)
            piece = f"({n_dl} mod(s) downloaded"
            if n_fail:
                piece += f", {n_fail} failed"
            piece += ")"
            bits.append(piece)
        if self.overrides_applied:
            bits.append(f"+ {self.overrides_applied} config file(s)")
        return " ".join(bits)


# --------------------------------------------------------------------------- #
# Core installer (from a local .mrpack)
# --------------------------------------------------------------------------- #
def install_modpack_from_mrpack(
    mrpack: str | Path,
    target: str | Path,
    *,
    accept_eula: bool = False,
    log_cb: LogCb = None,
    cancel=None,
    progress_cb: ProgressCb = None,
) -> ModpackInstallResult:
    """Install a server from a local ``.mrpack`` file. Never raises."""
    target = Path(target).expanduser()
    res = ModpackInstallResult(path=str(target))

    # 1. Parse the index.
    try:
        index = read_index_from_mrpack(mrpack)
    except ValueError as exc:
        res.failed_reason = str(exc)
        res.messages.append(res.failed_reason)
        return res

    loader, loader_version, mc = loader_from_index(index)
    res.loader = loader
    res.loader_version = loader_version
    res.minecraft_version = mc
    _log(log_cb, f"Modpack targets {loader} {loader_version} on Minecraft {mc or '?'}.")

    if cancel is not None and cancel.is_set():
        res.cancelled = True
        res.failed_reason = "cancelled"
        return res

    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        res.failed_reason = f"cannot create target folder: {exc}"
        res.messages.append(res.failed_reason)
        return res

    # 2. Install the dedicated server loader.
    _log(log_cb, "Installing dedicated server loader…")
    install = sl.install_server_loader(
        target,
        minecraft_version=mc,
        loader=loader,
        loader_version=loader_version,
        log_cb=log_cb,
        cancel=cancel,
    )
    res.server_install = install
    res.messages.extend(install.messages)
    if install.cancelled:
        res.cancelled = True
        res.failed_reason = "cancelled"
        return res
    if not install.ok:
        # Non-fatal: we still lay down mods/configs so a user can finish manually.
        _log(log_cb, "! Server loader did not install cleanly; continuing with mods/config.")

    # 3. Stage the index where the downloader looks for it.
    try:
        staged = target / ".crucible" / "source-pack" / _INDEX_NAME
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps(index, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        res.failed_reason = f"could not stage pack index: {exc}"
        res.messages.append(res.failed_reason)
        return res

    if cancel is not None and cancel.is_set():
        res.cancelled = True
        res.failed_reason = "cancelled"
        return res

    # 4. Download server-side mods listed in the pack.
    _log(log_cb, "Downloading server-side mods from the pack…")
    try:
        download = dl.download_pack_mods(
            target,
            progress_cb=progress_cb,
            log_cb=log_cb,
            cancel=cancel,
        )
        res.download = download
        res.messages.append(download.summary())
        if download.cancelled:
            res.cancelled = True
            res.failed_reason = "cancelled"
            return res
    except Exception as exc:  # noqa: BLE001 - downloader shouldn't raise, but be safe
        _log(log_cb, f"! mod download error: {exc}")
        res.messages.append(f"mod download error: {exc}")

    # 5. Apply overrides (config files etc.).
    res.overrides_applied = apply_overrides(mrpack, target, log_cb=log_cb)

    # 6. Drop start script + server.properties + eula (only when missing).
    try:
        start = target / "start.sh"
        if not start.exists():
            start.write_text(_prism._START_SH, encoding="utf-8")
            try:
                import os
                os.chmod(start, 0o755)
            except OSError:
                pass
        _prism._write_default_server_properties(target)
        _prism._write_eula(target, accept_eula)
    except Exception as exc:  # noqa: BLE001
        _log(log_cb, f"! could not write support files: {exc}")
        res.messages.append(f"could not write support files: {exc}")

    res.ok = True
    _log(log_cb, "✓ " + res.summary())
    return res


# --------------------------------------------------------------------------- #
# Convenience: install straight from a Modrinth project id/slug
# --------------------------------------------------------------------------- #
def install_modpack_from_modrinth(
    project_id: str,
    target: str | Path,
    *,
    mc_version: str = "",
    loader: str = "",
    accept_eula: bool = False,
    log_cb: LogCb = None,
    cancel=None,
    progress_cb: ProgressCb = None,
) -> ModpackInstallResult:
    """Resolve a Modrinth modpack project, download its ``.mrpack`` and install it.

    Never raises.
    """
    target = Path(target).expanduser()
    res = ModpackInstallResult(path=str(target))

    # Resolve the newest matching .mrpack version.
    _log(log_cb, f"Resolving modpack {project_id} on Modrinth…")
    try:
        pack = modrinth.resolve_modpack(project_id, mc_version=mc_version, loader=loader)
    except modrinth.ModrinthError as exc:
        res.failed_reason = f"could not resolve modpack: {exc}"
        res.messages.append(res.failed_reason)
        return res
    except Exception as exc:  # noqa: BLE001
        res.failed_reason = f"could not resolve modpack: {exc}"
        res.messages.append(res.failed_reason)
        return res

    if cancel is not None and cancel.is_set():
        res.cancelled = True
        res.failed_reason = "cancelled"
        return res

    # Download the .mrpack to a temp location under the target.
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        res.failed_reason = f"cannot create target folder: {exc}"
        res.messages.append(res.failed_reason)
        return res

    mrpack_path = target / ".crucible" / "source-pack" / (pack.filename or "modpack.mrpack")
    _log(log_cb, f"Downloading {pack.filename or 'modpack'} ({pack.version_number})…")
    try:
        sl._download_to(pack.url, mrpack_path, cb=log_cb, cancel=cancel)
    except Exception as exc:  # noqa: BLE001
        if str(exc) == "cancelled" or (cancel is not None and cancel.is_set()):
            res.cancelled = True
            res.failed_reason = "cancelled"
        else:
            res.failed_reason = f"could not download modpack: {exc}"
        res.messages.append(res.failed_reason)
        return res

    # Hand off to the local-file installer.
    return install_modpack_from_mrpack(
        mrpack_path,
        target,
        accept_eula=accept_eula,
        log_cb=log_cb,
        cancel=cancel,
        progress_cb=progress_cb,
    )
