"""
crucible/data/instance_manager.py

The global registry of ServerInstance objects.
Persists to ~/.config/crucible/instances.json using atomic writes
(write → temp file → rename) so a crash mid-save never corrupts the registry.
"""

from __future__ import annotations

import json
from pathlib import Path

from .instance_model import ServerInstance

# ── Paths ──────────────────────────────────────────────────────────────────────

CONFIG_DIR     = Path.home() / ".config" / "crucible"
REGISTRY_FILE  = CONFIG_DIR / "instances.json"
REGISTRY_VERSION = 1


# ── Manager ────────────────────────────────────────────────────────────────────

class InstanceManager:
    """
    Loads, saves, and provides access to the instance registry.

    Usage:
        mgr = InstanceManager()
        mgr.load()
        inst = mgr.add_instance("/home/roaring/GTNH-Server-TEST", "Test Server")
        mgr.save()
    """

    def __init__(self, config_dir: Path = CONFIG_DIR) -> None:
        self.config_dir    = config_dir
        self.registry_file = config_dir / "instances.json"
        self.instances: list[ServerInstance] = []

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load registry from disk.  Missing file → empty list (not an error)."""
        if not self.registry_file.exists():
            self.instances = []
            return

        try:
            raw  = self.registry_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            self.instances = [
                ServerInstance.from_dict(d)
                for d in data.get("instances", [])
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # Corrupted file — surface the error but don't crash the app
            print(f"[crucible] Warning: registry parse error ({exc}) — starting empty")
            self.instances = []

    def save(self) -> None:
        """Atomically write registry to disk."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version":   REGISTRY_VERSION,
            "instances": [i.to_dict() for i in self.instances],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        # Atomic: write to .tmp then rename (rename is atomic on POSIX)
        tmp = self.registry_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.registry_file)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add_instance(
        self,
        path: str,
        name: str,
        version: str = "2.8.4",
        tmux_session: str = "",
    ) -> ServerInstance:
        """
        Register a new server directory.

        Resolves the path to absolute, checks for duplicates, creates
        a ServerInstance, warns about validation problems (but still registers —
        the user may be adding before files are in place).

        Raises ValueError on duplicate path.
        """
        resolved = str(Path(path).expanduser().resolve())

        for existing in self.instances:
            if existing.path == resolved:
                raise ValueError(
                    f"'{resolved}' is already registered as '{existing.name}'"
                )

        inst = ServerInstance(
            path         = resolved,
            name         = name,
            version      = version,
            tmux_session = tmux_session,  # empty → auto-derived in __post_init__
        )

        problems = inst.validate()
        if problems:
            for p in problems:
                print(f"[crucible] Warning: {p}")

        self.instances.append(inst)
        self.save()
        return inst

    def remove_instance(self, instance_id: str) -> ServerInstance:
        """
        Remove an instance from the registry by ID.
        Does NOT delete any files from disk.
        Raises KeyError if not found.
        """
        for i, inst in enumerate(self.instances):
            if inst.id == instance_id:
                removed = self.instances.pop(i)
                self.save()
                return removed
        raise KeyError(f"No instance with id: {instance_id!r}")

    def update_instance(self, inst: ServerInstance) -> None:
        """Persist changes made to an already-registered instance object."""
        for i, existing in enumerate(self.instances):
            if existing.id == inst.id:
                self.instances[i] = inst
                self.save()
                return
        raise KeyError(f"Instance {inst.id!r} not in registry")

    def reorder(self, new_order: list[str]) -> None:
        """
        Reorder instances to match the given list of IDs.
        IDs not present in new_order are dropped to the end.
        """
        id_map   = {i.id: i for i in self.instances}
        ordered  = [id_map[iid] for iid in new_order if iid in id_map]
        leftover = [i for i in self.instances if i.id not in set(new_order)]
        self.instances = ordered + leftover
        self.save()

    # ── Lookups ───────────────────────────────────────────────────────────────

    def get_by_id(self, instance_id: str) -> ServerInstance | None:
        for i in self.instances:
            if i.id == instance_id or i.id.startswith(instance_id):
                return i
        return None

    def get_by_name(self, name: str) -> ServerInstance | None:
        name_lower = name.lower()
        for i in self.instances:
            if i.name.lower() == name_lower:
                return i
        return None

    def get_by_name_or_id(self, key: str) -> ServerInstance | None:
        """Convenience: try name first, then ID prefix."""
        return self.get_by_name(key) or self.get_by_id(key)

    # ── Discovery ─────────────────────────────────────────────────────────────

    def find_server_dirs(
        self,
        search_path: Path,
        max_depth: int = 3,
    ) -> list[Path]:
        """
        Walk search_path looking for directories that contain a GTNH start script.
        Stops recursion at max_depth to avoid traversing enormous trees.
        """
        found: list[Path] = []
        search_path = Path(search_path).expanduser().resolve()

        start_names = {
            "ServerStart.sh",
            "startserver.sh",
            "startserver-java9.sh",
            "startserver-java17.sh",
        }

        def _walk(p: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                children = list(p.iterdir())
            except PermissionError:
                return

            for child in children:
                if not child.is_dir():
                    continue
                # Check if any start script lives directly inside this dir
                has_start = any((child / s).exists() for s in start_names)
                if not has_start:
                    # Glob fallback for unusual names
                    has_start = bool(list(child.glob("start*.sh")))

                if has_start:
                    found.append(child)
                else:
                    _walk(child, depth + 1)

        _walk(search_path, 0)
        return found
