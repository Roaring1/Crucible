"""
Prism / MultiMC / Modrinth / CurseForge import support.

Crucible hosts dedicated servers; Prism launches clients. This importer bridges
that gap by creating a server-oriented staging folder from Prism-compatible
packs and fully installed Prism instances.

Best source: an installed Prism instance. Exported Modrinth/CurseForge packs
frequently contain only download indexes, so Crucible imports local overrides and
warns instead of attempting network downloads.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import tempfile
import stat
import zipfile
from dataclasses import dataclass, field
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 fallback; kept for packaging friendliness
    tomllib = None  # type: ignore[assignment]
from pathlib import Path

SERVER_SAFE_DIRS = (
    "mods",
    "config",
    "defaultconfigs",
    "kubejs",
    "scripts",
    "patchouli_books",
    "libraries",
)
SERVER_SAFE_FILES = (
    "server.properties",
    "whitelist.json",
    "ops.json",
    "banned-players.json",
    "banned-ips.json",
)
CLIENT_ONLY_NAMES = {
    "resourcepacks",
    "shaderpacks",
    "screenshots",
    "saves",
    "logs",
    "crash-reports",
    "texturepacks",
    "options.txt",
    "servers.dat",
}
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB expanded
_MAX_SINGLE_MEMBER = 2 * 1024 * 1024 * 1024   # 2 GiB
_MAX_COMPRESSION_RATIO = 1_000

_LOADER_UIDS = {
    "net.minecraftforge": "forge",
    "net.neoforged": "neoforge",
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
}

_START_SH = '''#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

JAVA_BIN="${JAVA_BIN:-java}"
JAVA_ARGS="${CRUCIBLE_JAVA_ARGS:--Xms2G -Xmx4G}"

# Modern Forge/NeoForge server installs often provide unix_args.txt under
# libraries/net/...; prefer that because it preserves the exact classpath.
if compgen -G "libraries/net/minecraftforge/forge/*/unix_args.txt" > /dev/null; then
  ARGS_FILE="$(ls -1 libraries/net/minecraftforge/forge/*/unix_args.txt | sort -V | tail -n 1)"
  exec "$JAVA_BIN" $JAVA_ARGS @"$ARGS_FILE" nogui
fi
if compgen -G "libraries/net/neoforged/neoforge/*/unix_args.txt" > /dev/null; then
  ARGS_FILE="$(ls -1 libraries/net/neoforged/neoforge/*/unix_args.txt | sort -V | tail -n 1)"
  exec "$JAVA_BIN" $JAVA_ARGS @"$ARGS_FILE" nogui
fi

# Fabric/Quilt/vanilla/Paper launchers commonly use these names.
for jar in fabric-server-launch.jar quilt-server-launch.jar server.jar minecraft_server*.jar paper*.jar forge-*-server.jar forge-*.jar neoforge-*.jar; do
  if [[ -f "$jar" ]]; then
    exec "$JAVA_BIN" $JAVA_ARGS -jar "$jar" nogui
  fi
done

cat >&2 <<'EOF'
Crucible could not find a runnable server jar/args file.

This import copied the Prism-compatible pack files that are useful for a
server, but client launchers usually do not include the dedicated server
launcher itself.

Next step:
  1. Install the matching dedicated server loader into this folder
     (Forge/NeoForge installer, Fabric server launcher, Quilt server launcher,
     Paper/Vanilla server jar, etc.).
  2. Re-run this script or start from Crucible.
EOF
exit 2
'''


@dataclass
class PrismPackInfo:
    source_type: str = "unknown"
    name: str = ""
    pack_version: str = ""
    minecraft_version: str = ""
    loader: str = ""
    loader_version: str = ""
    managed_type: str = ""
    managed_id: str = ""
    managed_version_id: str = ""
    game_root_name: str = "minecraft"
    warnings: list[str] = field(default_factory=list)

    @property
    def version_label(self) -> str:
        parts: list[str] = []
        if self.minecraft_version:
            parts.append(f"MC {self.minecraft_version}")
        if self.loader:
            loader = self.loader
            if self.loader_version:
                loader += f" {self.loader_version}"
            parts.append(loader)
        if self.pack_version:
            parts.append(f"pack {self.pack_version}")
        return " · ".join(parts)


@dataclass
class PrismImportPlan:
    source: Path
    game_root: Path
    info: PrismPackInfo
    cleanup_dir: Path | None = None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_instance_cfg(path: Path) -> dict[str, str]:
    """Parse Prism/MultiMC instance.cfg, which is INI-like without sections."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string("[instance]\n" + raw)
    except configparser.Error:
        return {}
    return dict(parser["instance"])


def _load_mmc_pack(instance_root: Path, info: PrismPackInfo) -> None:
    mmc = _read_json(instance_root / "mmc-pack.json")
    for comp in mmc.get("components", []) if isinstance(mmc, dict) else []:
        if not isinstance(comp, dict):
            continue
        uid = str(comp.get("uid", ""))
        version = str(comp.get("version", "") or comp.get("cachedVersion", ""))
        if uid == "net.minecraft":
            info.minecraft_version = version
        elif uid in _LOADER_UIDS:
            info.loader = _LOADER_UIDS[uid]
            info.loader_version = version


def _find_game_root(instance_root: Path, info: PrismPackInfo | None = None) -> Path:
    candidates: list[Path] = []
    if info and info.game_root_name:
        candidates.append(instance_root / info.game_root_name)
    candidates.extend([instance_root / "minecraft", instance_root / ".minecraft", instance_root])
    for candidate in candidates:
        if (candidate / "mods").exists() or (candidate / "config").exists():
            return candidate
    return candidates[0]


def _detect_instance_dir(path: Path) -> PrismImportPlan | None:
    cfg_path = path / "instance.cfg"
    if not cfg_path.exists():
        return None
    cfg = _parse_instance_cfg(cfg_path)
    info = PrismPackInfo(source_type="prism_instance")
    info.name = cfg.get("name") or cfg.get("InstanceName") or path.name
    info.managed_type = cfg.get("ManagedPackType", "")
    info.managed_id = cfg.get("ManagedPackID", "")
    info.managed_version_id = cfg.get("ManagedPackVersionID", "")
    info.pack_version = cfg.get("ManagedPackVersionName", "")
    if info.managed_type:
        info.source_type = f"prism_{info.managed_type}"
    _load_mmc_pack(path, info)
    return PrismImportPlan(source=path, game_root=_find_game_root(path, info), info=info)


def _extract_archive(path: Path) -> tuple[Path, list[str]]:
    tmp = Path(tempfile.mkdtemp(prefix="crucible-prism-import-"))
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            members = zf.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"Archive has too many entries ({len(members):,})")
            expanded = sum(m.file_size for m in members)
            if expanded > _MAX_ARCHIVE_BYTES:
                raise ValueError(f"Archive expands beyond {_MAX_ARCHIVE_BYTES // (1024**3)} GiB")
            for member in members:
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    warnings.append(f"Skipped archive symlink: {member.filename}")
                    continue
                if member.file_size > _MAX_SINGLE_MEMBER:
                    raise ValueError(f"Archive member is too large: {member.filename}")
                if (member.file_size > 100 * 1024 * 1024 and member.compress_size > 0
                        and member.file_size / member.compress_size > _MAX_COMPRESSION_RATIO):
                    raise ValueError(f"Suspicious compression ratio: {member.filename}")
                dest = (tmp / member.filename).resolve()
                try:
                    dest.relative_to(tmp.resolve())
                except ValueError:
                    warnings.append(f"Skipped unsafe archive path: {member.filename}")
                    continue
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, dest.open("wb") as out:
                    remaining = member.file_size
                    while remaining:
                        chunk = src.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                    if remaining:
                        raise ValueError(f"Truncated archive member: {member.filename}")
    except (zipfile.BadZipFile, ValueError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(exc, zipfile.BadZipFile):
            raise ValueError(f"Not a valid zip/mrpack archive: {path}") from exc
        raise
    return tmp, warnings


def _find_file(root: Path, name: str, max_depth: int = 4) -> Path | None:
    for p in root.rglob(name):
        try:
            depth = len(p.relative_to(root).parts)
        except ValueError:
            continue
        if depth <= max_depth:
            return p
    return None


def _detect_archive(path: Path) -> PrismImportPlan:
    tmp, warnings = _extract_archive(path)
    info = PrismPackInfo(name=path.stem, source_type="directory", warnings=warnings)

    cfg = _find_file(tmp, "instance.cfg")
    if cfg:
        plan = _detect_instance_dir(cfg.parent)
        if plan:
            plan.cleanup_dir = tmp
            plan.info.warnings.extend(warnings)
            plan.info.source_type = "multimc_zip"
            return plan

    mr = tmp / "modrinth.index.json"
    if mr.exists():
        obj = _read_json(mr)
        info.source_type = "modrinth"
        info.name = str(obj.get("name") or path.stem)
        info.pack_version = str(obj.get("versionId") or "")
        deps = obj.get("dependencies", {}) if isinstance(obj, dict) else {}
        if isinstance(deps, dict):
            info.minecraft_version = str(deps.get("minecraft", ""))
            for key, loader_name in (("forge", "forge"), ("neoforge", "neoforge"), ("fabric-loader", "fabric"), ("quilt-loader", "quilt")):
                if deps.get(key):
                    info.loader = loader_name
                    info.loader_version = str(deps.get(key))
                    break
        if obj.get("files"):
            info.warnings.append("Modrinth pack indexes remote files; imported local overrides only. Import a fully installed Prism instance to copy downloaded mods.")
        return PrismImportPlan(source=path, game_root=tmp / "overrides", info=info, cleanup_dir=tmp)

    manifest = tmp / "manifest.json"
    if manifest.exists():
        obj = _read_json(manifest)
        if obj.get("manifestType") == "minecraftModpack":
            info.source_type = "curseforge"
            info.name = str(obj.get("name") or path.stem)
            info.pack_version = str(obj.get("version") or "")
            mc = obj.get("minecraft", {}) if isinstance(obj, dict) else {}
            if isinstance(mc, dict):
                info.minecraft_version = str(mc.get("version") or "")
                for loader in mc.get("modLoaders", []) or []:
                    if not isinstance(loader, dict):
                        continue
                    lid = str(loader.get("id", ""))
                    for prefix in ("neoforge-", "forge-", "fabric-", "quilt-"):
                        if lid.startswith(prefix):
                            info.loader = prefix[:-1]
                            info.loader_version = lid[len(prefix):]
                            break
                    if info.loader:
                        break
            if obj.get("files"):
                info.warnings.append("CurseForge pack indexes remote project/file IDs; imported overrides only. Import a fully installed Prism instance to copy downloaded mods.")
            overrides = obj.get("overrides") or "overrides"
            return PrismImportPlan(source=path, game_root=tmp / str(overrides), info=info, cleanup_dir=tmp)

    return PrismImportPlan(source=path, game_root=tmp, info=info, cleanup_dir=tmp)


def detect_prism_source(source: str | Path) -> PrismImportPlan:
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        return _detect_archive(path)
    plan = _detect_instance_dir(path)
    if plan:
        return plan
    if (path.parent / "instance.cfg").exists() and path.name in {"minecraft", ".minecraft"}:
        plan = _detect_instance_dir(path.parent)
        if plan:
            plan.game_root = path
            return plan
    return PrismImportPlan(source=path, game_root=path, info=PrismPackInfo(source_type="directory", name=path.name))


def _copy_tree(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    count = 0
    source_root = src.resolve()
    for p in src.rglob("*"):
        if p.is_dir() or p.is_symlink():
            continue
        resolved = p.resolve()
        try:
            resolved.relative_to(source_root)
        except ValueError:
            continue
        rel = p.relative_to(src)
        if any(part in CLIENT_ONLY_NAMES for part in rel.parts):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, target)
        count += 1
    return count


def _write_default_server_properties(target: Path) -> None:
    props = target / "server.properties"
    if props.exists():
        return
    props.write_text(
        "# Minecraft server properties generated by Crucible Prism import\n"
        "server-port=25565\n"
        "online-mode=true\n"
        "enable-command-block=false\n"
        "motd=A Crucible-hosted Prism modpack server\n"
        "level-name=world\n"
        "view-distance=10\n"
        "simulation-distance=10\n",
        encoding="utf-8",
    )


def _write_eula(target: Path, accept_eula: bool) -> None:
    eula = target / "eula.txt"
    if eula.exists():
        return
    eula.write_text(
        "# Generated by Crucible Prism import.\n"
        "# Change to eula=true only if you agree to the Minecraft EULA.\n"
        f"eula={'true' if accept_eula else 'false'}\n",
        encoding="utf-8",
    )


def _detect_client_only_mods(mods_dir: Path) -> list[str]:
    """Best-effort server-hosting review list.

    Uses Prism's copied mods/.index Packwiz metadata first.  Prism stores
    side="client"/"server"/"both" there when it can resolve Modrinth or
    CurseForge metadata.  Falls back to filename heuristics for local/manual jars.
    """
    if not mods_dir.exists():
        return []

    flagged: set[str] = set()
    index_dir = mods_dir / ".index"
    if tomllib is not None and index_dir.exists():
        for meta in index_dir.glob("*.pw.toml"):
            try:
                data = tomllib.loads(meta.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            side = str(data.get("side", "")).lower()
            filename = str(data.get("filename", ""))
            if side == "client" and filename:
                flagged.add(filename)

    hints = re.compile(
        r"(sodium|iris|oculus|rubidium|embeddium|optifine|lambdynamiclights|dynamiclights|xaero|journeymap|appleskin|modmenu|notenoughanimations|betterf3|controlling|mouse|zoom|replaymod|shulkerboxtooltip|inventoryhud)",
        re.IGNORECASE,
    )
    for jar in mods_dir.glob("*.jar"):
        if hints.search(jar.name):
            flagged.add(jar.name)
    return sorted(flagged)

def _write_import_report(target: Path, plan: PrismImportPlan, copied: dict[str, int], warnings: list[str]) -> None:
    info = plan.info
    lines = [
        "# Crucible Prism Import",
        "",
        f"Source: {plan.source}",
        f"Detected type: {info.source_type}",
        f"Name: {info.name or target.name}",
        f"Minecraft: {info.minecraft_version or 'unknown'}",
        f"Loader: {(info.loader + (' ' + info.loader_version if info.loader_version else '')) if info.loader else 'unknown'}",
        f"Pack version: {info.pack_version or 'unknown'}",
        "",
        "## Copied",
    ]
    if copied:
        for name, count in copied.items():
            lines.append(f"- {name}: {count} file(s)")
    else:
        lines.append("- No server-safe folders/files were found to copy.")
    lines.extend([
        "",
        "## Not copied on purpose",
        "- resourcepacks/, shaderpacks/, texturepacks/",
        "- saves/, screenshots/, logs/, crash-reports/",
        "- options.txt and servers.dat",
        "",
        "## Warnings / next steps",
    ])
    if warnings:
        for w in warnings:
            lines.append(f"- {w}")
    else:
        lines.append("- No warnings.")
    lines.append("")
    lines.append("If start.sh says it cannot find a server jar, install the matching dedicated server loader/jar into this folder.")
    (target / "CRUCIBLE_IMPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def import_prism_source(
    source: str | Path,
    target: str | Path,
    *,
    accept_eula: bool = False,
    overwrite: bool = False,
    download_mods: bool = False,
    download_log_cb=None,
    download_progress_cb=None,
    install_server: bool = False,
    install_log_cb=None,
) -> PrismPackInfo:
    """Import a Prism instance / modpack into a server folder.

    When ``download_mods`` is True and the pack only shipped a download index
    (modrinth.index.json / manifest.json), Crucible will attempt to fetch the
    referenced mod jars after staging. This is best-effort and never fatal:
    download problems are recorded as warnings on the returned PrismPackInfo.
    """
    plan = detect_prism_source(source)
    target = Path(target).expanduser().resolve()
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise FileExistsError(f"Target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)

    copied: dict[str, int] = {}
    warnings = list(plan.info.warnings)
    game_root = plan.game_root
    if not game_root.exists():
        warnings.append(f"Expected game root does not exist: {game_root}")
    else:
        for dirname in SERVER_SAFE_DIRS:
            src = game_root / dirname
            if src.exists() and src.is_dir():
                copied[dirname + "/"] = _copy_tree(src, target / dirname)
        for filename in SERVER_SAFE_FILES:
            src = game_root / filename
            if src.exists() and src.is_file():
                shutil.copy2(src, target / filename)
                copied[filename] = 1

    meta_dir = target / ".crucible" / "source-pack"
    meta_dir.mkdir(parents=True, exist_ok=True)
    for metadata_name in ("instance.cfg", "mmc-pack.json", "modrinth.index.json", "manifest.json"):
        for base in (plan.source if plan.source.is_dir() else plan.game_root, plan.game_root, plan.game_root.parent):
            src = base / metadata_name
            if src.exists() and src.is_file():
                shutil.copy2(src, meta_dir / metadata_name)
                break

    (target / "start.sh").write_text(_START_SH, encoding="utf-8")
    os.chmod(target / "start.sh", 0o755)
    _write_default_server_properties(target)
    _write_eula(target, accept_eula)

    if not (target / "mods").exists() or not list((target / "mods").glob("*.jar")):
        warnings.append("No mod jars were copied. Use an installed Prism instance, not only an index-only exported pack, when possible.")
    clientish = _detect_client_only_mods(target / "mods")
    if clientish:
        warnings.append("Possible client-side mods copied; review before hosting: " + ", ".join(clientish[:20]) + ("..." if len(clientish) > 20 else ""))
    server_jar_patterns = ("server.jar", "minecraft_server*.jar", "fabric-server-launch.jar", "quilt-server-launch.jar", "forge-*.jar", "neoforge-*.jar", "paper*.jar")
    has_server_launcher = any(match for pattern in server_jar_patterns for match in target.glob(pattern))
    if not has_server_launcher and not (target / "libraries").exists():
        warnings.append("No obvious dedicated server jar/loader was found; start.sh will explain how to finish the server install.")

    # Optional best-effort mod download from a staged pack index.
    download_summary: dict | None = None
    if download_mods:
        try:
            from .downloader import download_pack_mods, has_downloadable_index
            if has_downloadable_index(target):
                dl = download_pack_mods(
                    target,
                    log_cb=download_log_cb,
                    progress_cb=download_progress_cb,
                )
                download_summary = {
                    "downloaded": dl.downloaded,
                    "already_present": dl.already_present,
                    "skipped_client": dl.skipped_client,
                    "failed": dl.failed,
                    "cancelled": dl.cancelled,
                }
                warnings.append("Mod download attempt: " + dl.summary())
                for name, reason in dl.failed[:20]:
                    warnings.append(f"  download failed: {name} ({reason})")
            else:
                warnings.append("download_mods requested but no pack index was found to download from.")
        except Exception as exc:  # never let downloading break an import
            warnings.append(f"Mod download step errored (import still succeeded): {exc}")

    # Optional best-effort install of the dedicated server program when the
    # imported pack didn't ship one (Prism ships clients, not servers).
    server_install_summary: dict | None = None
    if install_server and not has_server_launcher and not (target / "libraries").exists():
        try:
            from . import serverloader as _sl
            log = install_log_cb or download_log_cb
            si = _sl.install_server_loader(
                target,
                minecraft_version=plan.info.minecraft_version,
                loader=plan.info.loader,
                loader_version=plan.info.loader_version,
                log_cb=log,
            )
            server_install_summary = {
                "ok": si.ok, "loader": si.loader, "launcher": si.launcher,
                "failed_reason": si.failed_reason,
            }
            warnings.append("Server install attempt: " + si.summary())
        except Exception as exc:  # never let install break an import
            warnings.append(f"Server install step errored (import still succeeded): {exc}")

    plan.info.warnings = warnings
    summary = {
        "source": str(plan.source),
        "game_root": str(plan.game_root),
        "detected": plan.info.__dict__,
        "copied": copied,
        "download": download_summary,
        "server_install": server_install_summary,
    }
    (target / ".crucible" / "import-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _write_import_report(target, plan, copied, warnings)

    if plan.cleanup_dir:
        shutil.rmtree(plan.cleanup_dir, ignore_errors=True)
    return plan.info


def scan_prism_instances(root: str | Path, max_depth: int = 4) -> list[Path]:
    root = Path(root).expanduser().resolve()
    if not root.exists():
        return []
    found: list[Path] = []
    for cfg in root.rglob("instance.cfg"):
        try:
            depth = len(cfg.relative_to(root).parts) - 1
        except ValueError:
            continue
        if depth <= max_depth:
            found.append(cfg.parent)
    return sorted(set(found))
