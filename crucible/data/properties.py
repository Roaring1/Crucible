"""
crucible/data/properties.py

Robust, launcher-agnostic validator & auto-repairer for ``server.properties``.

Why this exists
---------------
Vanilla Minecraft parses several properties with ``Integer.parseInt(...)`` the
instant the server boots.  If any of those keys is blank or non-numeric (a very
common result of hand-editing or a half-written file) the server dies before it
ever starts with:

    java.lang.NumberFormatException: For input string: ""
        at ...DedicatedServerProperties.<init>(DedicatedServerProperties.java:110)

That error message names no key, so a non-technical owner has no idea what to
fix.  This module finds the offending key(s), explains them in plain language,
and offers a "did you mean —" menu of safe replacement values so the problem can
be fixed in one click (GUI) or automatically before start (CLI).

Design goals
------------
* **Never raises** on bad input — a quirky file produces issues, not a crash.
* **Order- and comment-preserving** — re-saving keeps the user's file intact.
* **Conservative** — only crash-causing problems (blank/invalid numbers) are
  treated as start-blocking *errors*; everything else is a *warning* with a
  suggestion the user may accept or ignore.
* **No third-party deps** — typo detection uses the stdlib ``difflib``.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------- #
# Property schema
# --------------------------------------------------------------------------- #
# type values: "int", "bool", "enum", "str"
# Defaults follow vanilla server.properties for modern releases (1.20+).

@dataclass(frozen=True)
class PropSpec:
    key: str
    type: str
    default: str
    options: tuple[str, ...] = ()      # for enums
    minimum: Optional[int] = None      # for ints (inclusive)
    maximum: Optional[int] = None      # for ints (inclusive)


# Keys vanilla parses with Integer.parseInt -> a blank/garbage value crashes boot.
_INT = "int"
_BOOL = "bool"
_ENUM = "enum"
_STR = "str"

_SPECS: dict[str, PropSpec] = {s.key: s for s in [
    # ---- numeric (THE crash-causing group) ----
    PropSpec("server-port", _INT, "25565", minimum=1, maximum=65535),
    PropSpec("query.port", _INT, "25565", minimum=1, maximum=65535),
    PropSpec("rcon.port", _INT, "25575", minimum=1, maximum=65535),
    PropSpec("max-players", _INT, "20", minimum=0, maximum=2147483647),
    PropSpec("view-distance", _INT, "10", minimum=2, maximum=32),
    PropSpec("simulation-distance", _INT, "10", minimum=2, maximum=32),
    PropSpec("spawn-protection", _INT, "16", minimum=0, maximum=2147483647),
    PropSpec("op-permission-level", _INT, "4", minimum=0, maximum=4),
    PropSpec("function-permission-level", _INT, "2", minimum=1, maximum=4),
    PropSpec("player-idle-timeout", _INT, "0", minimum=0, maximum=2147483647),
    PropSpec("network-compression-threshold", _INT, "256", minimum=-1, maximum=2147483647),
    PropSpec("max-tick-time", _INT, "60000", minimum=-1, maximum=2147483647),
    PropSpec("entity-broadcast-range-percentage", _INT, "100", minimum=10, maximum=1000),
    PropSpec("rate-limit", _INT, "0", minimum=0, maximum=2147483647),
    PropSpec("max-chained-neighbor-updates", _INT, "1000000"),
    PropSpec("max-world-size", _INT, "29999984", minimum=1, maximum=29999984),
    PropSpec("pause-when-empty-seconds", _INT, "60", minimum=-1, maximum=2147483647),
    # ---- booleans ----
    PropSpec("online-mode", _BOOL, "true"),
    PropSpec("pvp", _BOOL, "true"),
    PropSpec("spawn-monsters", _BOOL, "true"),
    PropSpec("generate-structures", _BOOL, "true"),
    PropSpec("allow-nether", _BOOL, "true"),
    PropSpec("allow-flight", _BOOL, "false"),
    PropSpec("hardcore", _BOOL, "false"),
    PropSpec("white-list", _BOOL, "false"),
    PropSpec("enforce-whitelist", _BOOL, "false"),
    PropSpec("enable-command-block", _BOOL, "false"),
    PropSpec("enable-rcon", _BOOL, "false"),
    PropSpec("enable-query", _BOOL, "false"),
    PropSpec("enable-status", _BOOL, "true"),
    PropSpec("enable-jmx-monitoring", _BOOL, "false"),
    PropSpec("broadcast-console-to-ops", _BOOL, "true"),
    PropSpec("broadcast-rcon-to-ops", _BOOL, "true"),
    PropSpec("enforce-secure-profile", _BOOL, "true"),
    PropSpec("force-gamemode", _BOOL, "false"),
    PropSpec("hide-online-players", _BOOL, "false"),
    PropSpec("prevent-proxy-connections", _BOOL, "false"),
    PropSpec("require-resource-pack", _BOOL, "false"),
    PropSpec("sync-chunk-writes", _BOOL, "true"),
    PropSpec("use-native-transport", _BOOL, "true"),
    PropSpec("accepts-transfers", _BOOL, "false"),
    PropSpec("log-ips", _BOOL, "true"),
    # ---- enums ----
    PropSpec("difficulty", _ENUM, "easy", options=("peaceful", "easy", "normal", "hard")),
    PropSpec("gamemode", _ENUM, "survival", options=("survival", "creative", "adventure", "spectator")),
    PropSpec("level-type", _ENUM, "minecraft:normal", options=(
        "minecraft:normal", "minecraft:flat", "minecraft:large_biomes",
        "minecraft:amplified", "minecraft:single_biome_surface")),
    PropSpec("region-file-compression", _ENUM, "deflate", options=("deflate", "none", "lz4")),
    # ---- free-form strings (never crash) ----
    PropSpec("motd", _STR, "A Minecraft Server"),
    PropSpec("level-name", _STR, "world"),
    PropSpec("level-seed", _STR, ""),
    PropSpec("server-ip", _STR, ""),
    PropSpec("resource-pack", _STR, ""),
    PropSpec("resource-pack-sha1", _STR, ""),
    PropSpec("resource-pack-prompt", _STR, ""),
    PropSpec("resource-pack-id", _STR, ""),
    PropSpec("rcon.password", _STR, ""),
    PropSpec("generator-settings", _STR, "{}"),
    PropSpec("bug-report-link", _STR, ""),
    PropSpec("text-filtering-config", _STR, ""),
    PropSpec("text-filtering-version", _INT, "0"),
    PropSpec("initial-enabled-packs", _STR, "vanilla"),
    PropSpec("initial-disabled-packs", _STR, ""),
]}

# Common legacy / alternate spellings we can confidently auto-migrate.
_LEGACY_ENUM_VALUES = {
    "level-type": {
        "default": "minecraft:normal", "normal": "minecraft:normal",
        "flat": "minecraft:flat", "largebiomes": "minecraft:large_biomes",
        "large_biomes": "minecraft:large_biomes", "amplified": "minecraft:amplified",
        "buffet": "minecraft:single_biome_surface",
    },
}

# Numeric gamemode/difficulty (very old files used integers) -> names.
_LEGACY_NUMERIC_ENUM = {
    "gamemode": {"0": "survival", "1": "creative", "2": "adventure", "3": "spectator"},
    "difficulty": {"0": "peaceful", "1": "easy", "2": "normal", "3": "hard"},
}


def known_keys() -> list[str]:
    return list(_SPECS.keys())


# --------------------------------------------------------------------------- #
# Issue model
# --------------------------------------------------------------------------- #

@dataclass
class PropIssue:
    key: str
    kind: str            # empty_numeric|invalid_numeric|out_of_range|invalid_bool|
                         # invalid_enum|unknown_key|duplicate_key
    current: str
    message: str
    severity: str = "warning"          # "error" blocks startup; "warning" is advisory
    suggestion: Optional[str] = None   # best single auto-fix (a value, or a key for unknown_key)
    options: list[str] = field(default_factory=list)  # menu of replacement choices
    default: Optional[str] = None

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass
class RepairResult:
    path: str
    changed: list[tuple[str, str, str]] = field(default_factory=list)  # (key, old, new)
    remaining: list[PropIssue] = field(default_factory=list)
    backup_path: Optional[str] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        if self.error:
            return f"could not repair: {self.error}"
        if not self.changed:
            return "no changes needed"
        return f"fixed {len(self.changed)} setting(s): " + ", ".join(k for k, _, _ in self.changed)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #

class PropertiesDoc:
    """Order/comment-preserving model of a server.properties file."""

    def __init__(self) -> None:
        # Each line is ("comment", raw) or ("kv", key, value) or ("blank", "").
        self._lines: list[tuple] = []

    # ---- construction ----

    @classmethod
    def loads(cls, text: str) -> "PropertiesDoc":
        doc = cls()
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped:
                doc._lines.append(("blank", ""))
            elif stripped.startswith("#") or stripped.startswith("!"):
                doc._lines.append(("comment", raw))
            elif "=" in raw:
                key, _, value = raw.partition("=")
                doc._lines.append(("kv", key.strip(), value.strip()))
            else:
                # A key with no '=' (e.g. "max-players") — treat as blank value.
                doc._lines.append(("kv", stripped, ""))
        return doc

    @classmethod
    def load(cls, path) -> "PropertiesDoc":
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        return cls.loads(text)

    # ---- access ----

    def items(self) -> list[tuple[str, str]]:
        return [(t[1], t[2]) for t in self._lines if t[0] == "kv"]

    def keys(self) -> list[str]:
        return [t[1] for t in self._lines if t[0] == "kv"]

    def get(self, key: str) -> Optional[str]:
        for t in reversed(self._lines):
            if t[0] == "kv" and t[1] == key:
                return t[2]
        return None

    def set(self, key: str, value: str) -> None:
        for i in range(len(self._lines) - 1, -1, -1):
            t = self._lines[i]
            if t[0] == "kv" and t[1] == key:
                self._lines[i] = ("kv", key, value)
                return
        self._lines.append(("kv", key, value))

    def rename_key(self, old: str, new: str) -> None:
        for i, t in enumerate(self._lines):
            if t[0] == "kv" and t[1] == old:
                self._lines[i] = ("kv", new, t[2])
                return

    def to_text(self) -> str:
        out: list[str] = []
        for t in self._lines:
            if t[0] == "kv":
                out.append(f"{t[1]}={t[2]}")
            elif t[0] == "comment":
                out.append(t[1])
            else:
                out.append("")
        return "\n".join(out) + "\n"

    def save(self, path) -> None:
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(self.to_text(), encoding="utf-8")
        tmp.replace(p)

    # ---- validation ----

    def validate(self) -> list[PropIssue]:
        issues: list[PropIssue] = []
        seen: dict[str, int] = {}

        for key, value in self.items():
            seen[key] = seen.get(key, 0) + 1
            spec = _SPECS.get(key)
            if spec is None:
                # Unknown key. Minecraft ignores it (no crash) — but it's often a
                # typo, so suggest the closest real key.
                match = difflib.get_close_matches(key, _SPECS.keys(), n=3, cutoff=0.7)
                issues.append(PropIssue(
                    key=key, kind="unknown_key", current=value, severity="warning",
                    message=(f"“{key}” isn't a recognized server setting"
                             + (f" — did you mean “{match[0]}”?" if match else "")),
                    suggestion=(match[0] if match else None), options=match,
                ))
                continue

            issues.extend(self._check_value(spec, value))

        for key, count in seen.items():
            if count > 1:
                issues.append(PropIssue(
                    key=key, kind="duplicate_key", current=str(count), severity="warning",
                    message=f"“{key}” is listed {count} times — the last value wins",
                ))
        return issues

    def _check_value(self, spec: PropSpec, value: str) -> list[PropIssue]:
        v = value.strip()
        if spec.type == _INT:
            if v == "":
                return [PropIssue(
                    key=spec.key, kind="empty_numeric", current=value, severity="error",
                    message=(f"“{spec.key}” is blank but must be a whole number — "
                             f"this is what crashes the server before it starts"),
                    suggestion=spec.default, default=spec.default,
                    options=self._int_options(spec),
                )]
            iv = _try_int(v)
            if iv is None:
                guessed = _digits_only(v)
                opts = self._int_options(spec)
                if guessed and guessed not in opts:
                    opts = [guessed] + opts
                return [PropIssue(
                    key=spec.key, kind="invalid_numeric", current=value, severity="error",
                    message=(f"“{spec.key}={value}” is not a whole number — "
                             f"the server will crash on start"),
                    suggestion=(guessed or spec.default), default=spec.default, options=opts,
                )]
            # range check (advisory only — vanilla clamps some, rejects others)
            if spec.minimum is not None and iv < spec.minimum:
                return [PropIssue(
                    key=spec.key, kind="out_of_range", current=value, severity="warning",
                    message=f"“{spec.key}={value}” is below the usual minimum ({spec.minimum})",
                    suggestion=str(spec.minimum), default=spec.default,
                    options=[str(spec.minimum), spec.default],
                )]
            if spec.maximum is not None and iv > spec.maximum:
                return [PropIssue(
                    key=spec.key, kind="out_of_range", current=value, severity="warning",
                    message=f"“{spec.key}={value}” is above the usual maximum ({spec.maximum})",
                    suggestion=str(spec.maximum), default=spec.default,
                    options=[str(spec.maximum), spec.default],
                )]
            return []

        if spec.type == _BOOL:
            if v.lower() in ("true", "false"):
                if v != v.lower():
                    return [PropIssue(
                        key=spec.key, kind="invalid_bool", current=value, severity="warning",
                        message=f"“{spec.key}” should be lowercase true/false",
                        suggestion=v.lower(), default=spec.default, options=["true", "false"],
                    )]
                return []
            guess = _bool_guess(v)
            return [PropIssue(
                key=spec.key, kind="invalid_bool", current=value, severity="warning",
                message=(f"“{spec.key}={value}” should be true or false"
                         + (f" — did you mean “{guess}”?" if guess else "")),
                suggestion=(guess or spec.default), default=spec.default, options=["true", "false"],
            )]

        if spec.type == _ENUM:
            if v in spec.options:
                return []
            # legacy numeric (gamemode=1) or alternate spelling (level-type=default)
            legacy = _LEGACY_NUMERIC_ENUM.get(spec.key, {}).get(v)
            if not legacy:
                legacy = _LEGACY_ENUM_VALUES.get(spec.key, {}).get(v.lower())
            match = difflib.get_close_matches(v.lower(), [o.lower() for o in spec.options], n=1, cutoff=0.5)
            best = legacy
            if not best and match:
                # map back to canonical casing
                best = next((o for o in spec.options if o.lower() == match[0]), None)
            return [PropIssue(
                key=spec.key, kind="invalid_enum", current=value, severity="warning",
                message=(f"“{spec.key}={value}” is not a valid choice"
                         + (f" — did you mean “{best}”?" if best else "")),
                suggestion=(best or spec.default), default=spec.default,
                options=list(spec.options),
            )]
        return []

    def _int_options(self, spec: PropSpec) -> list[str]:
        opts = [spec.default]
        # offer the port-style common values where helpful
        return opts

    # ---- repair ----

    def autofix(self, *, only_errors: bool = False,
                fix_unknown_keys: bool = False) -> list[tuple[str, str, str]]:
        """Apply each issue's best suggestion in place.

        only_errors=True restricts to start-blocking problems (the safe default
        for an automatic pre-start repair).  Returns a list of (key, old, new).
        """
        changed: list[tuple[str, str, str]] = []
        for issue in self.validate():
            if only_errors and not issue.is_error:
                continue
            if issue.kind == "duplicate_key":
                continue  # "last wins" already works; leave the file as-is
            if issue.kind == "unknown_key":
                if fix_unknown_keys and issue.suggestion and self.get(issue.suggestion) is None:
                    old = self.get(issue.key) or ""
                    self.rename_key(issue.key, issue.suggestion)
                    changed.append((f"{issue.key}→{issue.suggestion}", old, old))
                continue
            if issue.suggestion is None:
                continue
            old = self.get(issue.key)
            if old == issue.suggestion:
                continue
            self.set(issue.key, issue.suggestion)
            changed.append((issue.key, old if old is not None else "", issue.suggestion))
        return changed


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _try_int(s: str) -> Optional[int]:
    try:
        return int(s.strip())
    except (ValueError, TypeError):
        return None


def _digits_only(s: str) -> str:
    out = "".join(ch for ch in s if ch.isdigit() or ch == "-")
    # keep a single leading minus only
    neg = out.startswith("-")
    out = out.lstrip("-")
    return ("-" if neg else "") + out if out else ""


def _bool_guess(s: str) -> Optional[str]:
    t = s.strip().lower()
    if t in ("1", "yes", "y", "on", "enable", "enabled", "t"):
        return "true"
    if t in ("0", "no", "n", "off", "disable", "disabled", "f"):
        return "false"
    m = difflib.get_close_matches(t, ["true", "false"], n=1, cutoff=0.5)
    return m[0] if m else None


# --------------------------------------------------------------------------- #
# Public convenience API (used by CLI + GUI)
# --------------------------------------------------------------------------- #

def validate_file(path) -> list[PropIssue]:
    """Return all issues for a server.properties file (missing file -> [])."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        return PropertiesDoc.load(p).validate()
    except Exception:
        return []


def has_blocking_errors(path) -> bool:
    return any(i.is_error for i in validate_file(path))


def autorepair_file(path, *, only_errors: bool = True, fix_unknown_keys: bool = False,
                    backup: bool = True) -> RepairResult:
    """Validate, apply safe fixes, and write the file back atomically.

    Never raises; failures are reported in ``RepairResult.error``.
    """
    p = Path(path)
    result = RepairResult(path=str(p))
    if not p.exists():
        result.error = "server.properties does not exist"
        return result
    try:
        doc = PropertiesDoc.load(p)
        changed = doc.autofix(only_errors=only_errors, fix_unknown_keys=fix_unknown_keys)
        if changed:
            if backup:
                try:
                    bak = p.with_suffix(p.suffix + ".bak")
                    shutil.copy2(p, bak)
                    result.backup_path = str(bak)
                except OSError:
                    pass
            doc.save(p)
        result.changed = changed
        result.remaining = doc.validate()
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"{type(exc).__name__}: {exc}"
        return result
