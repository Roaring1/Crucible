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
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..mods import distribution_catalog

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
# Only these KubeJS subfolders are appropriate to share with a client. In
# particular, server_scripts/ and generated data/export dumps are never shipped.
_KUBEJS_CLIENT_DIRS = frozenset({"client_scripts", "startup_scripts", "assets", "config"})
_ALREADY_COMPRESSED_SUFFIXES = frozenset({
    ".jar", ".zip", ".mrpack", ".7z", ".rar", ".gz", ".xz", ".zst", ".pdf",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ogg", ".mp3", ".mp4", ".m4a",
})


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
    input_fingerprint: str = ""

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


def _fingerprint_input_tree(root: Path, *, label: str, top_level_jars: bool = False) -> list[dict]:
    """Hash files that can affect an export without following symlinks."""
    if root.is_symlink():
        return [{"label": label, "state": "symlink-root"}]
    if not root.exists():
        return [{"label": label, "state": "missing"}]
    if not root.is_dir():
        return [{"label": label, "state": "not-directory"}]

    if top_level_jars:
        candidates = sorted(root.glob("*.jar"), key=lambda item: (item.name.casefold(), item.name))
    else:
        candidates = sorted(root.rglob("*"), key=lambda item: (item.as_posix().casefold(), item.as_posix()))

    rows: list[dict] = []
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append({"label": label, "path": relative, "state": "symlink"})
            continue
        if path.is_dir():
            if not top_level_jars:
                rows.append({"label": label, "path": relative, "state": "directory"})
            continue
        if not path.is_file():
            # The exporter rejects special entries under configuration roots;
            # fail closed here rather than letting a cache hide the problem.
            raise OSError(f"Cannot fingerprint special input file: {relative}")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        signature_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        signature_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if signature_before != signature_after:
            raise OSError(f"Input changed while its fingerprint was computed: {relative}")
        rows.append({
            "label": label,
            "path": relative,
            "size": after.st_size,
            "sha256": digest.hexdigest(),
        })
    return rows


def compute_input_fingerprint(
    instance,
    *,
    client_source: str = "",
    include_config: bool = True,
    include_resourcepacks: bool = False,
    fmt: str = "prism",
) -> str:
    """Return a deterministic hash of the source data and options for a client export.

    The fingerprint is opaque: local absolute paths contribute to the hash but
    are never embedded in the archive. File content hashes, relative names,
    exact game/loader versions, source-profile identity and export options are
    included. Excluded resource packs are represented only by their presence,
    because that affects the review warning but not the archive payload.
    """
    server = Path(getattr(instance, "path")).expanduser()
    if not server.is_dir() or server.is_symlink():
        raise OSError("The server folder is unavailable for fingerprinting.")
    server = server.resolve(strict=True)
    name = getattr(instance, "name", "Server")
    mc = (getattr(instance, "minecraft_version", "") or "").strip()
    loader = (getattr(instance, "loader", "vanilla") or "vanilla").strip().lower()
    loader_version = (getattr(instance, "loader_version", "") or "").strip()

    source: Path | None = None
    client_root: Path | None = None
    if client_source:
        source = Path(client_source).expanduser()
        if source.is_symlink() or not source.is_dir():
            raise OSError("The selected Prism source is unavailable for fingerprinting.")
        source = source.resolve(strict=True)
        prism_root = source / ".minecraft"
        multimc_root = source / "minecraft"
        if prism_root.is_symlink() or multimc_root.is_symlink():
            raise OSError("The selected Prism source has a symbolic-link Minecraft directory.")
        if (prism_root / "mods").is_dir():
            client_root = prism_root
        elif (multimc_root / "mods").is_dir():
            client_root = multimc_root
        else:
            raise OSError("The selected Prism source has no installed mods folder.")
        mmc_pack = source / "mmc-pack.json"
        if mmc_pack.is_symlink() or not mmc_pack.is_file():
            raise OSError("The selected Prism source has no regular mmc-pack.json.")

    records: list[dict] = []
    records.append({"label": "distribution-catalog", "revision": distribution_catalog.catalog_revision()})
    records.extend(_fingerprint_input_tree(server / "mods", label="server-mods", top_level_jars=True))
    if include_config:
        records.extend(_fingerprint_input_tree(server / "config", label="server-config"))
        records.extend(_fingerprint_input_tree(server / "kubejs", label="server-kubejs"))
    records.append({
        "label": "defaultconfigs-review-warning",
        "present": bool(include_config and (server / "defaultconfigs").is_dir()),
    })

    client_resourcepacks_present = False
    client_shaderpacks_present = False
    if source is not None and client_root is not None:
        mmc = source / "mmc-pack.json"
        records.append({
            "label": "client-mmc-pack",
            "sha256": _sha256_file(mmc),
            "size": mmc.stat().st_size,
        })
        records.extend(_fingerprint_input_tree(client_root / "mods", label="client-mods", top_level_jars=True))
        if include_config:
            records.extend(_fingerprint_input_tree(client_root / "config", label="client-config"))
            records.extend(_fingerprint_input_tree(client_root / "kubejs", label="client-kubejs"))

        resourcepacks = client_root / "resourcepacks"
        shaderpacks = client_root / "shaderpacks"
        client_resourcepacks_present = resourcepacks.is_symlink() or (
            resourcepacks.is_dir() and any(resourcepacks.iterdir())
        )
        client_shaderpacks_present = shaderpacks.is_symlink() or (
            shaderpacks.is_dir() and any(shaderpacks.iterdir())
        )
        if include_resourcepacks:
            records.extend(_fingerprint_input_tree(resourcepacks, label="client-resourcepacks"))
        records.append({"label": "client-resourcepacks-review-warning", "present": client_resourcepacks_present})
        records.append({"label": "client-shaderpacks-review-warning", "present": client_shaderpacks_present})

    material = {
        "schema": 1,
        "format": fmt,
        "server_root": str(server),
        "client_source": str(source) if source is not None else "",
        "instance_name": str(name),
        "minecraft_version": mc,
        "loader": loader,
        "loader_version": loader_version,
        "include_config": bool(include_config),
        "include_resourcepacks": bool(include_resourcepacks),
        "inputs": records,
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _archive_mod_member(fmt: str, instance_name: str, filename: str) -> str:
    if fmt == "prism":
        return f"{_safe_name(instance_name)}/.minecraft/mods/{filename}"
    return f"overrides/mods/{filename}"


def _write_download_requirements(archive_path: Path, fmt: str, instance_name: str, requirements: list[dict]) -> None:
    """Embed transparent manual-install instructions for curated non-redistributable artifacts."""
    if not requirements:
        return
    prefix = _safe_name(instance_name) if fmt == "prism" else "overrides"
    guide_path = f"{prefix}/CRUCIBLE-MOD-DOWNLOADS.txt"
    lines = [
        "Crucible did not bundle the following exact JAR artifact(s) because its curated",
        "distribution catalog marks those bytes as prohibited for redistribution.",
        "This instance may not start until you obtain and install the required mod(s).",
        "",
        "For each item below, use the reviewed alternative download page and choose the",
        "compatible version. Import/open the pack in the target launcher first, then",
        "close that instance before copying the downloaded JAR into its mods folder.",
        "For Prism this is typically .minecraft/mods/; then relaunch the instance.",
        "Do not copy the original server JAR; only use a source you are allowed to download.",
        "",
    ]
    for index, item in enumerate(requirements, start=1):
        lines.extend([
            f"{index}. {item['filename']} ({item.get('modId') or 'mod ID not recorded'} {item.get('modVersion') or ''})".rstrip(),
            f"   Project: {item['projectUrl']}",
            f"   Reviewed alternative download: {item['alternativeDownloadUrl']}",
            f"   Catalog evidence: {item['evidenceUrl']}",
            f"   Evidence checked: {item['verifiedOn']}",
            f"   SHA-256 of the excluded local artifact: {item['sha256']}",
            "",
        ])
    lines.extend([
        "The alternative page was curated as a permitted source, but this file cannot",
        "verify what you ultimately download. Confirm the mod/version and its terms.",
        "",
    ])
    with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        if guide_path in archive.namelist():
            raise OSError("Generated mod-download instructions collide with an existing archive member.")
        _validate_archive_member_name(guide_path)
        archive.writestr(guide_path, "\n".join(lines))


def _is_server_only(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in _SERVER_ONLY_HINTS)


def _merge_config_dirs(
    server_path: Path,
    client_minecraft_root: Path | None,
    temporary_root: Path,
    *,
    include_config: bool,
    include_resourcepacks: bool,
) -> tuple[list[Path], list[str]]:
    """Stage client-shareable config/KubeJS and optional resource packs safely."""
    merged_roots: list[Path] = []
    warnings: list[str] = []

    for dirname in (("config", "kubejs") if include_config else ()):
        sources = [(server_path / dirname, "server")]
        if client_minecraft_root is not None:
            sources.append((client_minecraft_root / dirname, "client"))
        sources = [(path, kind) for path, kind in sources if path.is_dir()]
        if not sources:
            continue

        destination_root = temporary_root / dirname
        destination_root.mkdir(parents=True, exist_ok=True)
        for source_root, source_kind in sources:
            if source_root.is_symlink():
                raise OSError(f"Refusing to merge symbolic-link {source_kind} configuration root: {dirname}/")
            root = source_root.resolve(strict=True)
            excluded_kubejs_roots: set[str] = set()
            for entry in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
                relative = entry.relative_to(source_root)
                if dirname == "kubejs" and relative.parts and relative.parts[0] not in _KUBEJS_CLIENT_DIRS:
                    excluded_root = relative.parts[0]
                    if excluded_root not in excluded_kubejs_roots:
                        excluded_kubejs_roots.add(excluded_root)
                        if excluded_root == "server_scripts":
                            warnings.append(
                                "Excluded KubeJS/server_scripts/ from the player pack; server-side scripts stay on the managed server."
                            )
                        elif excluded_root in {"data", "exported"}:
                            warnings.append(
                                f"Excluded KubeJS/{excluded_root}/ from the player pack; data/generated files stay on the server."
                            )
                        else:
                            suffix = "/" if (source_root / excluded_root).is_dir() else ""
                            warnings.append(
                                f"Excluded KubeJS/{excluded_root}{suffix} from the player pack because it is not in the client-safe folder allowlist."
                            )
                    continue
                if entry.is_symlink():
                    warnings.append(
                        f"Excluded symlink from client configuration: {relative.as_posix()}."
                    )
                    continue
                if entry.is_dir():
                    continue
                if not entry.is_file():
                    raise OSError(
                        f"Special files are not exported from {source_kind} {dirname}/: {relative.as_posix()}"
                    )
                resolved = entry.resolve(strict=True)
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise OSError(
                        f"Configuration file escapes its {source_kind} root: {relative.as_posix()}"
                    ) from exc

                target = destination_root / relative
                if target.exists():
                    if _sha256_file(target) != _sha256_file(resolved):
                        if source_kind == "client":
                            warnings.append(
                                f"Client profile configuration differs from the server copy; the server file was kept: "
                                f"{dirname}/{relative.as_posix()}."
                            )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(resolved, target)

        merged_roots.append(destination_root)

    if include_resourcepacks and client_minecraft_root is not None:
        source_root = client_minecraft_root / "resourcepacks"
        if source_root.is_dir():
            if source_root.is_symlink():
                raise OSError("Refusing to package a symbolic-link resourcepacks/ directory.")
            resolved_root = source_root.resolve(strict=True)
            destination_root = temporary_root / "resourcepacks"
            destination_root.mkdir(parents=True, exist_ok=True)
            copied = 0
            for entry in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
                relative = entry.relative_to(source_root)
                if entry.is_symlink():
                    warnings.append(
                        f"Excluded symlink from client resource packs: {relative.as_posix()}."
                    )
                    continue
                if entry.is_dir():
                    continue
                if not entry.is_file():
                    raise OSError(
                        f"Special files are not exported from client resourcepacks/: {relative.as_posix()}"
                    )
                resolved = entry.resolve(strict=True)
                try:
                    resolved.relative_to(resolved_root)
                except ValueError as exc:
                    raise OSError(
                        f"Resource pack file escapes its source root: {relative.as_posix()}"
                    ) from exc
                target = destination_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(resolved, target)
                copied += 1
            if copied:
                merged_roots.append(destination_root)
                warnings.append(
                    "Client resource packs from the selected Prism profile were included by explicit owner choice; review whether each pack is required and licensed for sharing."
                )
    # The same excluded KubeJS root can exist in both server and client source;
    # report a warning once rather than making the review list noisy.
    return merged_roots, list(dict.fromkeys(warnings))


def export(instance, out_path, fmt: str = "mrpack", *,
           include_config: bool = True, client_source: str = "",
           include_resourcepacks: bool = False,
           expected_input_fingerprint: str = "") -> ExportResult:
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
    if include_resourcepacks and not client_source:
        return ExportResult(error="Choose a matching Prism client source before including its resource packs.")

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

    client_minecraft_root = None
    client_resourcepacks_present = False
    client_shaderpacks_present = False
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
        client_minecraft_root = source / ".minecraft"
        client_mods = client_minecraft_root / "mods"
        if not client_mods.is_dir():
            client_minecraft_root = source / "minecraft"
            client_mods = client_minecraft_root / "mods"
        if not client_mods.is_dir():
            return ExportResult(error="The source instance has no installed mods folder.")
        if client_mods.is_symlink():
            return ExportResult(error="The client source mods directory is a symbolic link. No archive was written.")
        if include_config:
            for dirname in ("config", "kubejs"):
                candidate = client_minecraft_root / dirname
                if candidate.is_symlink():
                    return ExportResult(
                        error=f"The client source has a symbolic-link {dirname}/ directory. No archive was written."
                    )
                if candidate.exists() and not candidate.is_dir():
                    return ExportResult(
                        error=f"The client source {dirname} path is not a directory. No archive was written."
                    )
        resourcepacks_candidate = client_minecraft_root / "resourcepacks"
        shaderpacks_candidate = client_minecraft_root / "shaderpacks"
        client_resourcepacks_present = resourcepacks_candidate.is_symlink()
        if not resourcepacks_candidate.is_symlink() and resourcepacks_candidate.is_dir():
            try:
                client_resourcepacks_present = any(resourcepacks_candidate.iterdir())
            except OSError:
                client_resourcepacks_present = True
        client_shaderpacks_present = shaderpacks_candidate.is_symlink()
        if not shaderpacks_candidate.is_symlink() and shaderpacks_candidate.is_dir():
            try:
                client_shaderpacks_present = any(shaderpacks_candidate.iterdir())
            except OSError:
                client_shaderpacks_present = True
        if include_resourcepacks:
            if resourcepacks_candidate.is_symlink():
                return ExportResult(
                    error="The client source has a symbolic-link resourcepacks/ directory. No archive was written."
                )
            if resourcepacks_candidate.exists() and not resourcepacks_candidate.is_dir():
                return ExportResult(
                    error="The client source resourcepacks path is not a directory. No archive was written."
                )

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

    if fmt not in {"mrpack", "prism", "curseforge"}:
        return ExportResult(error=f"Unknown format: {fmt}")

    try:
        catalog = distribution_catalog.load_catalog()
    except (distribution_catalog.DistributionCatalogError, OSError) as exc:
        return ExportResult(error=f"Could not verify the curated mod distribution catalog; no client pack was written: {exc}")

    distribution_review = []
    required_downloads = []
    distributable_ship = []
    distribution_warnings = []
    for jar in ship:
        digest = _sha256_file(jar)
        member_path = _archive_mod_member(fmt, name, jar.name)
        record = catalog.lookup(digest)
        if record is None:
            review_row = {
                "path": member_path,
                "filename": jar.name,
                "sha256": digest,
                "modId": "",
                "modVersion": "",
                "decision": "unverified",
                "projectUrl": "",
                "evidenceUrl": "",
                "verifiedOn": "",
                "basis": "No curated record matches this exact JAR SHA-256.",
                "license": "",
                "licenseUrl": "",
                "conditions": [],
                "alternativeDownloadUrl": "",
                "notes": "",
                "bundled": True,
            }
            distributable_ship.append(jar)
            distribution_warnings.append(
                f"No curated distribution record matches the exact SHA-256 of {jar.name}; redistribution remains unverified."
            )
        else:
            review_row = record.review_entry(
                archive_path=member_path,
                filename=jar.name,
                bundled=record.decision != "prohibited",
            )
            if record.decision == "prohibited":
                if not record.alternative_download_url:
                    return ExportResult(error=(
                        f"Cannot build a complete player pack: the curated catalog marks {jar.name} "
                        f"(SHA-256 {digest}) as prohibited for redistribution and has no reviewed alternative download URL. "
                        f"Review the catalog evidence: {record.evidence_url}. No ZIP was written."
                    ))
                required_downloads.append({
                    "path": member_path,
                    "filename": jar.name,
                    "sha256": digest,
                    "modId": record.mod_id,
                    "modVersion": record.mod_version,
                    "projectUrl": record.project_url,
                    "alternativeDownloadUrl": record.alternative_download_url,
                    "evidenceUrl": record.evidence_url,
                    "verifiedOn": record.verified_on,
                    "notes": record.notes,
                })
                distribution_warnings.append(
                    f"Excluded {jar.name} because the curated catalog marks this exact artifact prohibited; "
                    "the ZIP includes manual instructions for a reviewed alternative download."
                )
            else:
                distributable_ship.append(jar)
                if record.decision == "conditional":
                    distribution_warnings.append(
                        f"Conditional distribution record for {jar.name}; review its listed conditions and evidence before sharing."
                    )
                else:
                    distribution_warnings.append(
                        f"Curated catalog permits bundling the exact artifact {jar.name} based on the recorded evidence; verify terms remain current."
                    )
        distribution_review.append(review_row)
    ship = distributable_ship

    if any(out_path.resolve() == p.resolve() for p in ship):
        return ExportResult(error="The export destination cannot overwrite a mod JAR.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".partial")
    try:
        if fmt == "prism":
            if expected_input_fingerprint:
                if not re.fullmatch(r"[0-9a-f]{64}", expected_input_fingerprint):
                    raise OSError("The expected input fingerprint is invalid.")
                input_fingerprint = expected_input_fingerprint
            else:
                input_fingerprint = compute_input_fingerprint(
                    instance,
                    client_source=client_source,
                    include_config=include_config,
                    include_resourcepacks=include_resourcepacks,
                    fmt=fmt,
                )
        else:
            input_fingerprint = ""
        # Merge server config first, then add client-profile-only config/KubeJS
        # files. Where the same relative path differs, the server's copy remains
        # authoritative and the conflict is surfaced for owner review.
        with tempfile.TemporaryDirectory(prefix="crucible-client-config-") as cfg_tmp:
            cfg, config_warnings = _merge_config_dirs(
                server,
                client_minecraft_root,
                Path(cfg_tmp),
                include_config=include_config,
                include_resourcepacks=include_resourcepacks,
            ) if include_config or include_resourcepacks else ([], [])
            partial.unlink(missing_ok=True)
            if fmt == "mrpack":
                build_warnings = _build_mrpack(partial, name, mc, loader, loader_version, ship, cfg)
            elif fmt == "prism":
                build_warnings = _build_prism(partial, name, mc, loader, loader_version, ship, cfg)
            else:
                build_warnings = _build_curseforge(partial, name, mc, loader, loader_version, ship, cfg)
            build_warnings = config_warnings + build_warnings
            _write_download_requirements(partial, fmt, name, required_downloads)
            manifest = _seal_archive(
                partial, fmt=fmt, name=name, minecraft_version=mc,
                loader=loader, loader_version=loader_version,
                input_fingerprint=input_fingerprint,
                distribution_catalog_version=catalog.catalog_version,
                distribution_catalog_revision=catalog.revision,
                distribution_review=distribution_review,
                required_downloads=required_downloads,
            )
            # Reading each archive member through _seal_archive also validates its
            # CRC. Keep memory use bounded for large player packs.
            with zipfile.ZipFile(partial, "r") as check:
                if manifest["path"] not in check.namelist():
                    raise OSError("ZIP verification failed: manifest missing")
                json.loads(check.read(manifest["path"]))
            archive_sha256 = _sha256_file(partial)
            if distribution_catalog.catalog_revision() != catalog.revision:
                raise OSError("The curated distribution catalog changed while the ZIP was being built. Restart the export and review it again.")
            if input_fingerprint and compute_input_fingerprint(
                instance,
                client_source=client_source,
                include_config=include_config,
                include_resourcepacks=include_resourcepacks,
                fmt=fmt,
            ) != input_fingerprint:
                raise OSError("Client-pack inputs changed before the ZIP could be finalized. Rebuild and review the stable source before sharing.")
            partial.replace(out_path)
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        partial.unlink(missing_ok=True)
        return ExportResult(error=f"Could not write {fmt} file: {e}")

    warnings = ([f"{len(unknown)} mod(s) have unknown client compatibility; test this pack in Prism before sharing."] if unknown else [])
    if excluded_defaultconfigs:
        warnings.append("defaultconfigs/ was excluded from the client pack; review whether any required player config belongs in config/ or kubejs/.")
    if client_resourcepacks_present and not include_resourcepacks:
        warnings.append(
            "The selected Prism profile contains resourcepacks/ content, but it was excluded by default. "
            "Rebuild with the resource-pack option enabled only if these packs are required and cleared for sharing."
        )
    if client_shaderpacks_present:
        warnings.append(
            "Shaderpacks from the selected Prism profile were excluded; Crucible does not bundle shaderpacks in player shares."
        )
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
    warnings.extend(distribution_warnings)
    warnings.extend(manifest["privacy_warnings"])
    warnings.extend([
        "A SHA-256 manifest is embedded in the archive. Review privacy warnings before sharing.",
        "Server packs may not include required client-side mods. Only explicitly client-only files from the optional matching Prism source were added.",
        "Distribution status is artifact-specific: follow catalog conditions, and check every JAR with no matching curated record before redistribution.",
    ])
    return ExportResult(
        path=str(out_path), fmt=fmt, mod_count=len(ship),
        skipped=[p.name for p in skipped], warnings=warnings,
        manifest_path=manifest["path"],
        included_files=[row["path"] for row in manifest["files"]],
        archive_sha256=archive_sha256,
        input_fingerprint=input_fingerprint,
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
        archive_name = f"{arc_prefix}/{f.relative_to(src.parent).as_posix()}"
        if resolved.suffix.lower() in _ALREADY_COMPRESSED_SUFFIXES:
            zf.write(resolved, archive_name, compress_type=zipfile.ZIP_STORED)
        else:
            zf.write(resolved, archive_name)
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
    input_fingerprint: str = "",
    distribution_catalog_version: str = "",
    distribution_catalog_revision: str = "",
    distribution_review: list[dict] | None = None,
    required_downloads: list[dict] | None = None,
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
    unsupported_config_count = 0
    unsupported_config_examples = []
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
            if _is_config_entry(member_name) and not text_candidate:
                unsupported_config_count += 1
                if len(unsupported_config_examples) < 10:
                    unsupported_config_examples.append(member_name)
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
    if unsupported_config_count:
        examples = ", ".join(unsupported_config_examples)
        more = unsupported_config_count - len(unsupported_config_examples)
        suffix = f" (and {more} more)" if more else ""
        privacy_warnings.append(
            f"Privacy scan skipped {unsupported_config_count} config/KubeJS file(s) with unrecognized file types: "
            f"{examples}{suffix}. Review these files manually before sharing."
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
        "inputFingerprint": input_fingerprint,
        "distributionCatalog": {
            "version": distribution_catalog_version,
            "revision": distribution_catalog_revision,
        },
        "distributionReview": distribution_review or [],
        "requiredDownloads": required_downloads or [],
        "privacyReview": {
            "scanner": "bounded heuristic pattern scan; this is not a guarantee that a pack contains no secrets",
            "scope": (
                "Scans eligible text files under included config/ and KubeJS/ roots only. "
                "Binary/unknown config files, oversized files, mod JARs, resource packs, and other payloads are not inspected for secrets."
            ),
            "coverageComplete": (
                not scan_incomplete_count
                and not unsupported_config_count
                and not binary_config_skipped
                and not privacy_findings_truncated
            ),
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
