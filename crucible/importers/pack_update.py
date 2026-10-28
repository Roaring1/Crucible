"""
crucible/importers/pack_update.py

Modpack UPDATING (as opposed to importing).

The hard part of updating a modpack server is not downloading the new pack --
it is not destroying the world, the config the owner hand-tuned, or the mods
the owner added themselves. So this module never updates "in place". It
always does a stage-and-swap:

    1. Identify which pack/version this instance is actually running.
    2. Ask a provider what newer versions exist.
    3. Build an UpdatePlan and show it to the human BEFORE anything is written.
    4. Take a mandatory, verified world backup.
    5. Download the new server pack to .crucible/downloads/ (atomic).
    6. Extract it into a sibling staging directory.
    7. Copy the preserve-list (world, configs, user mods, ...) into staging.
    8. Rename live -> "<name>.pre-update-<ts>", rename staging -> live.
    9. On ANY failure in 5-8, roll back so the server is never half-updated.
   10. Write a provenance receipt so the NEXT update knows exactly what the
       pack shipped versus what the owner added.

Design rules (same as downloader.py / serverloader.py):
  * Public API never raises for expected failures -- it returns a structured
    result with .blockers / .failed_reason.
  * HTTPS only. Streamed downloads with a size cap, temp file + atomic rename.
  * Cancellable via a threading.Event.
  * No PyQt import, so all of this is unit-testable headless.

GTNH specifics that drove this design (verified against the live index):
  * The authoritative version index is
    https://downloads.gtnewhorizons.com/versions.json -- a dict keyed by
    version string ('2.8.4', '2.9.0-beta-3', 'April fools 2025', ...) whose
    values carry {'server': {'java8Url', 'java17_2XUrl'}, 'client', 'mmc',
    'releaseDate', 'maxJavaVersion', 'title', 'description'}.
  * Server-pack URLs MUST be read from that index, never constructed:
    2.8.4 ships '..._Server_Java_17-25.zip' but 2.9.0-beta-3 ships
    '.../betas/..._Server_Java_17-26.zip'. The Java suffix and the /betas/
    path segment both change between releases.
  * There is no 'Latest-Stable-Server' alias (the Client/Prism ones exist,
    the server one 404s), so 'latest' has to be computed by sorting.
  * GTNH advises updating one major version at a time, and a server whose
    mod list changed will ask for '/fml confirm' on first boot.
"""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import zipfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from crucible import __version__

LogCb = Callable[[str], None]
ProgressCb = Callable[[int, int], None]  # (bytes_done, bytes_total)

_USER_AGENT = f"Crucible/{__version__} (+https://github.com/Roaring1/Crucible; Minecraft server manager)"
_TIMEOUT = 30
_MAX_RETRIES = 3
_MAX_INDEX_BYTES = 16 * 1024 * 1024
_MAX_PACK_BYTES = 8 * 1024 * 1024 * 1024
_CHUNK = 256 * 1024

GTNH_VERSIONS_URL = "https://downloads.gtnewhorizons.com/versions.json"

# Files/dirs that belong to the SERVER OWNER, not to the pack. These are
# carried across an update untouched. Anything not listed here is replaced by
# the new pack's copy.
#
# Directories are matched by exact top-level name.
PRESERVE_DIRS = (
    "backups",            # Crucible + pack-side backups
    "journeymap",         # per-world map data
    "visualprospecting",  # GTNH ore-vein discoveries -- expensive to lose
    "serverutilities",    # claims, homes, permissions
    "dynmap",
    "blueprints",         # GTNH building gadgets blueprints
    "logs",
    "crash-reports",
    "playerdata",
    ".crucible",          # Crucible's own provenance/state
)

PRESERVE_FILES = (
    "server.properties",
    "ops.json",
    "whitelist.json",
    "banned-ips.json",
    "banned-players.json",
    "eula.txt",
    "server-icon.png",
    "usercache.json",
    "usernamecache.json",
)

# The pack ships these and they MUST come from the new version, even though
# they look like config. Listed for documentation/readability.
PACK_OWNED_HINTS = ("mods", "config", "libraries", "scripts", "coretweaks")


# --------------------------------------------------------------------------
# Version parsing / ordering
# --------------------------------------------------------------------------

_CHANNEL_RANK = {"alpha": 0, "beta": 1, "rc": 2, "stable": 3}

_VER_RE = re.compile(
    r"^v?(?P<maj>\d+)\.(?P<min>\d+)(?:\.(?P<pat>\d+))?"
    r"(?:[-_. ]*(?P<kind>alpha|beta|rc)[-_. ]*(?P<num>\d*))?$",
    re.IGNORECASE,
)


def parse_pack_version(raw: str) -> tuple | None:
    """Return a sortable tuple for a GTNH-style version, or None if unparseable.

    Handles '2.8.4', '2.9.0-beta-3', '2.7.0-RC-2', '2.5.0-RC1', '2.8.0-rc-1'.
    Returns None for non-versions like 'April fools 2025' so callers can
    exclude them from "is there a newer one?" maths instead of mis-ordering
    them.

    Pre-releases sort BELOW the matching stable release:
        2.9.0-beta-3 < 2.9.0 and 2.8.4 < 2.9.0-beta-1
    """
    m = _VER_RE.match((raw or "").strip())
    if not m:
        return None
    kind = (m.group("kind") or "").lower()
    rank = _CHANNEL_RANK.get(kind, _CHANNEL_RANK["stable"])
    num = m.group("num")
    return (
        int(m.group("maj")),
        int(m.group("min")),
        int(m.group("pat") or 0),
        rank,
        int(num) if num else 0,
    )


def channel_of(raw: str) -> str:
    """'stable' | 'beta' | 'rc' | 'alpha' | 'unknown' for a version string."""
    m = _VER_RE.match((raw or "").strip())
    if not m:
        return "unknown"
    return (m.group("kind") or "stable").lower()


# --------------------------------------------------------------------------
# Identity: what pack IS this instance?
# --------------------------------------------------------------------------

# 'GT_New_Horizons_2.8.4_Server_Java_17-25' and friends.
_GTNH_SERVER_RE = re.compile(
    r"GT[ _]?New[ _]?Horizons[ _]+(?P<version>.+?)[ _]+Server[ _]+Java[ _]+(?P<java>8|17-\d+)",
    re.IGNORECASE,
)
# Looser: any GTNH-looking name with a version in it.
_GTNH_LOOSE_RE = re.compile(
    r"GT[ _]?New[ _]?Horizons[ _]+(?P<version>\d+\.\d+(?:\.\d+)?(?:[-_. ]*(?:alpha|beta|rc)[-_. ]*\d*)?)",
    re.IGNORECASE,
)
# 'changelog from 2.8.3 to 2.8.4.md' -- shipped inside GTNH server packs.
_CHANGELOG_RE = re.compile(
    r"changelog\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+?)(?:\.md)?$",
    re.IGNORECASE,
)

VARIANT_JAVA8 = "java8"
VARIANT_JAVA17 = "java17_2X"


@dataclass
class PackIdentity:
    """What pack + version an instance is running, and how sure we are."""

    provider: str = ""          # "gtnh" | "" (unknown)
    version: str = ""           # e.g. "2.8.4"
    variant: str = ""           # VARIANT_JAVA8 | VARIANT_JAVA17
    confidence: str = "none"    # "receipt" | "high" | "guess" | "none"
    evidence: list = field(default_factory=list)

    @property
    def known(self) -> bool:
        return bool(self.provider and self.version)

    def label(self) -> str:
        if not self.known:
            return "unknown pack"
        variant = "Java 8" if self.variant == VARIANT_JAVA8 else "Java 17+"
        return f"GT New Horizons {self.version} ({variant})"


def _normalize_variant(java_token: str) -> str:
    return VARIANT_JAVA8 if java_token.strip() == "8" else VARIANT_JAVA17


def identify_gtnh_name(text: str) -> tuple | None:
    """Parse a GTNH server-pack name into (version, variant), or None.

    GTNH server packs are distributed as
    'GT_New_Horizons_<version>_Server_Java_<8|17-NN>.zip'. That filename is
    the ONLY provenance an import gets -- the pack ships no machine-readable
    manifest -- so this is also what the importer uses to stop writing an
    instance with an empty version/loader.
    """
    m = _GTNH_SERVER_RE.search(text or "")
    if m and parse_pack_version(m.group("version")):
        return m.group("version").strip(), _normalize_variant(m.group("java"))
    return None


def read_receipt(server_path: str | Path) -> dict:
    """Read .crucible/pack-provenance.json, or {} if absent/corrupt."""
    p = Path(server_path) / ".crucible" / "pack-provenance.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def identify_pack(instance) -> PackIdentity:
    """Work out which pack/version an instance is running.

    Evidence is consulted best-first and every source that agreed (or was
    tried) is recorded on .evidence so the UI can explain itself instead of
    silently guessing. `instance` only needs .path, .version and
    .prism_source, so a plain ServerInstance works.
    """
    root = Path(getattr(instance, "path", "") or ".")
    ident = PackIdentity()

    # 1. A receipt we wrote ourselves is authoritative.
    receipt = read_receipt(root)
    if receipt.get("provider") and receipt.get("version"):
        return PackIdentity(
            provider=str(receipt["provider"]),
            version=str(receipt["version"]),
            variant=str(receipt.get("variant") or VARIANT_JAVA17),
            confidence="receipt",
            evidence=[".crucible/pack-provenance.json"],
        )

    # 2. Strings that are likely to contain the original pack filename.
    candidates: list[tuple[str, str]] = []
    summary = root / ".crucible" / "import-summary.json"
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
        src = str((data or {}).get("source") or "")
        if src:
            candidates.append(("import-summary.json source", Path(src).name))
        detected = (data or {}).get("detected") or {}
        if isinstance(detected, dict) and detected.get("name"):
            candidates.append(("import-summary.json detected name", str(detected["name"])))
    except (OSError, ValueError):
        pass

    prism_source = str(getattr(instance, "prism_source", "") or "")
    if prism_source:
        candidates.append(("registry prism_source", Path(prism_source).name))
    candidates.append(("folder name", root.name))

    for where, text in candidates:
        hit = identify_gtnh_name(text)
        if hit:
            version, variant = hit
            return PackIdentity(
                provider="gtnh", version=version, variant=variant,
                confidence="high", evidence=[f"{where}: {text}"],
            )

    # 3. A changelog file shipped in the pack names the version it upgraded TO.
    try:
        for child in root.iterdir():
            if not child.is_file():
                continue
            m = _CHANGELOG_RE.match(child.stem if child.suffix == ".md" else child.name)
            if m and parse_pack_version(m.group("new")):
                ident = PackIdentity(
                    provider="gtnh",
                    version=m.group("new").strip(),
                    variant=_guess_variant_from_disk(root),
                    confidence="guess",
                    evidence=[f"shipped changelog file: {child.name}"],
                )
                break
    except OSError:
        pass
    if ident.known:
        return ident

    # 4. Loose match, plus the registry's own version field.
    for where, text in candidates:
        m = _GTNH_LOOSE_RE.search(text)
        if m:
            return PackIdentity(
                provider="gtnh", version=m.group("version").strip(),
                variant=_guess_variant_from_disk(root),
                confidence="guess", evidence=[f"{where}: {text}"],
            )

    reg_version = str(getattr(instance, "version", "") or "").strip()
    if reg_version and parse_pack_version(reg_version) and _looks_like_gtnh(root):
        return PackIdentity(
            provider="gtnh", version=reg_version,
            variant=_guess_variant_from_disk(root),
            confidence="guess",
            evidence=["registry version field + GTNH files on disk"],
        )

    ident.evidence = [f"tried: {w}" for w, _ in candidates]
    return ident


def _looks_like_gtnh(root: Path) -> bool:
    """Cheap structural check for a GTNH 1.7.10 server on disk."""
    markers = (
        "lwjgl3ify-forgePatches.jar",
        "minecraft_server.1.7.10.jar",
        "GregTech.lang",
        "startserver-java9.sh",
    )
    try:
        return any((root / m).exists() for m in markers)
    except OSError:
        return False


def _guess_variant_from_disk(root: Path) -> str:
    """Java 17+ GTNH server packs ship java9args.txt + lwjgl3ify patches."""
    try:
        if (root / "java9args.txt").exists() or (root / "lwjgl3ify-forgePatches.jar").exists():
            return VARIANT_JAVA17
    except OSError:
        pass
    return VARIANT_JAVA8


# --------------------------------------------------------------------------
# Provider: what versions exist?
# --------------------------------------------------------------------------


class PackUpdateError(Exception):
    """Raised only by fetch_releases(); everything else returns a result."""


@dataclass
class PackRelease:
    version: str
    title: str = ""
    channel: str = "unknown"
    release_date: str = ""
    max_java_version: int = 0
    server_urls: dict = field(default_factory=dict)  # variant -> url
    changelog_url: str = ""

    def server_url(self, variant: str) -> str:
        return self.server_urls.get(variant, "")

    def sort_key(self):
        return parse_pack_version(self.version) or (-1, -1, -1, -1, -1)

    def label(self) -> str:
        bits = [self.version]
        if self.title:
            bits.append(self.title)
        if self.release_date:
            bits.append(self.release_date)
        return " \u00b7 ".join(bits)


_HREF_RE = re.compile(r'href="(?P<url>[^"]+)"', re.IGNORECASE)


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _http_bytes(url: str, *, limit: int, timeout: int = _TIMEOUT) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PackUpdateError("Refusing a non-HTTPS URL.")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    last = ""
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
                data = r.read(limit + 1)
            if len(data) > limit:
                raise PackUpdateError("Response was unexpectedly large.")
            return data
        except PackUpdateError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(getattr(exc, "reason", exc) or exc)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 6))
    raise PackUpdateError(f"Could not reach {parsed.hostname} (no internet?). Details: {last}")


def parse_gtnh_index(raw: bytes | str) -> list:
    """Parse a GTNH versions.json payload into PackRelease objects.

    Split out from fetch_releases() so it can be tested without network.
    Entries with no 'server' block (client-only releases) are skipped, since
    Crucible can only update a dedicated server from a server pack.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PackUpdateError(f"GTNH version index was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PackUpdateError("GTNH version index had an unexpected shape.")

    out: list = []
    for version, entry in data.items():
        if not isinstance(entry, dict):
            continue
        server = entry.get("server")
        if not isinstance(server, dict):
            continue
        urls = {}
        if isinstance(server.get("java8Url"), str) and server["java8Url"].startswith("https://"):
            urls[VARIANT_JAVA8] = server["java8Url"]
        if isinstance(server.get("java17_2XUrl"), str) and server["java17_2XUrl"].startswith("https://"):
            urls[VARIANT_JAVA17] = server["java17_2XUrl"]
        if not urls:
            continue
        changelog = ""
        m = _HREF_RE.search(str(entry.get("description") or ""))
        if m:
            changelog = m.group("url")
        try:
            max_java = int(entry.get("maxJavaVersion") or 0)
        except (TypeError, ValueError):
            max_java = 0
        out.append(PackRelease(
            version=str(version),
            title=str(entry.get("title") or ""),
            channel=channel_of(str(version)),
            release_date=str(entry.get("releaseDate") or ""),
            max_java_version=max_java,
            server_urls=urls,
            changelog_url=changelog,
        ))

    # versions.json is NOT ordered (2.4.0 precedes 2.4.1, betas trail stables),
    # so always sort explicitly. Unparseable names ('April fools 2025') sort
    # last and are never treated as "newer".
    out.sort(key=lambda r: r.sort_key(), reverse=True)
    return out


def fetch_releases(*, url: str = GTNH_VERSIONS_URL, log_cb: LogCb | None = None) -> list:
    """Fetch + parse the GTNH server-pack index, newest first."""
    if log_cb:
        log_cb(f"Fetching version index: {url}")
    raw = _http_bytes(url, limit=_MAX_INDEX_BYTES)
    releases = parse_gtnh_index(raw)
    if log_cb:
        log_cb(f"Found {len(releases)} releases with a downloadable server pack.")
    return releases


def newest_release(releases: list, *, include_prerelease: bool = False) -> PackRelease | None:
    """Newest release, optionally allowing beta/rc."""
    usable = [r for r in releases if parse_pack_version(r.version)]
    if not include_prerelease:
        usable = [r for r in usable if r.channel == "stable"]
    if not usable:
        return None
    return max(usable, key=lambda r: r.sort_key())


def find_release(releases: list, version: str) -> PackRelease | None:
    target = (version or "").strip().lower()
    for r in releases:
        if r.version.strip().lower() == target:
            return r
    return None


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class UpdatePlan:
    instance_path: str = ""
    current: PackIdentity = field(default_factory=PackIdentity)
    target: PackRelease | None = None
    variant: str = VARIANT_JAVA17
    download_url: str = ""
    local_archive: str = ""  # a pack the caller already has on disk
    preserve: list = field(default_factory=list)
    carry_mods: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (not self.blockers and self.target is not None
                and bool(self.download_url or self.local_archive))

    def summary(self) -> str:
        if self.target is None:
            return "No target version selected."
        head = f"{self.current.label()} \u2192 {self.target.version}"
        if self.blockers:
            return f"{head}: blocked ({len(self.blockers)})"
        return f"{head}: {len(self.preserve)} items preserved, {len(self.carry_mods)} added mods carried"


def _world_names(instance) -> list:
    """level-name plus its sibling dimension folders, if they exist."""
    root = Path(getattr(instance, "path", "") or ".")
    level = "world"
    props = root / "server.properties"
    try:
        for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("level-name="):
                candidate = s.split("=", 1)[1].strip()
                if candidate:
                    level = candidate
                break
    except OSError:
        pass
    names = [level, f"{level}_nether", f"{level}_the_end"]
    return [n for n in names if (root / n).is_dir()]


def pack_baseline(server_path: str | Path) -> set:
    """Mod filenames the CURRENT pack shipped, per our own receipt.

    Empty set means "we have no baseline" -- the caller must then fall back to
    the conservative heuristic in compute_carry_mods().
    """
    receipt = read_receipt(server_path)
    mods = receipt.get("pack_mods")
    return {str(m) for m in mods} if isinstance(mods, list) else set()


def compute_carry_mods(instance, *, new_pack_mods: set | None = None) -> tuple:
    """Which mod jars are the OWNER's, not the pack's?

    Returns (carry, notes). Sources, best first:
      1. .crucible/pack-provenance.json 'pack_mods' baseline (exact).
      2. .crucible/added-mods.json -- what Crucible itself installed.
      3. Nothing: report that we cannot tell, and carry only (2).

    Nested mod subdirectories (mods/ic2, mods/OpenSecurity) are pack-owned
    and never carried.
    """
    root = Path(getattr(instance, "path", "") or ".")
    mods_dir = root / "mods"
    notes: list = []
    present: set = set()
    if mods_dir.is_dir():
        for p in mods_dir.iterdir():
            if p.is_file() and (p.suffix == ".jar" or p.name.endswith(".jar.disabled")):
                present.add(p.name)

    added: set = set()
    try:
        rec = json.loads((root / ".crucible" / "added-mods.json").read_text(encoding="utf-8"))
        if isinstance(rec, dict):
            added = {str(k) for k in rec}
    except (OSError, ValueError):
        pass

    baseline = pack_baseline(root)
    if baseline:
        carry = sorted((present - baseline) | (added & present))
        notes.append(f"Using recorded pack baseline ({len(baseline)} pack mods).")
    else:
        carry = sorted(added & present)
        notes.append(
            "No pack baseline recorded for this instance, so Crucible can only "
            "identify mods it installed itself. Review the mods list before "
            "updating -- hand-copied jars may not be detected."
        )
        if new_pack_mods:
            extra = sorted(n for n in present - set(new_pack_mods) if n not in carry)
            if extra:
                notes.append(
                    f"{len(extra)} jar(s) exist here but not in the new pack: "
                    + ", ".join(extra[:6]) + (" ..." if len(extra) > 6 else "")
                )
    return carry, notes


def plan_update(
    instance,
    target: PackRelease,
    *,
    running: bool | None = False,
    identity: PackIdentity | None = None,
    installed_java: int | None = None,
    allow_downgrade: bool = False,
    min_free_bytes: int = 2 * 1024 ** 3,
    local_archive: Path | str | None = None,
) -> UpdatePlan:
    """Build a reviewable plan. Never touches the filesystem.

    `running` is supplied by the caller (TmuxManager.probe_running); None means
    "could not tell", which is a BLOCKER, not a green light -- the same
    fail-closed rule the rest of Crucible uses before destructive work.
    """
    root = Path(getattr(instance, "path", "") or ".")
    ident = identity or identify_pack(instance)
    plan = UpdatePlan(instance_path=str(root), current=ident, target=target,
                      variant=ident.variant or VARIANT_JAVA17)

    if running is None:
        plan.blockers.append(
            "Could not verify whether the server is running. Refusing to update "
            "until tmux reports a definite state."
        )
    elif running:
        plan.blockers.append(
            "The server is running. Stop it first -- swapping mods under a live "
            "JVM corrupts chunks."
        )

    if not root.is_dir():
        plan.blockers.append(f"Server folder does not exist: {root}")

    # Where the new pack comes from: a file the user already has, or a download.
    if local_archive:
        arc = Path(local_archive).expanduser()
        plan.local_archive = str(arc)
        if not arc.is_file():
            plan.blockers.append(f"Server pack file not found: {arc}")
    else:
        plan.download_url = target.server_url(plan.variant)
        if not plan.download_url:
            other = VARIANT_JAVA8 if plan.variant == VARIANT_JAVA17 else VARIANT_JAVA17
            if target.server_url(other):
                plan.blockers.append(
                    f"{target.version} has no {plan.variant} server pack, only {other}. "
                    "Switching Java variants changes the launch scripts, so do that "
                    "as a fresh import rather than an update."
                )
            else:
                plan.blockers.append(f"{target.version} has no downloadable server pack.")

    # Version sanity.
    cur = parse_pack_version(ident.version) if ident.known else None
    new = parse_pack_version(target.version)
    if cur is None:
        plan.warnings.append(
            "Crucible could not confirm which version is installed "
            f"({ident.confidence}). The update will still be staged and "
            "backed up, but it cannot verify this is a step forward."
        )
    elif new is not None:
        if new < cur and not allow_downgrade:
            plan.blockers.append(
                f"{target.version} is older than the installed {ident.version}. "
                "Rolling a world back to an older pack usually breaks it."
            )
        elif new == cur:
            plan.warnings.append(f"{target.version} is already installed; this is a reinstall.")
        elif new[0] != cur[0] or new[1] != cur[1]:
            plan.warnings.append(
                f"This crosses a major version ({ident.version} \u2192 {target.version}). "
                "GTNH recommends updating one major version at a time and "
                "warns that world-breaking changes are possible."
            )

    if target.channel in ("beta", "rc", "alpha"):
        plan.warnings.append(
            f"{target.version} is a {target.channel} release, not a stable one. "
            "Known issues have been reported running new betas against older "
            "stable worlds -- keep the pre-update backup."
        )

    if installed_java and target.max_java_version and installed_java > target.max_java_version:
        plan.warnings.append(
            f"Java {installed_java} is installed but {target.version} supports at "
            f"most Java {target.max_java_version}."
        )

    # Preserve list.
    worlds = _world_names(instance)
    plan.preserve = list(worlds)
    for d in PRESERVE_DIRS:
        if (root / d).is_dir():
            plan.preserve.append(d)
    for f in PRESERVE_FILES:
        if (root / f).is_file():
            plan.preserve.append(f)
    if not worlds:
        plan.warnings.append(
            "No world folder found, so nothing world-shaped will be preserved. "
            "If this server has a world, check level-name in server.properties."
        )

    plan.carry_mods, notes = compute_carry_mods(instance)
    plan.warnings.extend(notes)
    if plan.carry_mods:
        plan.warnings.append(
            f"{len(plan.carry_mods)} owner-added mod(s) will be copied into the new "
            "pack. They are NOT checked for compatibility with the new version."
        )

    # Disk: zip + extracted copy + the swapped-aside old tree all coexist.
    try:
        free = shutil.disk_usage(root if root.is_dir() else root.parent).free
        need = _dir_size(root, cap=64 * 1024 ** 3) * 2 + min_free_bytes
        if free < need:
            plan.blockers.append(
                f"Not enough free disk space: about {need / 1024 ** 3:.1f} GiB needed, "
                f"{free / 1024 ** 3:.1f} GiB free."
            )
    except OSError:
        plan.warnings.append("Could not check free disk space.")

    plan.warnings.append(
        "After the first start on the new version, the console may ask to "
        "confirm removed blocks/items. Answer with '/fml confirm' only once you "
        "have the backup Crucible just made."
    )
    return plan


def installed_java_major() -> int | None:
    """Best-effort major version of the `java` on PATH, or None.

    Used to warn when the JVM is newer than a pack supports. GTNH publishes a
    maxJavaVersion per release and running past it fails in confusing ways.
    """
    exe = shutil.which("java")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    if major == 1 and m.group(2):
        return int(m.group(2))  # "1.8.0_452" -> 8
    return major


def _dir_size(root: Path, *, cap: int) -> int:
    total = 0
    try:
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
                if total > cap:
                    return total
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


@dataclass
class UpdateResult:
    ok: bool = False
    from_version: str = ""
    to_version: str = ""
    messages: list = field(default_factory=list)
    failed_reason: str = ""
    cancelled: bool = False
    backup_path: str = ""
    previous_dir: str = ""
    archive_path: str = ""
    carried_mods: list = field(default_factory=list)

    def summary(self) -> str:
        if self.cancelled:
            return "Update cancelled; nothing was changed."
        if not self.ok:
            return f"Update failed: {self.failed_reason}"
        bits = [f"Updated {self.from_version or 'unknown'} \u2192 {self.to_version}"]
        if self.carried_mods:
            bits.append(f"{len(self.carried_mods)} added mod(s) carried over")
        if self.previous_dir:
            bits.append("previous install kept alongside")
        return "; ".join(bits) + "."


def download_pack(
    url: str,
    dest_dir: Path,
    *,
    progress_cb: ProgressCb | None = None,
    log_cb: LogCb | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Stream a server pack to dest_dir atomically. Raises PackUpdateError."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PackUpdateError("Refusing a non-HTTPS download URL.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.unquote(parsed.path)).name or "serverpack.zip"
    dest = dest_dir / name
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if log_cb:
        log_cb(f"Downloading {name}")
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_ssl_ctx()) as r:
            declared = r.headers.get("Content-Length")
            total = int(declared) if declared and declared.isdigit() else 0
            if total > _MAX_PACK_BYTES:
                raise PackUpdateError("Server pack exceeds the safe size limit.")
            done = 0
            with tmp.open("wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise PackUpdateError("cancelled")
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > _MAX_PACK_BYTES:
                        raise PackUpdateError("Server pack exceeds the safe size limit.")
                    out.write(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        if total and done != total:
            raise PackUpdateError(f"Download was truncated ({done} of {total} bytes).")
        tmp.replace(dest)
        if log_cb:
            log_cb(f"Downloaded {done / 1024 ** 2:.0f} MiB")
        return dest
    except PackUpdateError:
        tmp.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        tmp.unlink(missing_ok=True)
        raise PackUpdateError(f"Download failed: {exc}") from exc


def _staged_root(extracted: Path) -> Path:
    """GTNH server packs extract flat, but tolerate a single wrapper folder."""
    if (extracted / "mods").is_dir():
        return extracted
    children = [c for c in extracted.iterdir() if c.is_dir()]
    if len(children) == 1 and (children[0] / "mods").is_dir():
        return children[0]
    return extracted


def _copy_preserved(live: Path, staging: Path, names: list, log_cb: LogCb | None) -> int:
    copied = 0
    for name in names:
        src = live / name
        if not src.exists():
            continue
        dst = staging / name
        try:
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            copied += 1
        except OSError as exc:
            if log_cb:
                log_cb(f"Could not preserve {name}: {exc}")
            raise
    return copied


def _write_receipt(server_path: Path, *, provider: str, version: str, variant: str,
                   previous_version: str, archive: str, carried: list) -> None:
    """Record what the pack shipped so the NEXT update can diff precisely."""
    mods_dir = server_path / "mods"
    pack_mods: list = []
    if mods_dir.is_dir():
        pack_mods = sorted(
            p.name for p in mods_dir.iterdir()
            if p.is_file() and p.suffix == ".jar" and p.name not in set(carried)
        )
    payload = {
        "provider": provider,
        "version": version,
        "variant": variant,
        "previous_version": previous_version,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "crucible_version": __version__,
        "source_archive": archive,
        "carried_mods": sorted(carried),
        "pack_mods": pack_mods,
    }
    out = server_path / ".crucible" / "pack-provenance.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(out)
    except OSError:
        pass


def _update_safety_blocker(
    instance,
    *,
    running_check: Callable[[], bool | None] | None = None,
    port_check: Callable[[object], str | None] | None = None,
) -> str | None:
    """Fail closed unless the server is stopped and its configured port is free.

    The preview check can be stale by the time the owner confirms the update.
    Re-probe both Crucible's tmux session and the configured IPv4 listening port
    at the start of apply and immediately before the live directory rename.
    Callbacks are injectable so tests can exercise race windows without touching
    the machine's actual tmux sessions or ports.
    """
    manager = None
    if running_check is None or port_check is None:
        try:
            from ..process.tmux_manager import TmuxManager
            manager = TmuxManager()
        except Exception:
            return (
                "Crucible could not initialize the server/port safety checks. "
                "Refusing to apply the update."
            )

    try:
        running = running_check() if running_check is not None else manager.probe_running(instance)
    except Exception:
        running = None
    if running is None:
        return (
            "Crucible could not verify whether the server is running. "
            "Refusing to update until the status check succeeds."
        )
    if running:
        return "The server is running. Stop it before applying the pack update."

    try:
        conflict = (
            port_check(instance)
            if port_check is not None
            else manager._port_conflict_message(instance)
        )
    except Exception:
        return (
            "Crucible could not verify whether the configured server port is free. "
            "Refusing to apply the update."
        )
    if conflict:
        return str(conflict)
    return None


def apply_update(
    instance,
    plan: UpdatePlan,
    *,
    backup_cb: Callable[[], str] | None = None,
    archive: Path | str | None = None,
    log_cb: LogCb | None = None,
    progress_cb: ProgressCb | None = None,
    cancel: threading.Event | None = None,
    keep_previous: bool = True,
    running_check: Callable[[], bool | None] | None = None,
    port_check: Callable[[object], str | None] | None = None,
) -> UpdateResult:
    """Execute a plan as a stage-and-swap. Never raises.

    backup_cb MUST take the mandatory world backup and return its path. It is
    injected rather than imported so this module stays free of PyQt (the real
    caller passes BackupManager.create_backup). Passing None is allowed only
    for a server with no world yet.

    `archive` lets a caller reuse an already-downloaded zip (or a local file)
    instead of re-downloading. The server-running and port checks are repeated
    immediately before backup/staging and again before the live rename, because
    the preview may have gone stale while the user was reading it. Optional
    check callbacks exist for deterministic tests; production callers use
    fresh TmuxManager and configured-port probes by default.
    """
    from .prism import _extract_archive  # local import: avoids a cycle

    res = UpdateResult(from_version=plan.current.version, to_version=(plan.target.version if plan.target else ""))
    log = log_cb or (lambda _m: None)

    if not plan.ok:
        res.failed_reason = plan.blockers[0] if plan.blockers else "The update plan is not runnable."
        return res

    live = Path(plan.instance_path)
    staging_parent = live.parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging = staging_parent / f"{live.name}.crucible-staging-{stamp}"
    previous = staging_parent / f"{live.name}.pre-update-{stamp}"
    extracted: Path | None = None
    swapped = False

    try:
        if cancel is not None and cancel.is_set():
            res.cancelled = True
            return res

        # The review can sit open for minutes. Recheck before taking a backup
        # or doing any update work rather than trusting the preview's old state.
        blocker = _update_safety_blocker(
            instance, running_check=running_check, port_check=port_check
        )
        if blocker:
            res.failed_reason = blocker
            return res

        # 1. Mandatory backup FIRST -- before a single byte is downloaded.
        if backup_cb is not None:
            log("Taking a verified world backup before touching anything...")
            res.backup_path = str(backup_cb() or "")
            log(f"Backup complete: {res.backup_path}")
        else:
            res.messages.append("No backup callback supplied; no world backup was taken.")

        # 2. Acquire the pack: an explicit archive, one the plan already
        #    resolved on disk, or a fresh download.
        archive = archive or (plan.local_archive or None)
        if archive:
            arc = Path(archive)
            if not arc.is_file():
                res.failed_reason = f"Server pack not found: {arc}"
                return res
        else:
            arc = download_pack(
                plan.download_url, live / ".crucible" / "downloads",
                progress_cb=progress_cb, log_cb=log, cancel=cancel,
            )
        res.archive_path = str(arc)

        # 2b. Verify the archive really is a server pack BEFORE unpacking a
        #     gigabyte of it, and long before the live tree is touched. This
        #     is what catches a client pack or a mods-less index pack.
        probe = probe_archive(arc)
        res.messages.extend(probe.notes)
        if probe.problems:
            res.failed_reason = probe.problems[0]
            return res
        log(f"Archive checks out: {probe.label()}")
        if probe.version and plan.target and plan.target.version not in ("custom", ""):
            if probe.version != plan.target.version:
                res.messages.append(
                    f"Note: the archive identifies itself as {probe.version}, "
                    f"not {plan.target.version}."
                )

        if cancel is not None and cancel.is_set():
            res.cancelled = True
            return res

        # 3. Extract (zip-slip / symlink / ratio hardened, shared with import).
        log("Extracting the new pack...")
        # Extract onto the instance's own filesystem so the staging tree can be
        # renamed into place later instead of copied across devices.
        extracted, warns = _extract_archive(arc, staging_parent)
        for w in warns:
            res.messages.append(w)
        source_root = _staged_root(extracted)
        if not (source_root / "mods").is_dir():
            res.failed_reason = (
                "The downloaded archive does not look like a server pack "
                "(no mods/ folder inside)."
            )
            return res

        # 4. Stage: new pack, then the owner's data on top.
        log("Staging the new install...")
        shutil.move(str(source_root), str(staging))
        log(f"Preserving {len(plan.preserve)} item(s) from the current install...")
        _copy_preserved(live, staging, plan.preserve, log)

        carried: list = []
        if plan.carry_mods:
            (staging / "mods").mkdir(parents=True, exist_ok=True)
            for jar in plan.carry_mods:
                src = live / "mods" / jar
                if src.is_file():
                    shutil.copy2(src, staging / "mods" / jar)
                    carried.append(jar)
            log(f"Carried {len(carried)} owner-added mod(s) forward.")
        res.carried_mods = carried

        # Keep the launch scripts executable, exactly as import does.
        for script in list(staging.glob("*.sh")):
            try:
                script.chmod(0o755)
            except OSError:
                pass

        if cancel is not None and cancel.is_set():
            res.cancelled = True
            return res

        # A server may have been started manually while download/staging ran.
        # Recheck immediately before the destructive rename. In particular,
        # port occupancy catches a JVM that isn't in Crucible's tmux session.
        blocker = _update_safety_blocker(
            instance, running_check=running_check, port_check=port_check
        )
        if blocker:
            res.failed_reason = (
                f"{blocker} The live install was not swapped; staged content was discarded."
            )
            if res.backup_path:
                res.messages.append(f"Pre-update backup retained at {res.backup_path}.")
            return res

        # 5. Swap. Two same-filesystem renames -- metadata only, no copying.
        log("Swapping the new install into place...")
        live.rename(previous)
        try:
            staging.rename(live)
        except OSError:
            previous.rename(live)  # put it straight back
            raise
        swapped = True
        res.previous_dir = str(previous)

        # 6. Receipt for the next update.
        _write_receipt(
            live, provider=plan.current.provider or "gtnh",
            version=plan.target.version, variant=plan.variant,
            previous_version=plan.current.version, archive=str(arc),
            carried=carried,
        )

        if not keep_previous:
            shutil.rmtree(previous, ignore_errors=True)
            res.previous_dir = ""
        else:
            res.messages.append(
                f"Previous install kept at {previous.name} -- delete it once the "
                "new version has started cleanly."
            )

        res.ok = True
        log(res.summary())
        return res

    except PackUpdateError as exc:
        if str(exc) == "cancelled":
            res.cancelled = True
        else:
            res.failed_reason = str(exc)
        return res
    except (OSError, ValueError, shutil.Error) as exc:
        res.failed_reason = f"{type(exc).__name__}: {exc}"
        return res
    finally:
        # Roll back anything half-done, then clean scratch space.
        if not res.ok:
            try:
                if swapped and previous.exists():
                    if live.exists():
                        shutil.rmtree(live, ignore_errors=True)
                    previous.rename(live)
                    res.messages.append("Rolled back: the previous install was restored.")
            except OSError as exc:
                res.messages.append(
                    f"CRITICAL: rollback failed ({exc}). The previous install is at "
                    f"{previous}."
                )
            shutil.rmtree(staging, ignore_errors=True)
        if extracted is not None:
            shutil.rmtree(extracted, ignore_errors=True)


def check_for_update(
    instance,
    *,
    include_prerelease: bool = False,
    url: str = GTNH_VERSIONS_URL,
    log_cb: LogCb | None = None,
) -> tuple:
    """Convenience: (identity, newest, releases, note).

    `newest` is None when already current or nothing newer exists. Network
    failures come back as a note instead of an exception so a GUI can show a
    friendly offline message.
    """
    ident = identify_pack(instance)
    if not ident.known:
        return ident, None, [], (
            "Crucible could not tell which modpack this instance is running, so "
            "it cannot check for updates automatically."
        )
    try:
        releases = fetch_releases(url=url, log_cb=log_cb)
    except PackUpdateError as exc:
        return ident, None, [], str(exc)

    cur = parse_pack_version(ident.version)
    candidates = [
        r for r in releases
        if r.server_url(ident.variant) and parse_pack_version(r.version)
        and (include_prerelease or r.channel == "stable")
    ]
    if not candidates:
        return ident, None, releases, "No server packs found in the version index."
    best = max(candidates, key=lambda r: r.sort_key())
    if cur is not None and best.sort_key() <= cur:
        return ident, None, releases, f"{ident.label()} is up to date."
    return ident, best, releases, f"{best.version} is available ({best.title or best.channel})."


# Ingesting a source the user supplies
#
# The end user does not always want "whatever the index says is newest". Often
# they paste a link somebody gave them, or point at a zip already sitting in
# ~/Downloads. Everything below turns free-form input into something
# plan_update() can accept -- and, more importantly, says out loud what it
# thinks that input actually IS before anything destructive happens.

SOURCE_LOCAL_FILE = "local_file"
SOURCE_DIRECT_URL = "direct_url"
SOURCE_INDEX_URL = "index_url"
SOURCE_INDEX_VERSION = "index_version"
SOURCE_UNKNOWN = "unknown"

_PACK_SUFFIXES = (".zip", ".mrpack")

# Fragments that mean "this download is for playing the game, not hosting it".
# Swapping a client pack onto a server leaves a folder that cannot start.
_CLIENT_URL_MARKERS = (
    "_client_", "clientpack", "client_java", "/clientpacks/",
    "multi_mc", "multimc", "/mmc_", "/multi_mc_downloads/", "prism",
)

ARCHIVE_SERVER_PACK = "server_pack"
ARCHIVE_CLIENT_PACK = "client_pack"
ARCHIVE_MODPACK_INDEX = "modpack_index"
ARCHIVE_UNKNOWN = "unknown"

# Top-level names that only ever appear in a client install.
_CLIENT_ONLY_TOP = ("resourcepacks", "shaderpacks", "saves", "options.txt",
                    "servers.dat", "screenshots", "texturepacks")
_SERVER_SCRIPT_NAMES = (
    "startserver-java9.sh", "startserver-java17.sh", "startserver.sh",
    "serverstart.sh", "start.sh", "run.sh", "launch.sh", "run-server.sh",
)


@dataclass
class PackSource:
    """What Crucible thinks a pasted link / path / version string refers to."""

    kind: str = SOURCE_UNKNOWN
    raw: str = ""
    url: str = ""
    path: str = ""
    provider: str = ""
    version: str = ""
    variant: str = ""
    filename: str = ""
    confidence: str = "none"  # high | medium | low | none
    questions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.kind != SOURCE_UNKNOWN

    @property
    def needs_confirmation(self) -> bool:
        """True when a human should look before this is installed."""
        return bool(self.questions) or self.confidence in ("none", "low")

    def label(self) -> str:
        if self.provider == "gtnh" and self.version:
            variant = "Java 8" if self.variant == VARIANT_JAVA8 else "Java 17+"
            return f"GT New Horizons {self.version} server pack ({variant})"
        if self.version:
            return f"modpack version {self.version}"
        return self.filename or self.url or self.path or self.raw or "unknown source"


def _clean_input(text: str) -> str:
    """Be forgiving about what a human actually pastes."""
    s = (text or "").strip()
    m = re.search(r"\]\((?P<url>[^)\s]+)\)", s)  # markdown [label](url)
    if m:
        s = m.group("url")
    s = s.strip().strip("<>").strip().strip('"').strip("'").strip()
    while s and s[-1] in ".,;:":  # trailing punctuation from a chat message
        s = s[:-1]
    return s


def resolve_pack_source(text: str, *, expected_variant: str = "") -> PackSource:
    """Classify free-form user input into something updatable.

    Accepts a direct https link to a server-pack zip, a link to a release
    index (versions.json), a path to a local .zip/.mrpack, or a bare version
    string. Does no I/O beyond stat()ing a local path, so it is safe to call
    on every keystroke in a dialog.
    """
    raw = _clean_input(text)
    src = PackSource(raw=raw, variant=expected_variant)
    if not raw:
        src.questions.append(
            "Nothing was entered. Paste a link, a file path, or a version like 2.8.4.")
        return src

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in ("http", "https", "ftp", "file"):
        if parsed.scheme != "https":
            src.warnings.append(
                f"{parsed.scheme}:// is not accepted. A modpack is code that will "
                "run as your server user, so it is only fetched over HTTPS."
            )
            src.questions.append("Is there an https:// version of this link?")
            return src
        src.url = raw
        src.filename = Path(urllib.parse.unquote(parsed.path)).name
        low = urllib.parse.unquote(raw).lower()
        suffix = Path(src.filename).suffix.lower()

        if suffix == ".json":
            src.kind = SOURCE_INDEX_URL
            src.confidence = "high"
            src.provider = "gtnh" if "gtnewhorizons" in low else ""
            return src

        if suffix not in _PACK_SUFFIXES:
            src.questions.append(
                f"That link does not end in {' or '.join(_PACK_SUFFIXES)}, so "
                "Crucible cannot tell whether it points at a modpack. Is it a "
                "direct download link to a server pack?"
            )
            return src

        src.kind = SOURCE_DIRECT_URL
        hit = identify_gtnh_name(src.filename)
        if hit:
            src.provider = "gtnh"
            src.version, detected = hit
            src.variant = expected_variant or detected
            src.confidence = "high"
            if expected_variant and detected != expected_variant:
                src.warnings.append(
                    f"This is the {detected} build but the server looks like "
                    f"{expected_variant}. The two variants ship different launch "
                    "scripts, so mixing them is not an update."
                )
                src.questions.append(f"Really install the {detected} build?")
        else:
            src.confidence = "medium" if parse_pack_version(Path(src.filename).stem) else "low"
            src.questions.append(
                f"Crucible does not recognise {src.filename!r} as a known pack "
                "name. Which modpack and version is this?"
            )

        if any(m in low for m in _CLIENT_URL_MARKERS) or suffix == ".mrpack":
            src.warnings.append(
                f"{src.filename!r} looks like a CLIENT/launcher pack rather than a "
                "dedicated server pack. Installing it would leave a folder with "
                "no way to start."
            )
            src.questions.append("Is this really the server pack download?")
            src.confidence = "low"
        return src

    # Not a URL: a local file, or a version string.
    candidate = Path(raw).expanduser()
    if candidate.exists():
        if candidate.is_dir():
            src.questions.append(
                f"{candidate} is a folder. Updating installs a pack archive; to "
                "adopt an existing folder, import it as a new instance instead."
            )
            return src
        if candidate.suffix.lower() not in _PACK_SUFFIXES:
            src.questions.append(
                f"{candidate.name} is not a {' or '.join(_PACK_SUFFIXES)} archive. "
                "Is it a modpack?"
            )
            return src
        src.kind = SOURCE_LOCAL_FILE
        src.path = str(candidate)
        src.filename = candidate.name
        hit = identify_gtnh_name(candidate.name)
        if hit:
            src.provider = "gtnh"
            src.version, detected = hit
            src.variant = expected_variant or detected
            src.confidence = "high"
        else:
            src.confidence = "low"
            src.questions.append(
                f"Crucible does not recognise {candidate.name!r}. Which modpack "
                "and version is this?"
            )
        return src

    if parse_pack_version(raw):
        src.kind = SOURCE_INDEX_VERSION
        src.version = raw
        src.confidence = "high"
        return src

    src.questions.append(
        f"{raw!r} is not a link, an existing file, or a version number. Paste a "
        "direct https:// link to a server pack, a path to a downloaded zip, or "
        "a version like 2.8.4."
    )
    return src


@dataclass
class ArchiveProbe:
    """What is actually inside an archive, read from its index only."""

    path: str = ""
    kind: str = ARCHIVE_UNKNOWN
    version: str = ""
    variant: str = ""
    root_prefix: str = ""
    mod_count: int = 0
    entry_count: int = 0
    uncompressed_bytes: int = 0
    start_scripts: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind == ARCHIVE_SERVER_PACK and not self.problems

    def label(self) -> str:
        bits = []
        if self.version:
            bits.append(self.version)
        bits.append({
            ARCHIVE_SERVER_PACK: "server pack",
            ARCHIVE_CLIENT_PACK: "client pack",
            ARCHIVE_MODPACK_INDEX: "modpack index (mods not included)",
        }.get(self.kind, "unrecognised archive"))
        if self.mod_count:
            bits.append(f"{self.mod_count} mods")
        if self.variant:
            bits.append("Java 8" if self.variant == VARIANT_JAVA8 else "Java 17+")
        if self.uncompressed_bytes:
            bits.append(f"{self.uncompressed_bytes / 1024 ** 3:.2f} GiB unpacked")
        return ", ".join(bits)


# Names that are pack *content*, so finding only one of them at the top does
# not mean the pack was wrapped in a folder. Without this, a lean archive
# containing just mods/ would have mods/ stripped as a wrapper and would then
# look like it had no mods at all.
_NEVER_WRAPPER = frozenset({
    "mods", "config", "defaultconfigs", "scripts", "kubejs", "libraries",
    "resourcepacks", "shaderpacks", "saves", "world", "logs", "overrides",
    "coretweaks", "journeymap", "serverutilities", "visualprospecting",
})


def _wrapper_prefix(names: list) -> str:
    """The single top-level folder everything sits under, if there is one."""
    tops = {n.split("/", 1)[0] for n in names if n and not n.startswith("/")}
    tops.discard("")
    if len(tops) != 1:
        return ""
    only = tops.pop()
    if only.lower() in _NEVER_WRAPPER:
        return ""
    if all(n == only or n.startswith(only + "/") for n in names):
        return only
    return ""


def probe_archive(path: str | Path) -> ArchiveProbe:
    """Look inside an archive WITHOUT extracting it.

    This is the "is this really a modpack?" check. It reads only the zip's
    central directory, and it runs before the world backup and before anything
    is swapped -- so a client pack, a mods-less index pack or a random zip is
    caught while the live install is still completely untouched.
    """
    p = Path(path)
    probe = ArchiveProbe(path=str(p))
    if not p.is_file():
        probe.problems.append(f"Not a file: {p}")
        return probe
    try:
        with zipfile.ZipFile(p) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        probe.problems.append(f"Not a readable zip archive: {exc}")
        return probe

    names = [i.filename.replace("\\", "/") for i in infos]
    probe.entry_count = len(names)
    probe.uncompressed_bytes = sum(i.file_size for i in infos)
    prefix = _wrapper_prefix(names)
    probe.root_prefix = prefix

    def rel(n: str) -> str:
        if prefix and n.startswith(prefix + "/"):
            return n[len(prefix) + 1:]
        return n

    rels = [rel(n) for n in names]
    lower = [r.lower() for r in rels]

    probe.mod_count = sum(
        1 for r in lower if r.startswith("mods/") and r.endswith(".jar"))
    tops = {r.split("/", 1)[0].lower() for r in rels if r}

    for r, low in zip(rels, lower):
        if "/" not in low and low in _SERVER_SCRIPT_NAMES:
            probe.start_scripts.append(r)

    has_index = any(low in ("modrinth.index.json", "manifest.json") for low in lower)
    client_hits = sorted(t for t in tops if t in _CLIENT_ONLY_TOP)

    if probe.mod_count and probe.start_scripts:
        probe.kind = ARCHIVE_SERVER_PACK
    elif has_index and not probe.mod_count:
        probe.kind = ARCHIVE_MODPACK_INDEX
        probe.problems.append(
            "This is a modpack index: it lists mods to download rather than "
            "containing them. Crucible updates from a complete server pack."
        )
    elif client_hits and not probe.start_scripts:
        probe.kind = ARCHIVE_CLIENT_PACK
        probe.problems.append(
            "This looks like a client pack (contains " + ", ".join(client_hits)
            + ") with no server start script."
        )
    elif probe.mod_count:
        probe.kind = ARCHIVE_SERVER_PACK
        probe.notes.append(
            "No recognised start script at the top level; this pack's launcher "
            "may be named something unusual."
        )
    else:
        probe.problems.append(
            "No mods/ folder inside, so this archive is not a server pack.")

    if client_hits and probe.kind == ARCHIVE_SERVER_PACK:
        probe.notes.append(
            "Contains client-only folders (" + ", ".join(client_hits)
            + "); they are harmless but unused on a server."
        )

    # Java variant, from what the pack actually ships.
    if "java9args.txt" in lower or any(
            low.endswith("lwjgl3ify-forgepatches.jar") for low in lower):
        probe.variant = VARIANT_JAVA17
    elif "startserver-java9.sh" in lower or "startserver-java17.sh" in lower:
        probe.variant = VARIANT_JAVA17

    # Version, from the wrapper folder, the filename, or a shipped changelog.
    hit = identify_gtnh_name(prefix) or identify_gtnh_name(p.name)
    if hit:
        probe.version, detected = hit
        probe.variant = probe.variant or detected
    else:
        for r in rels:
            m = _CHANGELOG_RE.match(r.rsplit("/", 1)[-1].strip())
            if m:
                probe.version = m.group("new")
                break

    if probe.uncompressed_bytes > _MAX_PACK_BYTES:
        probe.problems.append(
            f"Archive expands to {probe.uncompressed_bytes / 1024 ** 3:.1f} GiB, "
            "past the safety ceiling."
        )
    return probe


def release_from_source(source: PackSource, *,
                        probe: ArchiveProbe | None = None) -> PackRelease:
    """Wrap a user-supplied source as a PackRelease so plan_update can take it.

    A hand-supplied pack has no release metadata, so the version is whatever
    the archive or its name admits to, and max_java_version stays 0 (unknown)
    rather than being invented.
    """
    version = (probe.version if probe and probe.version else "") or source.version
    variant = ((probe.variant if probe and probe.variant else "")
               or source.variant or VARIANT_JAVA17)
    urls = {}
    if source.url:
        # The user pasted this exact link, so it is the answer for whichever
        # variant the planner ends up asking about. Filing it under only one
        # variant re-introduces a bogus "no server pack for your Java variant"
        # failure on a request that was completely explicit.
        urls = {VARIANT_JAVA8: source.url, VARIANT_JAVA17: source.url}
    return PackRelease(
        version=version or "custom",
        title="user-supplied pack",
        channel=channel_of(version) if version else "unknown",
        server_urls=urls,
    )


# Undo
#
# apply_update() renames the old install aside as "<name>.pre-update-<stamp>"
# instead of deleting it. That tree is a complete, bootable copy of the old
# pack WITH the world as it was at update time, which makes a real undo
# possible without unzipping a backup.

_PREV_MARKER = ".pre-update-"
_ROLLED_MARKER = ".rolled-back-"


@dataclass
class PreviousInstall:
    path: str = ""
    stamp: str = ""
    version: str = ""

    def label(self) -> str:
        when = self.stamp
        if len(self.stamp) == 15 and "_" in self.stamp:  # YYYYmmdd_HHMMSS
            d, t = self.stamp.split("_", 1)
            when = f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}"
        bits = [when]
        if self.version:
            bits.append(f"version {self.version}")
        return " \u00b7 ".join(bits)


def list_previous_installs(instance) -> list:
    """Kept pre-update trees for an instance, newest first."""
    root = Path(getattr(instance, "path", "") or ".")
    out = []
    try:
        for p in root.parent.glob(f"{root.name}{_PREV_MARKER}*"):
            if not p.is_dir():
                continue
            stamp = p.name.rsplit(_PREV_MARKER, 1)[-1]
            entry = PreviousInstall(path=str(p), stamp=stamp)
            receipt = read_receipt(p)
            entry.version = str(receipt.get("version", "") or "")
            if not entry.version:
                hit = identify_gtnh_name(p.name)
                if hit:
                    entry.version = hit[0]
            out.append(entry)
    except OSError:
        return []
    out.sort(key=lambda e: e.stamp, reverse=True)
    return out


def rollback_update(
    instance,
    previous: str | Path | None = None,
    *,
    running: bool | None = False,
    keep_current_world: bool = False,
    log_cb: LogCb | None = None,
) -> UpdateResult:
    """Put a kept pre-update tree back in place.

    The tree currently live is renamed aside rather than deleted, so a rollback
    is itself reversible and no world is ever destroyed by this call.

    keep_current_world moves the world folders from the current install into
    the restored one, trading "exactly as it was" for "keep recent play". Off
    by default: if the new pack damaged the world, carrying it back is the one
    thing you do not want.
    """
    log = log_cb or (lambda _m: None)
    res = UpdateResult()
    live = Path(getattr(instance, "path", "") or ".")

    if running is None:
        res.failed_reason = (
            "Could not verify whether the server is running. Refusing to roll "
            "back until tmux reports a definite state."
        )
        return res
    if running:
        res.failed_reason = "The server is running. Stop it before rolling back."
        return res

    if previous is None:
        found = list_previous_installs(instance)
        if not found:
            res.failed_reason = (
                "No kept pre-update install found next to this server. If the "
                "update was run with --no-keep-previous, restore the world "
                "backup instead."
            )
            return res
        prev = Path(found[0].path)
    else:
        prev = Path(previous).expanduser()

    if not prev.is_dir():
        res.failed_reason = f"Not a directory: {prev}"
        return res
    if prev.parent != live.parent or _PREV_MARKER not in prev.name:
        res.failed_reason = (
            f"{prev.name} is not a pre-update tree belonging to this instance."
        )
        return res
    if not (prev / "mods").is_dir():
        res.failed_reason = f"{prev.name} has no mods/ folder; it is not a usable install."
        return res

    receipt = read_receipt(prev)
    res.from_version = str(read_receipt(live).get("version", "") or "")
    res.to_version = str(receipt.get("version", "") or "")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    aside = live.parent / f"{live.name}{_ROLLED_MARKER}{stamp}"
    moved_live = False
    try:
        if live.exists():
            log(f"Moving the current install aside to {aside.name}...")
            live.rename(aside)
            moved_live = True
        log(f"Restoring {prev.name}...")
        prev.rename(live)

        if keep_current_world and moved_live:
            names = _world_names(instance)
            for name in names:
                src = aside / name
                dst = live / name
                if not src.is_dir():
                    continue
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.move(str(src), str(dst))
                log(f"Carried the current {name} back over.")
            if names:
                res.messages.append(
                    f"Kept the current world ({', '.join(names)}) on the restored pack."
                )

        res.ok = True
        res.previous_dir = str(aside) if moved_live else ""
        res.messages.append(
            f"The install that was live is kept at {aside.name}; delete it once "
            "you are happy, or roll forward again by renaming it back."
            if moved_live else "There was no live install to set aside."
        )
        return res
    except OSError as exc:
        # Best effort put-back so the instance is never left with no folder.
        if moved_live and not live.exists():
            try:
                aside.rename(live)
                res.messages.append("Rollback failed; the original install was put back.")
            except OSError:
                res.messages.append(
                    f"Rollback failed AND the original could not be put back. "
                    f"It is at {aside}."
                )
        res.failed_reason = f"Rollback failed: {exc}"
        return res


def prune_previous_installs(instance, *, keep: int = 1) -> tuple:
    """Delete all but the newest `keep` pre-update trees. Returns (freed, names)."""
    found = list_previous_installs(instance)
    freed = 0
    removed = []
    for entry in found[max(keep, 0):]:
        p = Path(entry.path)
        try:
            freed += _dir_size(p, cap=1024 ** 4)
            shutil.rmtree(p)
            removed.append(p.name)
        except OSError:
            continue
    return freed, removed
