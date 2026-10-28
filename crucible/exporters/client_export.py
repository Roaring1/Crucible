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

import hashlib
import json
import os
import re
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
    manifest_path: str = ""
    included_files: list = field(default_factory=list)
    archive_sha256: str = ""

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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    out_path = Path(out_path).expanduser()

    if not server.is_dir():
        return ExportResult(error=f"Server folder not found: {server}")
    if not mc:
        return ExportResult(error=(
            "Unknown Minecraft version for this server - set it first "
            "(Info tab) so the client can match."))
    if loader not in {"vanilla", *_PRISM_UID}:
        return ExportResult(error=f"Unsupported client-pack loader '{loader}'. Set a supported exact loader first.")
    if loader != "vanilla" and not loader_version:
        return ExportResult(error=(
            "Set the exact loader version in Setup before exporting a client; "
            "guessing a loader can make the pack unlaunchable."))

    # An export written inside the managed server could replace server.properties,
    # a config file, a world file, or another important input by accident.
    server_root = server.resolve()
    out_absolute = Path(os.path.abspath(str(out_path)))
    out_resolved = out_absolute.resolve()
    if (out_absolute == server_root or server_root in out_absolute.parents
            or out_resolved == server_root or server_root in out_resolved.parents):
        return ExportResult(error="Save the client export outside the server folder so it cannot overwrite server data.")
    if out_path.is_symlink():
        return ExportResult(error="The export destination cannot be a symbolic link.")
    if (server / "mods").is_symlink():
        return ExportResult(error="Refusing to export through a symbolic-link mods/ directory.")
    if include_config:
        for dirname in ("config", "kubejs"):
            candidate = server / dirname
            if candidate.is_symlink():
                return ExportResult(error=f"Refusing to export symbolic-link directory {dirname}/.")

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
        if source.is_symlink():
            return ExportResult(error="The selected Prism source is a symbolic link. Choose the real instance folder.")
        if not source.is_dir():
            return ExportResult(error="The selected Prism source folder does not exist.")
        # Never trust a metadata file that resolves outside the selected profile.
        pack_file = source / "mmc-pack.json"
        if pack_file.is_symlink():
            return ExportResult(error="The selected Prism source has a symbolic-link mmc-pack.json.")
        # Never overwrite the chosen Prism instance itself, even if the selected
        # destination is a file not otherwise included in the generated archive.
        source_root = source.resolve()
        if (out_absolute == source_root or source_root in out_absolute.parents
                or out_resolved == source_root or source_root in out_resolved.parents):
            return ExportResult(error="Save the client export outside the selected Prism source folder.")
        try:
            pack = json.loads(pack_file.read_text())
            components = {c["uid"]: c.get("version", "") for c in pack["components"]}
        except (OSError, ValueError, KeyError, TypeError):
            return ExportResult(error="Choose a fully installed Prism/MultiMC instance containing mmc-pack.json.")
        if components.get("net.minecraft") != mc:
            return ExportResult(error="The client source uses a different Minecraft version. No archive was written.")
        source_loader_uids = {
            component_uid: components[component_uid]
            for component_uid in _PRISM_UID.values()
            if component_uid in components
        }
        uid = _PRISM_UID.get(loader)
        if loader == "vanilla":
            if source_loader_uids:
                return ExportResult(error="The client source uses a mod loader, but this server is vanilla. No archive was written.")
        elif (
            uid not in components
            or any(source_uid != uid for source_uid in source_loader_uids)
            or components.get(uid) != loader_version
        ):
            return ExportResult(error="The client source uses a different loader/version. No archive was written.")

        for layout in (".minecraft", "minecraft"):
            if (source / layout).is_symlink():
                return ExportResult(error=f"The client source has a symbolic-link {layout}/ directory. No archive was written.")
        client_mods = source / ".minecraft" / "mods"
        if not client_mods.is_dir():
            client_mods = source / "minecraft" / "mods"
        if not client_mods.is_dir():
            return ExportResult(error="The source instance has no installed mods folder.")
        if client_mods.is_symlink():
            return ExportResult(error="The client source mods directory is a symbolic link. No archive was written.")

        names = {j.name for j in ship}
        hashes = {_sha256_file(j) for j in ship}
        client_unknown = []
        source_conflicts = []
        for j in sorted(client_mods.glob("*.jar")):
            if j.is_symlink():
                return ExportResult(error=f"The client source contains a symbolic-link mod: {j.name}. No archive was written.")
            digest = _sha256_file(j)
            if digest in hashes:
                continue
            entry = ModEntry(j.name, j, True, j.stat().st_size)
            inspector.inspect_jar(entry)
            # Shared server JARs are authoritative. Do not silently collide with a
            # different client-only JAR that has the same archive path.
            if j.name in names:
                if entry.side == "client":
                    return ExportResult(error=(
                        f"Client-only mod '{j.name}' conflicts with a different server JAR of the same name. "
                        "Resolve the conflict before exporting."))
                source_conflicts.append(j.name)
                continue
            if entry.side == "client":
                ship.append(j)
                names.add(j.name)
                hashes.add(digest)
            elif entry.side == "unknown":
                client_unknown.append(j.name)
            elif entry.side == "both":
                # A shared mod that isn't byte-identical to a server JAR may be
                # a stale or mismatched version. Do not silently drop it.
                source_conflicts.append(j.name)
    else:
        client_unknown = []
        source_conflicts = []

    excluded_defaultconfigs = include_config and (server / "defaultconfigs").is_dir()
    cfg = _config_dirs(server) if include_config else []

    if fmt not in {"mrpack", "prism", "curseforge"}:
        return ExportResult(error=f"Unknown format: {fmt}")
    if any(out_path.resolve() == p.resolve() for p in ship):
        return ExportResult(error="The export destination cannot overwrite a mod JAR.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".partial")
    try:
        partial.unlink(missing_ok=True)
        if fmt == "mrpack":
            build_warnings = _build_mrpack(partial, name, mc, loader, loader_version, ship, cfg)
        elif fmt == "prism":
            build_warnings = _build_prism(partial, name, mc, loader, loader_version, ship, cfg)
        else:
            build_warnings = _build_curseforge(partial, name, mc, loader, loader_version, ship, cfg)
        manifest = _seal_archive(
            partial, fmt=fmt, name=name, minecraft_version=mc,
            loader=loader, loader_version=loader_version,
        )
        # Reading each archive member through _seal_archive also validates its
        # CRC. Keep memory use bounded for large player packs.
        with zipfile.ZipFile(partial, "r") as check:
            if manifest["path"] not in check.namelist():
                raise OSError("ZIP verification failed: manifest missing")
            json.loads(check.read(manifest["path"]))
        archive_sha256 = _sha256_file(partial)
        partial.replace(out_path)
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        partial.unlink(missing_ok=True)
        return ExportResult(error=f"Could not write {fmt} file: {e}")

    warnings = ([f"{len(unknown)} mod(s) have unknown client compatibility; test this pack in Prism before sharing."] if unknown else [])
    if excluded_defaultconfigs:
        warnings.append("defaultconfigs/ was excluded from the client pack; review whether any required player config belongs in config/ or kubejs/.")
    if client_unknown:
        shown = ", ".join(client_unknown[:8])
        if len(client_unknown) > 8:
            shown += f", and {len(client_unknown) - 8} more"
        warnings.append(
            "Client-source mods with unknown client compatibility were not added: " + shown
        )
    if source_conflicts:
        unique_conflicts = sorted(set(source_conflicts))
        shown = ", ".join(unique_conflicts[:8])
        if len(unique_conflicts) > 8:
            shown += f", and {len(unique_conflicts) - 8} more"
        warnings.append(
            "Shared client-source mods that were not identical to a server JAR were not added; review versions and compatibility: " + shown
        )
    warnings.extend(build_warnings)
    warnings.extend(manifest["privacy_warnings"])
    warnings.extend([
        "A SHA-256 manifest is embedded in the archive. Review privacy warnings before sharing.",
        "Server packs may not include required client-side mods. Only explicitly client-only files from the optional matching Prism source were added.",
        "Bundled mod JARs are for personal use; check authors’ licenses before redistribution.",
    ])
    return ExportResult(
        path=str(out_path), fmt=fmt, mod_count=len(ship),
        skipped=[p.name for p in skipped], warnings=warnings,
        manifest_path=manifest["path"],
        included_files=[row["path"] for row in manifest["files"]],
        archive_sha256=archive_sha256,
    )


def _add_dir(zf: zipfile.ZipFile, src: Path, arc_prefix: str) -> list[str]:
    if src.is_symlink():
        raise OSError(f"Refusing to package symbolic-link directory: {src}")
    root = src.resolve(strict=True)
    warnings = []
    for f in src.rglob("*"):
        relative = f.relative_to(src)
        if f.is_symlink():
            warnings.append(
                f"Excluded symlink from client configuration: {relative.as_posix()}."
            )
            continue
        if f.is_dir():
            continue
        if not f.is_file():
            raise OSError(f"Refusing to package special file: {relative.as_posix()}")
        resolved = f.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise OSError(f"File escapes its configuration root: {relative.as_posix()}") from exc
        zf.write(resolved, f"{arc_prefix}/{f.relative_to(src.parent).as_posix()}")
    return warnings


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
    warnings = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index, indent=2))
        for j in jars:
            zf.write(j, f"overrides/mods/{j.name}", compress_type=zipfile.ZIP_STORED)
        for d in cfg_dirs:
            warnings.extend(_add_dir(zf, d, "overrides"))
    return warnings


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
    warnings = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/instance.cfg", instance_cfg)
        zf.writestr(f"{folder}/mmc-pack.json", json.dumps(mmc_pack, indent=2))
        for j in jars:
            zf.write(j, f"{folder}/.minecraft/mods/{j.name}", compress_type=zipfile.ZIP_STORED)
        for d in cfg_dirs:
            warnings.extend(_add_dir(zf, d, f"{folder}/.minecraft"))
    return warnings


def _build_curseforge(out_path, name, mc, loader, loader_version, jars, cfg_dirs):
    cf_loader = f"{loader}-{loader_version}" if loader_version else loader
    manifest = {
        "minecraft": {"version": mc,
                      "modLoaders": [{"id": cf_loader, "primary": True}]},
        "manifestType": "minecraftModpack", "manifestVersion": 1,
        "name": name, "author": "Crucible", "files": [],
        "overrides": "overrides",
    }
    warnings = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for j in jars:
            zf.write(j, f"overrides/mods/{j.name}", compress_type=zipfile.ZIP_STORED)
        for d in cfg_dirs:
            warnings.extend(_add_dir(zf, d, "overrides"))
    return warnings


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in name)
    return keep.strip() or "Client"

# Keep the per-file scan bounded. Mod JARs are binary and are not scanned for
# text secrets here; only included config/KubeJS entries are inspected. The
# manifest hashes every archive member, regardless of whether it is scannable.
_MAX_PRIVACY_FILE_BYTES = 1024 * 1024
_MAX_PRIVACY_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_PRIVACY_FINDINGS = 50
_TEXT_CONFIG_SUFFIXES = {
    "", ".cfg", ".conf", ".env", ".ini", ".json", ".json5", ".js", ".mjs",
    ".properties", ".ps1", ".sh", ".toml", ".txt", ".xml", ".yaml", ".yml",
    ".lua", ".properties",
}
_SECRET_PATTERNS = [
    ("credential assignment", re.compile(
        r'''(?im)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|passwd|webhook|authorization|auth[_-]?token)\b\s*[:=]\s*["']?([^\s"'#,;]{16,})'''
    )),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("Discord webhook", re.compile(r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]{20,}", re.IGNORECASE)),
    ("private-key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("AWS access-key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer credential", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.IGNORECASE)),
    ("private IPv4 address", re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    )),
]
_PLACEHOLDER_PREFIXES = (
    "your_", "your-", "replace_me", "replace-me", "change_me", "change-me",
    "changeme", "example_", "example-", "placeholder", "dummy_", "test_",
)


def _validate_archive_member_name(name: str) -> None:
    """Reject names that can escape or be interpreted differently by extractors."""
    if not name or name.startswith("/") or "\\" in name or "\x00" in name or re.match(r"^[A-Za-z]:", name):
        raise OSError(f"Unsafe archive path: {name[:160]!r}")
    parts = name.split("/")
    if len(name) > 4096 or any(part in {"", ".", ".."} for part in parts):
        raise OSError(f"Unsafe archive path: {name[:160]!r}")
    if any(ord(char) < 32 for char in name):
        raise OSError(f"Control character in archive path: {name[:160]!r}")


def _is_config_entry(name: str) -> bool:
    parts = name.split("/")
    return any(part in {"config", "kubejs"} for part in parts)


def _is_text_config_entry(name: str) -> bool:
    return _is_config_entry(name) and Path(name).suffix.lower() in _TEXT_CONFIG_SUFFIXES


def _privacy_findings(name: str, raw: bytes) -> list[str]:
    if b"\x00" in raw[:4096]:
        return []
    text = raw.decode("utf-8", "ignore")
    findings = []
    for category, pattern in _SECRET_PATTERNS:
        found = False
        for match in pattern.finditer(text):
            if category == "credential assignment":
                value = match.group(1)[:128].strip().strip("\"'").lower()
                if value in {"", "false", "null", "none", "changeme", "password", "example"}:
                    continue
                if value.startswith(_PLACEHOLDER_PREFIXES):
                    continue
            found = True
            break
        if found:
            findings.append(
                f"Privacy review: possible {category} in {name}; inspect this file before sharing (value not shown)."
            )
        if len(findings) >= _MAX_PRIVACY_FINDINGS:
            break
    return findings


def _seal_archive(
    archive_path: Path,
    *,
    fmt: str,
    name: str,
    minecraft_version: str,
    loader: str,
    loader_version: str,
) -> dict:
    """Validate member paths, hash all content, scan included config, and embed a manifest."""
    manifest_name = (
        f"{_safe_name(name)}/crucible-manifest.json"
        if fmt == "prism" else "overrides/crucible-manifest.json"
    )
    rows = []
    privacy_warnings = []
    seen = set()
    scan_bytes = 0
    scan_incomplete_count = 0
    scan_incomplete_examples = []
    binary_config_skipped = 0
    privacy_findings_truncated = False
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            member_name = info.filename
            _validate_archive_member_name(member_name)
            if member_name in seen:
                raise OSError(f"Duplicate archive path: {member_name[:160]!r}")
            seen.add(member_name)
            if info.is_dir():
                continue
            if member_name == manifest_name:
                raise OSError(f"Source files collide with generated manifest path: {manifest_name!r}")

            text_candidate = _is_text_config_entry(member_name)
            should_scan = False
            sample = bytearray()
            if text_candidate:
                if info.file_size > _MAX_PRIVACY_FILE_BYTES:
                    scan_incomplete_count += 1
                    if len(scan_incomplete_examples) < 10:
                        scan_incomplete_examples.append(member_name)
                elif scan_bytes + info.file_size > _MAX_PRIVACY_TOTAL_BYTES:
                    scan_incomplete_count += 1
                    if len(scan_incomplete_examples) < 10:
                        scan_incomplete_examples.append(member_name)
                else:
                    should_scan = True

            digest = hashlib.sha256()
            with zf.open(info, "r") as member:
                while True:
                    chunk = member.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    if should_scan:
                        sample.extend(chunk)

            if should_scan:
                scan_bytes += info.file_size
                if b"\x00" in sample[:4096]:
                    binary_config_skipped += 1
                    warning = (
                        f"Privacy scan skipped binary-looking config file {member_name}; "
                        "review it manually before sharing."
                    )
                    if len(privacy_warnings) < _MAX_PRIVACY_FINDINGS:
                        privacy_warnings.append(warning)
                    else:
                        privacy_findings_truncated = True
                else:
                    findings = _privacy_findings(member_name, bytes(sample))
                    remaining_findings = max(0, _MAX_PRIVACY_FINDINGS - len(privacy_warnings))
                    privacy_warnings.extend(findings[:remaining_findings])
                    if len(findings) > remaining_findings:
                        privacy_findings_truncated = True

            rows.append({
                "path": member_name,
                "size_bytes": info.file_size,
                "sha256": digest.hexdigest(),
            })

    if scan_incomplete_count:
        examples = ", ".join(scan_incomplete_examples)
        more = scan_incomplete_count - len(scan_incomplete_examples)
        suffix = f" (and {more} more)" if more else ""
        privacy_warnings.append(
            f"Privacy scan incomplete for {scan_incomplete_count} included config file(s): "
            f"{examples}{suffix}. Bounded size limits were reached; review these files manually before sharing."
        )
    if privacy_findings_truncated:
        privacy_warnings.append(
            f"Privacy finding display was capped at {_MAX_PRIVACY_FINDINGS}; review all included config files manually before sharing."
        )
    manifest_data = {
        "schemaVersion": 1,
        "generatedBy": "Crucible",
        "format": fmt,
        "minecraftVersion": minecraft_version,
        "loader": loader,
        "loaderVersion": loader_version,
        "manifestPath": manifest_name,
        "privacyReview": {
            "scanner": "bounded heuristic pattern scan; this is not a guarantee that a pack contains no secrets",
            "coverageComplete": not scan_incomplete_count and not binary_config_skipped and not privacy_findings_truncated,
            "warnings": privacy_warnings,
        },
        "files": rows,
    }
    payload = (json.dumps(manifest_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(manifest_name, payload)
    _validate_archive_member_name(manifest_name)
    return {
        "path": manifest_name,
        "files": rows,
        "privacy_warnings": privacy_warnings,
    }
