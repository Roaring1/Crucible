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

# "- Status Effect Bars (statuseffectbars) has failed to load correctly"
_FAILED_MOD_RE = re.compile(
    r"-\s*(?P<name>.+?)\s*\((?P<id>[A-Za-z0-9_][A-Za-z0-9_\-]*)\)\s*has failed to load",
    re.IGNORECASE,
)

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
        modids = [m.group("id").lower() for m in _FAILED_MOD_RE.finditer(text)]
        # de-dupe preserve order
        seen, uniq = set(), []
        for m in modids:
            if m not in seen:
                seen.add(m)
                uniq.append(m)
        cls_m = _CLIENT_CLASS_RE.search(text)
        cls = cls_m.group(1).replace("/", ".") if cls_m else "a client-only class"
        if uniq:
            names = ", ".join(uniq)
            detail = (f"Client-only mod(s) on a dedicated server: {names}. "
                      f"They try to load {cls}, which does not exist on servers "
                      f"(invalid dist DEDICATED_SERVER). These mods must be removed "
                      f"from the server (they only run on the client).")
        else:
            detail = (f"A mod tried to load {cls} on a dedicated server "
                      f"(invalid dist DEDICATED_SERVER), but the crash log did not "
                      f"name which mod. Check recently added client-side mods.")
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

    modids = diag.client_on_server_modids
    if modids:
        found, unresolved = map_modids_to_jars(root, modids)
        result.unresolved = unresolved
        if found:
            if apply:
                result.quarantined = quarantine_jars(
                    root, list(found.values()),
                    reason="client-only mod on dedicated server")
            else:
                result.quarantined = sorted({p.name for p in found.values()})
    return result
