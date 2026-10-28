"""
crucible/exporters/client_export.py

Turn a server instance into a ready-to-launch *client* instance in one click.

Three formats, all built offline from the files already on disk (the actual mod
jars are bundled, so nothing needs re-downloading):

  * mrpack     - Modrinth pack (.mrpack); import in Modrinth App or Prism
  * prism      - MultiMC/Prism instance .zip; "Add Instance -> Import from zip"
  * curseforge - CurseForge-style manifest zip with overrides

Explicit loader environment metadata excludes server-only jars. Unknown sides
are preserved and reported, not inferred from filenames.
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
    warnings: list = field(default_factory=list)

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
    # defaultconfigs/ is server-side defaults and can contain values meant to be copied
    # to a server-created world, not ordinary player configuration. Keep it out
    # of client exports unless a future explicit option opts in after showing
    # the included paths.
    for d in ("config", "kubejs"):
        p = server_path / d
        if p.is_dir():
            out.append(p)
    return out


def export(instance, out_path, fmt: str = "mrpack", *,
           include_config: bool = True, client_source: str = "") -> ExportResult:
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

    from ..mods.mod_manager import ModManager, ModEntry
    inspector = ModManager(instance)
    ship, skipped, unknown = [], [], []
    for j in _enabled_mods(server):
        if j.is_symlink():
            return ExportResult(error="Symlinked mods are not exported: " + j.name)
        entry = ModEntry(j.name, j, True, j.stat().st_size)
        inspector.inspect_jar(entry)
        if entry.side == "server":
            skipped.append(j)
        else:
            ship.append(j)
            if entry.side == "unknown": unknown.append(j.name)
    if client_source:
        source = Path(client_source).expanduser()
        pack_file = source / "mmc-pack.json"
        try:
            pack = json.loads(pack_file.read_text())
            components = {c["uid"]: c.get("version", "") for c in pack["components"]}
        except (OSError, ValueError, KeyError, TypeError):
            return ExportResult(error="Choose a fully installed Prism/MultiMC instance containing mmc-pack.json.")
        if components.get("net.minecraft") != mc:
            return ExportResult(error="The client source uses a different Minecraft version. No archive was written.")
        uid = _PRISM_UID.get(loader)
        if uid and (uid not in components or (loader_version and components[uid] != loader_version)):
            return ExportResult(error="The client source uses a different loader/version. No archive was written.")
        client_mods = source / ".minecraft" / "mods"
        if not client_mods.is_dir(): client_mods = source / "minecraft" / "mods"
        if not client_mods.is_dir(): return ExportResult(error="The source instance has no installed mods folder.")
        names = {j.name for j in ship}
        for j in sorted(client_mods.glob("*.jar")):
            if j.is_symlink() or j.name in names: continue
            entry = ModEntry(j.name, j, True, j.stat().st_size)
            inspector.inspect_jar(entry)
            # Restore only explicitly client-only mods. Never mix an older shared mod in.
            if entry.side == "client": ship.append(j); names.add(j.name)
    excluded_defaultconfigs = include_config and (server / "defaultconfigs").is_dir()
    cfg = _config_dirs(server) if include_config else []

    if fmt not in {"mrpack", "prism", "curseforge"}:
        return ExportResult(error=f"Unknown format: {fmt}")
    if loader != "vanilla" and loader and not loader_version:
        return ExportResult(error="Set the exact loader version in Setup before exporting a client; guessing a loader can make the pack unlaunchable.")
    if any(out_path.resolve() == p.resolve() for p in ship):
        return ExportResult(error="The export destination cannot overwrite a mod JAR.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".partial")
    try:
        partial.unlink(missing_ok=True)
        if fmt == "mrpack":
            _build_mrpack(partial, name, mc, loader, loader_version, ship, cfg)
        elif fmt == "prism":
            _build_prism(partial, name, mc, loader, loader_version, ship, cfg)
        else:
            _build_curseforge(partial, name, mc, loader, loader_version, ship, cfg)
        with zipfile.ZipFile(partial, "r") as check:
            bad = check.testzip()
            if bad:
                raise OSError(f"ZIP verification failed at {bad}")
        partial.replace(out_path)
    except (OSError, zipfile.BadZipFile) as e:
        partial.unlink(missing_ok=True)
        return ExportResult(error=f"Could not write {fmt} file: {e}")

    warnings = ([f"{len(unknown)} mod(s) have unknown client compatibility; test this pack in Prism before sharing."] if unknown else [])
    if excluded_defaultconfigs:
        warnings.append("defaultconfigs/ was excluded from the client pack; review whether any required player config belongs in config/ or kubejs/.")
    warnings.extend([
        "Server packs may not include required client-side mods. Only explicitly client-only files from the optional matching Prism source were added.",
        "Bundled mod JARs are for personal use; check authors’ licenses before redistribution.",
    ])
    return ExportResult(path=str(out_path), fmt=fmt, mod_count=len(ship),
                        skipped=[p.name for p in skipped], warnings=warnings)


def _add_dir(zf: zipfile.ZipFile, src: Path, arc_prefix: str) -> None:
    root = src.resolve()
    for f in src.rglob("*"):
        if not f.is_file() or f.is_symlink():
            continue
        resolved = f.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        zf.write(resolved, f"{arc_prefix}/{f.relative_to(src.parent).as_posix()}")


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
        f"name={name.replace(chr(10), ' ').replace(chr(13), ' ')}\nInstanceType=OneSix\niconKey=default\n"
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
