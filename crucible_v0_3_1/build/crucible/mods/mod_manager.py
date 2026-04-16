"""
crucible/mods/mod_manager.py

Manages the mods/ directory of a GTNH server.

Enabled mods:  *.jar
Disabled mods: *.jar.disabled

Jar inspection is done by opening the zip and reading:
  - META-INF/MANIFEST.MF  (implementation title/version)
  - mcmod.info            (Forge mod metadata, JSON)

All operations are synchronous and fast (directory scans only,
zip inspection only on request). The UI calls list_mods() on demand;
jar inspection is deferred until the user clicks a mod.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..data.instance_model import ServerInstance


@dataclass
class ModEntry:
    filename: str          # bare filename, e.g. "gregtech.jar"
    path: Path             # full path
    enabled: bool
    size_bytes: int
    name: str        = ""  # friendly name from mcmod.info or MANIFEST.MF
    mod_id: str      = ""
    version: str     = ""
    description: str = ""
    url: str         = ""
    bundled: bool    = False  # True = nested inside a subdir, not manageable

    @property
    def display_name(self) -> str:
        return self.name or self._clean_filename()

    def _clean_filename(self) -> str:
        stem = self.path.stem
        if stem.endswith(".jar"):   # handle .jar.disabled → strip extra
            stem = stem[:-4]
        return stem

    @property
    def size_mb(self) -> str:
        mb = self.size_bytes / (1024 * 1024)
        return f"{mb:.1f} MB"


class ModManager:
    """Manages the mods/ directory for one ServerInstance."""

    def __init__(self, instance: ServerInstance) -> None:
        self.instance = instance

    @property
    def _mods_dir(self) -> Path:
        return Path(self.instance.path) / "mods"

    # ── Listing ───────────────────────────────────────────────────────────────

    def list_mods(self) -> list[ModEntry]:
        """
        Return all mods (enabled + disabled + bundled), sorted:
        enabled alphabetically first, then disabled alphabetically,
        then bundled (nested-subdir) jars alphabetically last.

        Bundled entries have mod.bundled=True and mod.enabled=False.
        They are displayed in the mods tab as informational rows but
        all action buttons (enable/disable/delete) are suppressed for them.
        """
        if not self._mods_dir.exists():
            return []

        entries: list[ModEntry] = []
        for p in self._mods_dir.iterdir():
            if p.is_dir():
                # Surface nested jars so they're visible, even if unmanageable
                for nested in p.rglob("*.jar"):
                    entries.append(ModEntry(
                        filename   = nested.name,
                        path       = nested,
                        enabled    = False,
                        bundled    = True,
                        size_bytes = nested.stat().st_size,
                    ))
            elif p.suffix == ".jar" and not p.name.endswith(".jar.disabled"):
                entries.append(ModEntry(
                    filename   = p.name,
                    path       = p,
                    enabled    = True,
                    size_bytes = p.stat().st_size,
                ))
            elif p.name.endswith(".jar.disabled"):
                entries.append(ModEntry(
                    filename   = p.name,
                    path       = p,
                    enabled    = False,
                    size_bytes = p.stat().st_size,
                ))

        entries.sort(key=lambda m: (
            2 if m.bundled else (0 if m.enabled else 1),
            m.filename.lower()
        ))
        return entries

    def count_enabled(self) -> int:
        return sum(1 for m in self.list_mods() if m.enabled)

    # ── Enable / Disable ──────────────────────────────────────────────────────

    def enable(self, mod: ModEntry) -> ModEntry:
        """Remove .disabled suffix, return updated entry."""
        if mod.enabled:
            return mod
        new_path = mod.path.with_suffix("")   # drop .disabled
        mod.path.rename(new_path)
        return ModEntry(
            filename   = new_path.name,
            path       = new_path,
            enabled    = True,
            size_bytes = new_path.stat().st_size,
        )

    def disable(self, mod: ModEntry) -> ModEntry:
        """Add .disabled suffix, return updated entry."""
        if not mod.enabled:
            return mod
        new_path = mod.path.with_name(mod.path.name + ".disabled")
        mod.path.rename(new_path)
        return ModEntry(
            filename   = new_path.name,
            path       = new_path,
            enabled    = False,
            size_bytes = new_path.stat().st_size,
        )

    def delete(self, mod: ModEntry) -> None:
        """Permanently delete a mod file."""
        mod.path.unlink(missing_ok=True)

    def add_from_file(self, src: Path) -> ModEntry:
        """Copy a .jar file into the mods/ directory."""
        self._mods_dir.mkdir(exist_ok=True)
        dst = self._mods_dir / src.name
        # Avoid silent overwrite
        if dst.exists():
            stem = src.stem
            suffix = src.suffix
            i = 1
            while dst.exists():
                dst = self._mods_dir / f"{stem}_{i}{suffix}"
                i += 1
        import shutil
        shutil.copy2(src, dst)
        return ModEntry(
            filename   = dst.name,
            path       = dst,
            enabled    = True,
            size_bytes = dst.stat().st_size,
        )

    # ── Jar inspection ────────────────────────────────────────────────────────

    def inspect_jar(self, mod: ModEntry) -> None:
        """
        Fill in mod.name, mod.version, mod.description, mod.mod_id
        by opening the jar (zip) and reading Forge metadata.
        Modifies mod in-place.  Safe to call on any thread.
        """
        try:
            with zipfile.ZipFile(mod.path, "r") as zf:
                names = zf.namelist()

                # ── mcmod.info (Forge 1.7.10) ──
                if "mcmod.info" in names:
                    with zf.open("mcmod.info") as f:
                        raw = f.read().decode("utf-8", errors="replace")
                    self._parse_mcmod(mod, raw)
                    return

                # ── META-INF/MANIFEST.MF ──
                if "META-INF/MANIFEST.MF" in names:
                    with zf.open("META-INF/MANIFEST.MF") as f:
                        mf = f.read().decode("utf-8", errors="replace")
                    self._parse_manifest(mod, mf)
        except (zipfile.BadZipFile, KeyError, OSError):
            pass  # corrupt/unusual jar — leave fields empty

    def _parse_mcmod(self, mod: ModEntry, raw: str) -> None:
        try:
            data = json.loads(raw)
            # mcmod.info can be a list or a dict with "modList" key
            if isinstance(data, dict):
                data = data.get("modList", [data])
            if isinstance(data, list) and data:
                entry = data[0]
                mod.mod_id      = entry.get("modid", "")
                mod.name        = entry.get("name", "")
                mod.version     = entry.get("version", "")
                mod.description = entry.get("description", "")
                mod.url         = entry.get("url", "")
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    def _parse_manifest(self, mod: ModEntry, mf: str) -> None:
        for line in mf.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "Implementation-Title":
                mod.name = v
            elif k == "Implementation-Version":
                mod.version = v
