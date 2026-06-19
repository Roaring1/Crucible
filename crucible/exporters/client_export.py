"""
crucible/exporters/client_export.py

Turn a server instance into a ready-to-launch *client* instance in one click.

Three formats, all built offline from the files already on disk (the actual mod
jars are bundled, so nothing needs re-downloading):

  * mrpack     - Modrinth pack (.mrpack); import in Modrinth App or Prism
  * prism      - MultiMC/Prism instance .zip; "Add Instance -> Import from zip"
  * curseforge - CurseForge-style manifest zip with overrides

A small blocklist of known server-only jars is skipped so the client doesn't
choke on them.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_PRISM_UID = {
    "forge": "net.minecraftforge",
    "neoforge": "net.neoforged",
    "fabric": "net.fabricmc.fabric-loader",
    "quilt": "org.quiltmc.quilt-loader",
}
_MRPACK_DEP = {
    "forge": "forge",
    "neoforge": "neoforge",
    "fabric": "fabric-loader",
    "quilt": "quilt-loader",
}
_SERVER_ONLY_HINTS = ("spark", "server-only", "serverutilities", "ledger")


@dataclass
class ExportResult:
    path: str = ""
    fmt: str = ""
    mod_count: int = 0
    skipped: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.path)

    def summary(self) -> str:
        if not self.ok:
            return self.error or "export failed"
        s = f"{self.fmt} client with {self.mod_count} mod(s)"
        if self.skipped:
            s += f" ({len(self.skipped)} server-only skipped)"
        return s


def _enabled_mods(server_path: Path) -> list:
    mods = server_path / "mods"
    if not mods.is_dir():
        return []
    return sorted(p for p in mods.glob("*.jar") if p.is_file())


def _is_server_only(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in _SERVER_ONLY_HINTS)


def _config_dirs(server_path: Path) -> list:
    out = []
    for d in ("config", "kubejs", "defaultconfigs"):
        p = server_path / d
        if p.is_dir():
            out.append(p)
    return out


def export(instance, out_path, fmt: str = "mrpack", *,
           include_config: bool = True) -> ExportResult:
    """Build a client instance file from a server instance."""
    server = Path(getattr(instance, "path"))
    name = getattr(instance, "name", "Server")
    mc = (getattr(instance, "minecraft_version", "") or "").strip()
    loader = (getattr(instance, "loader", "") or "vanilla").strip().lower()
    loader_version = (getattr(instance, "loader_version", "") or "").strip()
    out_path = Path(out_path)

    if not server.is_dir():
        return ExportResult(error=f"Server folder not found: {server}")
    if not mc:
        return ExportResult(error=(
            "Unknown Minecraft version for this server - set it first "
            "(Info tab) so the client can match."))

    ship, skipped = [], []
    for j in _enabled_mods(server):
        (skipped if _is_server_only(j.name) else ship).append(j)
    cfg = _config_dirs(server) if include_config else []

    try:
        if fmt == "mrpack":
            _build_mrpack(out_path, name, mc, loader, loader_version, ship, cfg)
        elif fmt == "prism":
            _build_prism(out_path, name, mc, loader, loader_version, ship, cfg)
        elif fmt == "curseforge":
            _build_curseforge(out_path, name, mc, loader, loader_version, ship, cfg)
        else:
            return ExportResult(error=f"Unknown format: {fmt}")
    except (OSError, zipfile.BadZipFile) as e:
        return ExportResult(error=f"Could not write {fmt} file: {e}")

    return ExportResult(path=str(out_path), fmt=fmt, mod_count=len(ship),
                        skipped=[p.name for p in skipped])


def _add_dir(zf: zipfile.ZipFile, src: Path, arc_prefix: str) -> None:
    for f in src.rglob("*"):
        if f.is_file():
            zf.write(f, f"{arc_prefix}/{f.relative_to(src.parent).as_posix()}")


def _build_mrpack(out_path, name, mc, loader, loader_version, jars, cfg_dirs):
    deps = {"minecraft": mc}
    key = _MRPACK_DEP.get(loader)
    if key and loader != "vanilla":
        deps[key] = loader_version or "*"
    index = {
        "formatVersion": 1, "game": "minecraft", "versionId": "1.0.0",
        "name": name,
        "summary": f"Client export of {name} (built by Crucible)",
        "files": [], "dependencies": deps,
    }
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index, indent=2))
        for j in jars:
            zf.write(j, f"overrides/mods/{j.name}")
        for d in cfg_dirs:
            _add_dir(zf, d, "overrides")


def _build_prism(out_path, name, mc, loader, loader_version, jars, cfg_dirs):
    components = [{"uid": "net.minecraft", "version": mc}]
    uid = _PRISM_UID.get(loader)
    if uid and loader != "vanilla":
        comp = {"uid": uid}
        if loader_version:
            comp["version"] = loader_version
        components.append(comp)
    mmc_pack = {"components": components, "formatVersion": 1}
    instance_cfg = (
        "[General]\nConfigVersion=1.2\n"
        f"name={name}\nInstanceType=OneSix\niconKey=default\n"
    )
    folder = _safe_name(name)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/instance.cfg", instance_cfg)
        zf.writestr(f"{folder}/mmc-pack.json", json.dumps(mmc_pack, indent=2))
        for j in jars:
            zf.write(j, f"{folder}/.minecraft/mods/{j.name}")
        for d in cfg_dirs:
            _add_dir(zf, d, f"{folder}/.minecraft")


def _build_curseforge(out_path, name, mc, loader, loader_version, jars, cfg_dirs):
    cf_loader = f"{loader}-{loader_version}" if loader_version else loader
    manifest = {
        "minecraft": {"version": mc,
                      "modLoaders": [{"id": cf_loader, "primary": True}]},
        "manifestType": "minecraftModpack", "manifestVersion": 1,
        "name": name, "author": "Crucible", "files": [],
        "overrides": "overrides",
    }
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for j in jars:
            zf.write(j, f"overrides/mods/{j.name}")
        for d in cfg_dirs:
            _add_dir(zf, d, "overrides")


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in name)
    return keep.strip() or "Client"
