"""
crucible/diagnostics/loadcheck.py

Detect and auto-fix the most common reasons a modded server refuses to start —
especially the classic "client-only mod on a dedicated server" crash:

    java.lang.RuntimeException: Attempted to load class
        net/minecraft/client/gui/screens/Screen for invalid dist DEDICATED_SERVER

NeoForge/Forge name the offending mod right above that line, e.g.::

    - Status Effect Bars (statuseffectbars) has failed to load correctly

The authoritative signal is the crash log itself, so Crucible parses the
newest crash report (or logs/latest.log), figures out which jar provides the
offending mod id, and quarantines it by renaming it to ``<file>.jar.disabled``
(the same convention the Mods tab uses, so disabled mods stay visible and can
be re-enabled). Nothing here ever raises on bad input; callers get structured
results instead.

This module is pure-Python and has no Qt or network dependencies, so it is
driven by both the CLI (`crucible fix-loading`) and the GUI.
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Crash-log signatures
# --------------------------------------------------------------------------- #

# "for invalid dist DEDICATED_SERVER"  (client class loaded on a server)
_DIST_RE = re.compile(r"invalid dist DEDICATED_SERVER", re.IGNORECASE)
_CLIENT_CLASS_RE = re.compile(
    r"load class\s+([\w/.$]+)\s+for invalid dist DEDICATED_SERVER", re.IGNORECASE)

# "<Name> (<id>) has failed to load correctly" — matches every wording FML uses:
#   "- Status Effect Bars (statuseffectbars) has failed to load correctly"      (console)
#   "Failure message: Status Effect Bars (statuseffectbars) has failed to load"  (crash report)
_FAILED_MOD_RE = re.compile(
    r"(?P<name>[^\n(]+?)\s*\((?P<id>[A-Za-z0-9_][A-Za-z0-9_\-]*)\)\s*has failed to load",
    re.IGNORECASE,
)

# "Failed to create mod instance. ModID: statuseffectbars, class com.…"
_FAILED_INSTANCE_RE = re.compile(
    r"Failed to create mod instance\.\s*ModID:\s*(?P<id>[A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

# Stack frames like "TRANSFORMER/statuseffectbars@1.0.2/com.foo.Bar".
_TRANSFORMER_MOD_RE = re.compile(r"TRANSFORMER/(?P<id>[A-Za-z0-9_\-]+)@")

# The crash-report failure block names the offending jar like::
#     Mod file: /home/…/mods/statuseffectbars-1.21.1-NeoForge-1.0.2.jar
#     Failure message: Status Effect Bars (statuseffectbars) has failed to load
# Require the "Failure message: … has failed to load" line right after so we never
# pick up unrelated "Using Mod File: …" dependency-selection warnings.
_MOD_FILE_RE = re.compile(
    r"(?:^|\n)[ \t]*Mod [Ff]ile:[ \t]*(?P<path>\S+\.jar)[ \t]*\n"
    r"[ \t]*Failure message:[^\n]*has failed to load",
    re.IGNORECASE,
)

# Loader/engine ids that show up in stack frames but are never the culprit mod.
_NON_MOD_IDS = {"minecraft", "neoforge", "forge", "fml", "fml_loader",
                "mcp", "modlauncher", "bootstraplauncher"}
# Filename PREFIXES that mark a jar as the loader/engine itself, not a mod.
# (Matched with startswith — a mod named "foo-1.21.1-NeoForge-1.0.2.jar" is a
#  real mod and must NOT be excluded just because "neoforge" is in its version.)
_NON_MOD_JAR_PREFIXES = ("neoforge-", "forge-", "fmlloader-", "fmlcore-",
                         "loader-", "server-", "minecraft-", "client-")
# Substrings that unambiguously mark loader/runtime libraries (not mod names).
_NON_MOD_JAR_SUBSTR = ("modlauncher", "bootstraplauncher", "securejarhandler",
                       "javafmllanguage", "lowcodelanguage", "mclanguage")


def _is_loader_jar(filename: str) -> bool:
    low = filename.lower()
    return low.startswith(_NON_MOD_JAR_PREFIXES) or any(
        s in low for s in _NON_MOD_JAR_SUBSTR)

# Missing-dependency style errors (NeoForge/Forge wording varies by version)
_MISSING_DEP_RE = re.compile(
    r"Mod (?:ID )?'?(?P<id>[A-Za-z0-9_\-]+)'?.*requires.*?'?(?P<dep>[A-Za-z0-9_\-@.]+)'?",
    re.IGNORECASE,
)
_MISSING_DEP_HDR = re.compile(
    r"(missing|unsupported).*(mandatory )?dependenc", re.IGNORECASE)

# Duplicate mods
_DUP_RE = re.compile(r"(duplicate mods?|found a duplicate mod)", re.IGNORECASE)

_QUARANTINE_LOG = ".crucible/quarantine.json"


@dataclass
class LoadIssue:
    kind: str                       # client_on_server | missing_dependency | duplicate | unknown
    detail: str
    modids: list[str] = field(default_factory=list)


@dataclass
class Diagnosis:
    issues: list[LoadIssue] = field(default_factory=list)
    source: str = ""                # where the log came from
    found_crash: bool = False
    # Jar filenames named directly by the crash report ("Mod file: ….jar").
    offender_jar_names: list[str] = field(default_factory=list)

    @property
    def client_on_server_modids(self) -> list[str]:
        out: list[str] = []
        for iss in self.issues:
            if iss.kind == "client_on_server":
                out.extend(iss.modids)
        # de-dupe, preserve order
        seen, uniq = set(), []
        for m in out:
            if m not in seen:
                seen.add(m)
                uniq.append(m)
        return uniq

    @property
    def is_clean(self) -> bool:
        return not self.issues

    def human_summary(self) -> str:
        if not self.found_crash:
            return "No crash log found \u2014 nothing to diagnose."
        if self.is_clean:
            return "A crash log was found, but no known loading problem was recognised."
        lines = []
        for iss in self.issues:
            lines.append("\u2022 " + iss.detail)
        return "\n".join(lines)


@dataclass
class FixResult:
    diagnosis: Diagnosis
    quarantined: list[str] = field(default_factory=list)   # filenames disabled
    unresolved: list[str] = field(default_factory=list)    # modids with no matching jar
    applied: bool = False
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.diagnosis.found_crash:
            return "No crash log to analyse."
        if self.diagnosis.is_clean:
            return "No known loading problem detected."
        bits = []
        if self.quarantined:
            verb = "Disabled" if self.applied else "Would disable"
            bits.append(f"{verb} {len(self.quarantined)} client-only mod(s): "
                        + ", ".join(self.quarantined))
        if self.unresolved:
            bits.append("Could not find jars for: " + ", ".join(self.unresolved))
        return "; ".join(bits) if bits else self.diagnosis.human_summary()


# --------------------------------------------------------------------------- #
# Log discovery + parsing
# --------------------------------------------------------------------------- #

def latest_crash_text(server_path: str | Path) -> tuple[str | None, str | None]:
    """Return (text, source_label) for the newest relevant log.

    Prefers the newest crash-reports/*.txt, then logs/latest.log, then
    logs/debug.log. Returns (None, None) when nothing is found.
    """
    root = Path(server_path).expanduser()
    candidates: list[Path] = []
    cr = root / "crash-reports"
    if cr.is_dir():
        candidates.extend(sorted(cr.glob("crash-*.txt"),
                                 key=lambda p: p.stat().st_mtime, reverse=True))
    for name in ("logs/latest.log", "logs/debug.log"):
        p = root / name
        if p.is_file():
            candidates.append(p)
    for p in candidates:
        try:
            return p.read_text(encoding="utf-8", errors="replace"), str(p)
        except OSError:
            continue
    return None, None


def parse_crash_log(text: str) -> Diagnosis:
    """Parse a crash/log string into a structured Diagnosis. Never raises."""
    diag = Diagnosis(found_crash=bool(text and text.strip()))
    if not text:
        return diag

    # 1. Client-only-on-server (the dist crash).
    if _DIST_RE.search(text):
        # Gather mod ids from every wording FML/NeoForge uses.
        ids: list[str] = []
        for rx in (_FAILED_MOD_RE, _FAILED_INSTANCE_RE):
            for m in rx.finditer(text):
                ids.append(m.group("id").lower())
        for m in _TRANSFORMER_MOD_RE.finditer(text):
            mid = m.group("id").lower()
            if mid not in _NON_MOD_IDS:
                ids.append(mid)
        # de-dupe preserve order
        seen, uniq = set(), []
        for m in ids:
            if m and m not in seen:
                seen.add(m)
                uniq.append(m)

        # Jar paths named directly in the crash report ("Mod file: ….jar").
        for m in _MOD_FILE_RE.finditer(text):
            name = Path(m.group("path")).name
            if _is_loader_jar(name):
                continue
            if name not in diag.offender_jar_names:
                diag.offender_jar_names.append(name)

        cls_m = _CLIENT_CLASS_RE.search(text)
        cls = cls_m.group(1).replace("/", ".") if cls_m else "a client-only class"
        named = uniq or [Path(n).stem for n in diag.offender_jar_names]
        if named:
            names = ", ".join(named)
            detail = (f"Client-only mod(s) on a dedicated server: {names}. "
                      f"They try to load {cls}, which does not exist on servers "
                      f"(invalid dist DEDICATED_SERVER). These mods only run on the "
                      f"client and must be disabled on the server.")
        else:
            detail = (f"A mod tried to load {cls} on a dedicated server "
                      f"(invalid dist DEDICATED_SERVER). The crash log did not name "
                      f"the mod, so Crucible will scan installed mods for client-only "
                      f"jars and disable them automatically.")
        diag.issues.append(LoadIssue("client_on_server", detail, uniq))

    # 2. Missing/unsupported mandatory dependencies.
    if _MISSING_DEP_HDR.search(text) or _MISSING_DEP_RE.search(text):
        deps = []
        for m in _MISSING_DEP_RE.finditer(text):
            deps.append(f"{m.group('id')} -> {m.group('dep')}")
        detail = ("Missing or unsupported mandatory dependencies. "
                  + ("; ".join(deps[:8]) if deps else
                     "Add the required dependency mods (matching this Minecraft "
                     "version + loader)."))
        # Only record if we didn't already explain everything via the dist crash.
        if not any(i.kind == "client_on_server" for i in diag.issues) or deps:
            diag.issues.append(LoadIssue("missing_dependency", detail))

    # 3. Duplicate mods.
    if _DUP_RE.search(text):
        diag.issues.append(LoadIssue(
            "duplicate",
            "Duplicate mods detected \u2014 the same mod is present twice in mods/. "
            "Remove the older copy."))

    return diag


def diagnose_server(server_path: str | Path, *, log_text: str | None = None) -> Diagnosis:
    """Diagnose a server from a provided log string or its newest crash report."""
    if log_text is not None:
        diag = parse_crash_log(log_text)
        diag.source = "provided log"
        return diag
    text, src = latest_crash_text(server_path)
    diag = parse_crash_log(text or "")
    diag.source = src or ""
    return diag


# --------------------------------------------------------------------------- #
# Jar <-> mod id mapping
# --------------------------------------------------------------------------- #

_TOML_MODID_RE = re.compile(r'^\s*modId\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def jar_modids(jar_path: str | Path) -> set[str]:
    """Best-effort: read a jar's declared mod id(s) (Fabric + NeoForge/Forge)."""
    ids: set[str] = set()
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            names = set(zf.namelist())
            if "fabric.mod.json" in names:
                try:
                    data = json.loads(
                        zf.read("fabric.mod.json").decode("utf-8", "replace"))
                    if isinstance(data, dict) and data.get("id"):
                        ids.add(str(data["id"]).lower())
                except Exception:
                    pass
            for toml_name in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if toml_name in names:
                    try:
                        raw = zf.read(toml_name).decode("utf-8", "replace")
                        for m in _TOML_MODID_RE.finditer(raw):
                            ids.add(m.group(1).lower())
                    except Exception:
                        pass
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return ids


def jar_is_client_only(jar_path: str | Path) -> bool:
    """True if a jar statically declares it is client-only (Fabric env)."""
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            if "fabric.mod.json" in zf.namelist():
                data = json.loads(
                    zf.read("fabric.mod.json").decode("utf-8", "replace"))
                if isinstance(data, dict):
                    return str(data.get("environment", "")).lower() == "client"
    except Exception:
        pass
    return False


def _enabled_jars(mods_dir: Path) -> list[Path]:
    if not mods_dir.is_dir():
        return []
    return [p for p in mods_dir.glob("*.jar") if not p.name.endswith(".disabled")]


def _resolve_jar_by_name(mods_dir: Path, filename: str) -> Path | None:
    """Find an enabled jar in mods/ matching a filename from a crash report."""
    if not mods_dir.is_dir() or not filename:
        return None
    exact = mods_dir / filename
    if exact.exists():
        return exact
    low = filename.lower()
    for jar in _enabled_jars(mods_dir):
        if jar.name.lower() == low:
            return jar
    return None


def map_modids_to_jars(server_path: str | Path,
                       modids: list[str]) -> tuple[dict[str, Path], list[str]]:
    """Map mod ids to their jar files. Returns (found, unresolved_modids).

    Matches first on declared mod metadata, then falls back to a filename
    containing the mod id (handles jars we can't parse).
    """
    mods_dir = Path(server_path).expanduser() / "mods"
    wanted = {m.lower() for m in modids}
    found: dict[str, Path] = {}
    jars = _enabled_jars(mods_dir)

    # Pass 1: declared mod ids inside each jar.
    for jar in jars:
        for mid in jar_modids(jar):
            if mid in wanted and mid not in found:
                found[mid] = jar

    # Pass 2: filename heuristic for anything still missing.
    for mid in wanted - set(found):
        squashed = mid.replace("_", "").replace("-", "")
        for jar in jars:
            stem = jar.stem.lower().replace("_", "").replace("-", "")
            if squashed and squashed in stem:
                found[mid] = jar
                break

    unresolved = sorted(wanted - set(found))
    return found, unresolved


# --------------------------------------------------------------------------- #
# Quarantine / restore
# --------------------------------------------------------------------------- #

def _record_quarantine(server_path: Path, entries: list[dict]) -> None:
    log = server_path / _QUARANTINE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if log.is_file():
        try:
            existing = json.loads(log.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
    existing.extend(entries)
    try:
        log.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except OSError:
        pass


def quarantine_jars(server_path: str | Path, jar_paths: list[Path], *,
                    reason: str = "client-only mod on dedicated server") -> list[str]:
    """Disable jars by renaming to ``<name>.jar.disabled``. Returns disabled names."""
    root = Path(server_path).expanduser()
    disabled: list[str] = []
    entries: list[dict] = []
    for jar in jar_paths:
        if not jar.exists() or jar.name.endswith(".disabled"):
            continue
        target = jar.with_name(jar.name + ".disabled")
        try:
            jar.rename(target)
        except OSError:
            continue
        disabled.append(jar.name)
        entries.append({"filename": jar.name, "reason": reason,
                        "disabled_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if entries:
        _record_quarantine(root, entries)
    return disabled


def restore_quarantined(server_path: str | Path) -> list[str]:
    """Re-enable every ``*.jar.disabled`` in mods/. Returns restored names."""
    mods_dir = Path(server_path).expanduser() / "mods"
    restored: list[str] = []
    if not mods_dir.is_dir():
        return restored
    for p in mods_dir.glob("*.jar.disabled"):
        target = p.with_name(p.name[: -len(".disabled")])
        if target.exists():
            continue
        try:
            p.rename(target)
            restored.append(target.name)
        except OSError:
            continue
    return restored


def scan_client_only(server_path: str | Path) -> list[tuple[str, str]]:
    """Static, best-effort scan for likely client-only mods in mods/.

    Returns (filename, reason). Only flags jars that *declare* themselves
    client-only (Fabric environment=client); this is conservative and will not
    catch every case, which is why the crash-driven path is preferred.
    """
    mods_dir = Path(server_path).expanduser() / "mods"
    out: list[tuple[str, str]] = []
    for jar in _enabled_jars(mods_dir):
        if jar_is_client_only(jar):
            out.append((jar.name, "declares environment=client (Fabric)"))
    return out


# --------------------------------------------------------------------------- #
# High-level fix
# --------------------------------------------------------------------------- #

def autofix_loading(server_path: str | Path, *, apply: bool = False,
                    log_text: str | None = None) -> FixResult:
    """Diagnose a server's loading crash and optionally quarantine the culprits.

    Only the authoritative *client-on-server* crash triggers automatic
    quarantine; other issues are reported for the user to act on. Never raises.
    """
    root = Path(server_path).expanduser()
    diag = diagnose_server(root, log_text=log_text)
    result = FixResult(diagnosis=diag, applied=apply)

    if not any(i.kind == "client_on_server" for i in diag.issues):
        return result

    mods_dir = root / "mods"
    jars_to_disable: dict[str, Path] = {}   # filename -> path

    # (a) Jars named directly in the crash report ("Mod file:") — most reliable.
    for name in diag.offender_jar_names:
        p = _resolve_jar_by_name(mods_dir, name)
        if p is not None:
            jars_to_disable[p.name] = p

    # (b) Jars resolved from named mod ids.
    modids = diag.client_on_server_modids
    unresolved: list[str] = []
    if modids:
        found, unresolved = map_modids_to_jars(root, modids)
        for p in found.values():
            jars_to_disable[p.name] = p
        # A modid we already covered via its jar isn't really unresolved.
        disabled_stems = {n.lower() for n in jars_to_disable}
        unresolved = [m for m in unresolved
                      if not any(m.replace("_", "").replace("-", "")
                                 in s.replace("_", "").replace("-", "")
                                 for s in disabled_stems)]

    # (c) Fallback: the crash named nothing we could locate. Rather than giving
    #     up (the old behaviour), automatically scan mods/ for client-only jars
    #     and quarantine those. This is what "fix automatically" should do.
    if not jars_to_disable:
        for fname, _reason in scan_client_only(root):
            p = mods_dir / fname
            if p.exists():
                jars_to_disable[p.name] = p
        if jars_to_disable:
            result.messages.append(
                "Crash did not name the mod; disabled client-only jar(s) found "
                "by scanning mods/.")
            unresolved = []
        else:
            result.messages.append(
                "Crash did not name the mod and no jar statically declares itself "
                "client-only. Check the most recently added client-side mod.")

    result.unresolved = unresolved
    if jars_to_disable:
        if apply:
            result.quarantined = quarantine_jars(
                root, list(jars_to_disable.values()),
                reason="client-only mod on dedicated server")
        else:
            result.quarantined = sorted(jars_to_disable)
    return result
