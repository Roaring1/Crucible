"""
crucible/data/instance_model.py

One ServerInstance object per registered server directory.
Handles validation, start-script detection, status queries (via tmux),
and JSON serialization.
"""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Files that indicate a runnable dedicated server is actually present
# (as opposed to a client-only pack staging folder).
_SERVER_LAUNCHER_GLOBS = [
    "server.jar",
    "minecraft_server*.jar",
    "fabric-server-launch.jar",
    "quilt-server-launch.jar",
    "paper*.jar",
    "forge-*.jar",
    "neoforge-*.jar",
]


# Known start script names across Minecraft server flavours
# (GTNH, vanilla, Forge, Fabric, Paper, etc.)

_START_SCRIPT_NAMES = [
    "startserver-java9.sh",   # GTNH 2.8.x with java9args.txt
    "startserver-java17.sh",
    "ServerStart.sh",         # GTNH 2.7+ / forge servers
    "startserver.sh",
    "start.sh",
    "run.sh",                 # vanilla 1.17+ server launcher
    "launch.sh",
    "run-server.sh",
]


# Data model

@dataclass
class ServerInstance:
    """
    Represents one registered GTNH server directory.

    Fields that map 1:1 to the JSON registry entry are stored directly.
    Status is always derived live from tmux — never cached here.
    """

    path: str                      # absolute path to server dir (string for JSON compat)
    name: str                      # display name, editable
    version: str       = ""        # server / modpack version string
    pack_source: str   = ""        # prism_instance / modrinth / curseforge / manual
    minecraft_version: str = ""    # detected Minecraft version
    loader: str        = ""        # forge / neoforge / fabric / quilt / vanilla
    loader_version: str = ""       # detected loader version
    prism_source: str  = ""        # original Prism instance/archive path
    notes: str         = ""        # free-form markdown notes
    java_args: str     = "-Xms2G -Xmx4G"
    color: str         = "#c8903a" # accent color for sidebar dot
    tmux_session: str  = ""        # e.g. "my-server" or "gtnh"
    auto_restart: bool = False     # watchdog: auto-restart on unexpected crash
    id: str            = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str    = field(default_factory=lambda: datetime.now().isoformat())
    last_started: str | None = None

    def __post_init__(self) -> None:
        # Auto-generate session name only if caller didn't provide one
        if not self.tmux_session:
            self.tmux_session = self._derive_session_name(self.name)

    # Session name

    @staticmethod
    def _derive_session_name(name: str) -> str:
        """Produce a safe tmux session name from a display name."""
        slug = name.lower().replace(" ", "-").replace("_", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
        return slug or "mc-server"

    # Serialization

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "path":         self.path,
            "name":         self.name,
            "version":      self.version,
            "pack_source":  self.pack_source,
            "minecraft_version": self.minecraft_version,
            "loader":       self.loader,
            "loader_version": self.loader_version,
            "prism_source": self.prism_source,
            "notes":        self.notes,
            "java_args":    self.java_args,
            "color":        self.color,
            "tmux_session": self.tmux_session,
            "auto_restart": self.auto_restart,
            "created_at":   self.created_at,
            "last_started": self.last_started,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ServerInstance":
        return cls(
            id           = d.get("id", str(uuid.uuid4())),
            path         = d["path"],
            name         = d["name"],
            version      = d.get("version", ""),
            pack_source  = d.get("pack_source", ""),
            minecraft_version = d.get("minecraft_version", ""),
            loader       = d.get("loader", ""),
            loader_version = d.get("loader_version", ""),
            prism_source = d.get("prism_source", ""),
            notes        = d.get("notes", ""),
            java_args    = d.get("java_args", "-Xms2G -Xmx4G"),
            color        = d.get("color", "#c8903a"),
            tmux_session = d.get("tmux_session", ""),
            auto_restart = d.get("auto_restart", False),
            created_at   = d.get("created_at", datetime.now().isoformat()),
            last_started = d.get("last_started"),
        )

    # Validation

    def validate(self) -> list[str]:
        """
        Return a list of human-readable problems with this instance.
        Empty list = all clear.
        """
        problems: list[str] = []
        p = Path(self.path)

        if not p.exists():
            problems.append(f"Directory does not exist: {self.path}")
            return problems  # pointless to check further
        if not p.is_dir():
            problems.append(f"Path is not a directory: {self.path}")
            return problems

        if self.get_startscript() is None:
            problems.append(
                "No start script found — expected one of: "
                + ", ".join(_START_SCRIPT_NAMES)
            )

        if not (p / "mods").exists():
            problems.append("mods/ directory not found")

        if not (p / "eula.txt").exists():
            problems.append("eula.txt missing (server will refuse to start)")

        # Warn about nested jars that the mod manager cannot see
        bundled = self.get_bundled_jars()
        if bundled:
            names = ", ".join(j.name for j in bundled)
            problems.append(
                f"Nested jar(s) in mods/ subdirectory — invisible to mod manager "
                f"(must be managed manually): {names}"
            )

        return problems

    # Filesystem helpers

    @property
    def path_obj(self) -> Path:
        return Path(self.path)

    def get_startscript(self) -> Path | None:
        """
        Find the server start script, trying all known GTNH naming variants
        plus a glob fallback for unusual names.
        """
        p = self.path_obj
        for name in _START_SCRIPT_NAMES:
            candidate = p / name
            if candidate.exists():
                return candidate
        # Glob fallback -- catches e.g. start-prod.sh
        for match in sorted(p.glob("start*.sh")) + sorted(p.glob("Start*.sh")):
            return match
        return None

    def get_log_path(self) -> Path | None:
        """
        Return the active log file path for this server.

        GTNH 1.7.10 (FML era) writes nearly all output to fml-server-latest.log,
        not latest.log.  latest.log exists but contains only a thin wrapper with
        a handful of lines from the most recent JVM boot.

        Resolution order (stops at the first file that exists):
          1. logs/fml-server-latest.log  — GTNH 1.7.10 primary log
          2. logs/latest.log             — vanilla / 1.12+ Forge / fallback
        """
        logs = self.path_obj / "logs"
        for name in ("fml-server-latest.log", "latest.log"):
            candidate = logs / name
            if candidate.exists():
                return candidate
        return None

    def get_mod_count(self) -> int:
        """Count enabled mods (*.jar at top level of mods/, ignoring *.jar.disabled).

        Note: jars nested inside subdirectories (e.g. mods/ic2/EJML-core-0.26.jar)
        are NOT counted here — they are not manageable via the mods tab.
        Use get_bundled_jar_count() to discover those separately.
        """
        mods = self.path_obj / "mods"
        if not mods.exists():
            return 0
        return len(list(mods.glob("*.jar")))

    def get_bundled_jars(self) -> list[Path]:
        """
        Return any .jar files nested inside mods/ subdirectories.

        These are NOT managed by ModManager (which only scans the top level).
        They cannot be enabled, disabled, or inspected through the mods tab.
        Examples: mods/ic2/EJML-core-0.26.jar (IC2 math library).
        """
        mods = self.path_obj / "mods"
        if not mods.exists():
            return []
        bundled: list[Path] = []
        for subdir in mods.iterdir():
            if subdir.is_dir():
                for jar in subdir.rglob("*.jar"):
                    bundled.append(jar)
        return bundled

    def get_world_names(self) -> list[str]:
        """
        Return names of world directories present in the server folder.
        Reads level-name from server.properties first (handles custom world names
        like 'jojo's bizarre adventure diamond is unbreakable').
        Falls back to scanning for standard world directory names.
        """
        p = self.path_obj
        found: list[str] = []

        # Primary: read level-name from server.properties
        props = p / "server.properties"
        if props.exists():
            try:
                for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("level-name="):
                        level_name = line.split("=", 1)[1].strip()
                        if level_name and (p / level_name).exists():
                            found.append(level_name)
                        # Also check DIM dirs alongside the level-name dir
                        nether = p / f"{level_name}_nether"
                        the_end = p / f"{level_name}_the_end"
                        if nether.exists():
                            found.append(nether.name)
                        if the_end.exists():
                            found.append(the_end.name)
                        break
            except OSError:
                pass

        if found:
            return found

        # Fallback: standard world dir names
        for candidate in ["world", "world_nether", "world_the_end"]:
            if (p / candidate).exists():
                found.append(candidate)
        return found

    # Server-setup helpers (used by the GUI Setup tab and the CLI 'doctor')

    def eula_path(self) -> Path:
        return self.path_obj / "eula.txt"

    def eula_accepted(self) -> bool | None:
        """True if eula=true, False if eula=false, None if file is missing."""
        p = self.eula_path()
        if not p.exists():
            return None
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip().lower()
                if s.startswith("eula="):
                    return s.split("=", 1)[1].strip() == "true"
        except OSError:
            return None
        return False

    def set_eula_accepted(self, accepted: bool = True) -> None:
        """Write eula.txt with the requested acceptance state (atomic)."""
        p = self.eula_path()
        body = (
            "# Edited by Crucible. By setting eula=true you agree to the\n"
            "# Minecraft EULA (https://aka.ms/MinecraftEULA).\n"
            f"eula={'true' if accepted else 'false'}\n"
        )
        tmp = p.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)

    @staticmethod
    def java_info() -> tuple[bool, str]:
        """Return (found, detail). Detail is the java path or a hint."""
        java = shutil.which("java")
        if java:
            return True, java
        return False, "java not found on PATH — install a JDK/JRE to run the server"

    def has_server_launcher(self) -> bool:
        """True if a dedicated-server jar or loader args dir appears present."""
        p = self.path_obj
        if (p / "libraries").is_dir():
            # Forge/NeoForge unix_args style installs live under libraries/.
            if any(p.glob("libraries/net/*/*/*/unix_args.txt")):
                return True
        for pattern in _SERVER_LAUNCHER_GLOBS:
            if any(p.glob(pattern)):
                return True
        return False

    def server_port(self) -> str:
        props = self.path_obj / "server.properties"
        if props.exists():
            try:
                for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip().startswith("server-port="):
                        port = line.split("=", 1)[1].strip()
                        if port:
                            return port
            except OSError:
                pass
        return "25565"

    # Memory (Xms/Xmx) — used by the GUI Setup tab's in-app memory editor and
    # the CLI. Parses/rewrites only the -Xms/-Xmx tokens inside java_args,
    # preserving every other flag (IPv4 stack flags, GC tuning, etc.) exactly.

    _RE_XMS = re.compile(r"-Xms(\d+)([kKmMgG])")
    _RE_XMX = re.compile(r"-Xmx(\d+)([kKmMgG])")

    @staticmethod
    def _mem_token_to_mb(value: int, unit: str) -> int:
        unit = unit.lower()
        if unit == "g":
            return value * 1024
        if unit == "k":
            return max(1, value // 1024)
        return value  # "m"

    def get_memory_mb(self) -> tuple[int | None, int | None]:
        """Return (xms_mb, xmx_mb) parsed from java_args, or None for either
        that isn't present / isn't parseable."""
        args = self.java_args or ""
        xms = xmx = None
        m = self._RE_XMS.search(args)
        if m:
            xms = self._mem_token_to_mb(int(m.group(1)), m.group(2))
        m = self._RE_XMX.search(args)
        if m:
            xmx = self._mem_token_to_mb(int(m.group(1)), m.group(2))
        return xms, xmx

    def set_memory_mb(self, xms_mb: int, xmx_mb: int) -> None:
        """Rewrite (or insert) the -Xms/-Xmx tokens in java_args in MB units,
        leaving every other flag untouched. Raises ValueError for nonsensical
        input so callers never silently write a broken launch command."""
        xms_mb = int(xms_mb)
        xmx_mb = int(xmx_mb)
        if xms_mb <= 0 or xmx_mb <= 0:
            raise ValueError("Memory values must be positive.")
        if xms_mb > xmx_mb:
            raise ValueError("Minimum memory (-Xms) cannot exceed maximum memory (-Xmx).")
        args = self.java_args or ""
        xms_tok = f"-Xms{xms_mb}M"
        xmx_tok = f"-Xmx{xmx_mb}M"
        if self._RE_XMS.search(args):
            args = self._RE_XMS.sub(xms_tok, args, count=1)
        else:
            args = (xms_tok + " " + args).strip()
        if self._RE_XMX.search(args):
            args = self._RE_XMX.sub(xmx_tok, args, count=1)
        else:
            args = (args + " " + xmx_tok).strip()
        self.java_args = args

    def _mc_version_tuple(self) -> tuple[int, ...]:
        """Best-effort parse of the Minecraft version into a comparable tuple.

        Pulls the first dotted numeric run out of the version string, so
        "1.21.1", "MC 1.21.1" and "1.20" all work. Returns an empty tuple when
        the version is unknown/unparseable (so comparisons stay False-safe).
        """
        import re as _re
        raw = (self.minecraft_version or "") or (self.version or "")
        m = _re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
        if not m:
            return ()
        return tuple(int(g) for g in m.groups() if g is not None)

    def tps_command(self) -> str | None:
        """Console command to query tick performance for this server, or None.

        The correct command depends on BOTH the Minecraft version and loader:

        * Minecraft 1.21+ ships a built-in vanilla ``tick query`` command that
          reports MSPT / tick-rate on every loader (vanilla, Fabric, Forge,
          NeoForge, …). We prefer it because NeoForge 1.21 no longer understands
          the old ``forge tps`` command — sending it just spammed
          "Unknown or incomplete command" every poll.
        * Older Forge uses ``forge tps``; older NeoForge uses ``neoforge tps``.
        * Paper-family servers use ``tps`` across versions.

        Returning None means "this server has no TPS command — don't poll it".
        """
        loader = (self.loader or "").strip().lower()
        mc = self._mc_version_tuple()

        # Paper-family keeps /tps across versions and lacks the vanilla /tick cmd.
        if loader in ("paper", "purpur", "spigot", "bukkit", "folia"):
            return "tps"

        # Minecraft 1.21+ has the vanilla /tick command on every loader.
        if mc >= (1, 21):
            return "tick query"

        if loader == "neoforge":
            return "neoforge tps"
        if loader == "forge":
            return "forge tps"

        # Vanilla/Fabric/Quilt below 1.21 have no built-in TPS command.
        return None

    def readiness(self) -> list[dict]:
        """Friendly pre-flight checklist for non-technical owners.

        Each item: {key, label, ok (bool|None), detail, fix} where 'fix' is a
        machine-readable hint the GUI can turn into an action button:
        'accept_eula', 'install_server', 'start_once', or None.
        """
        items: list[dict] = []
        p = self.path_obj

        exists = p.is_dir()
        items.append({
            "key": "folder", "label": "Server folder exists",
            "ok": exists, "detail": self.path, "fix": None,
        })
        if not exists:
            return items

        java_ok, java_detail = self.java_info()
        items.append({
            "key": "java", "label": "Java is installed",
            "ok": java_ok, "detail": java_detail, "fix": None,
        })

        eula = self.eula_accepted()
        items.append({
            "key": "eula", "label": "Minecraft EULA accepted",
            "ok": eula,
            "detail": ("Accepted" if eula else
                       "eula.txt missing — will be created" if eula is None else
                       "eula=false — the server will refuse to start"),
            "fix": None if eula else "accept_eula",
        })

        launcher = self.has_server_launcher()
        script = self.get_startscript()
        items.append({
            "key": "launcher", "label": "Server program is installed",
            "ok": launcher or script is not None,
            "detail": (str(script) if script else
                       "Found server jar/loader" if launcher else
                       "No server jar/loader found — install the dedicated server"),
            "fix": None if (launcher or script) else "install_server",
        })

        mods = self.get_mod_count()
        items.append({
            "key": "mods", "label": "Mods present",
            "ok": mods > 0 if (p / "mods").exists() else None,
            "detail": f"{mods} mod(s) in mods/" if (p / "mods").exists() else "no mods/ folder",
            "fix": None,
        })

        props_ok = (p / "server.properties").exists()
        items.append({
            "key": "properties", "label": "server.properties present",
            "ok": props_ok,
            "detail": f"port {self.server_port()}" if props_ok else
                      "will be generated on first start",
            "fix": None if props_ok else "start_once",
        })

        if props_ok:
            try:
                from .properties import validate_file
                issues = validate_file(p / "server.properties")
            except Exception:
                issues = []
            errs = [i for i in issues if i.is_error]
            if errs:
                items.append({
                    "key": "settings", "label": "Server settings are valid",
                    "ok": False,
                    "detail": (f"{len(errs)} setting(s) would crash the server "
                               f"(e.g. {errs[0].key}) — click to fix"),
                    "fix": "fix_properties",
                })
            elif issues:
                items.append({
                    "key": "settings", "label": "Server settings are valid",
                    "ok": None,
                    "detail": f"{len(issues)} minor issue(s) — optional cleanup",
                    "fix": "fix_properties",
                })
            else:
                items.append({
                    "key": "settings", "label": "Server settings are valid",
                    "ok": True, "detail": "No problems found", "fix": None,
                })
        return items

    # Display helpers

    def short_id(self) -> str:
        return self.id[:8]

    def __repr__(self) -> str:
        return f"<ServerInstance name={self.name!r} session={self.tmux_session!r}>"
