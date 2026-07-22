"""
crucible/data/instance_manager.py

The global registry of ServerInstance objects.
Persists to ~/.config/crucible/instances.json using atomic writes
(write → temp file → rename) so a crash mid-save never corrupts the registry.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .instance_model import ServerInstance

# Paths

CONFIG_DIR     = Path.home() / ".config" / "crucible"
REGISTRY_FILE  = CONFIG_DIR / "instances.json"
REGISTRY_VERSION = 1
_MAX_REGISTRY_BYTES = 16 * 1024 * 1024

_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def validate_delete_target(path: str, *, home: Path | None = None) -> Path:
    """Return a resolved recursive-delete target or raise ValueError."""
    if not path or not path.strip():
        raise ValueError("delete path is empty")
    expanded = Path(path).expanduser()
    if expanded.is_symlink():
        raise ValueError("refusing to recursively delete a symbolic link")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"delete path cannot be resolved: {exc}") from exc
    home = (home or Path.home()).expanduser().resolve()
    if resolved in {Path("/"), home, home.parent} or len(resolved.parts) < 4:
        raise ValueError("refusing to recursively delete an unusually broad path")
    if not resolved.is_dir():
        raise ValueError("delete target is not a directory")
    if resolved.is_mount():
        raise ValueError("refusing to recursively delete a mount point")
    return resolved


def validate_session_name(name: str) -> str:
    name = name.strip()
    if name in {"", ".", ".."} or not _SESSION_RE.fullmatch(name):
        raise ValueError(
            "tmux session names must be 1-80 characters using only letters, "
            "numbers, dot, underscore, or hyphen"
        )
    return name



# Manager

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
        self._known_registry_signature = None
        self.load_error: str | None = None

    # Persistence

    def _registry_signature(self):
        try:
            st = self.registry_file.stat()
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def registry_changed_externally(self) -> bool:
        """True when the registry changed without going through this manager."""
        return self._registry_signature() != self._known_registry_signature

    def load(self) -> None:
        """Load registry from disk.  Missing file → empty list (not an error)."""
        if not self.registry_file.exists():
            self.instances = []
            self._known_registry_signature = None
            self.load_error = None
            return

        try:
            if self.registry_file.stat().st_size > _MAX_REGISTRY_BYTES:
                raise OSError("registry exceeds 16 MiB safety limit")
            raw = self.registry_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("registry root must be a JSON object")
            rows = data.get("instances", [])
            if not isinstance(rows, list):
                raise TypeError("registry instances must be a JSON list")
            loaded: list[ServerInstance] = []
            seen_ids: set[str] = set()
            seen_paths: set[str] = set()
            seen_sessions: set[str] = set()
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise TypeError(f"registry row {index} must be an object")
                instance = ServerInstance.from_dict(row)
                resolved_path = str(Path(instance.path).expanduser().resolve())
                if instance.id in seen_ids:
                    raise TypeError(f"duplicate instance id: {instance.id}")
                if resolved_path in seen_paths:
                    raise TypeError(f"duplicate instance path: {resolved_path}")
                if instance.tmux_session in seen_sessions:
                    raise TypeError(f"duplicate tmux session: {instance.tmux_session}")
                instance.path = resolved_path
                seen_ids.add(instance.id)
                seen_paths.add(resolved_path)
                seen_sessions.add(instance.tmux_session)
                loaded.append(instance)
            self.instances = loaded
            self.load_error = None
            self._known_registry_signature = self._registry_signature()
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # Never silently overwrite a malformed externally edited registry.
            # All writes remain blocked until it is fixed and Crucible restarts.
            self.load_error = str(exc)
            print(f"[crucible] Warning: registry parse error ({exc}) — writes disabled")
            self.instances = []
            self._known_registry_signature = self._registry_signature()

    def _write_instances(self, instances: list[ServerInstance]) -> None:
        """Durably publish a complete registry snapshot without mutating memory."""
        if self.load_error is not None:
            raise RuntimeError(
                "registry writes are disabled because instances.json could not "
                f"be loaded safely: {self.load_error}"
            )
        if self.registry_changed_externally():
            raise RuntimeError(
                "instances.json changed outside Crucible; restart to reconcile "
                "before making registry changes"
            )
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "instances": [i.to_dict() for i in instances],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        tmp = self.registry_file.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                import os
                os.fsync(fh.fileno())
            tmp.replace(self.registry_file)
            self._known_registry_signature = self._registry_signature()
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def save(self) -> None:
        """Atomically and durably write the current registry snapshot."""
        self._write_instances(self.instances)

    # CRUD

    def add_instance(
        self,
        path: str,
        name: str,
        version: str = "",
        tmux_session: str = "",
        pack_source: str = "",
        minecraft_version: str = "",
        loader: str = "",
        loader_version: str = "",
        prism_source: str = "",
    ) -> ServerInstance:
        """
        Register a new server directory.

        Resolves the path to absolute, checks for duplicates, creates
        a ServerInstance, warns about validation problems (but still registers —
        the user may be adding before files are in place).

        Raises ValueError on duplicate path.
        """
        resolved = str(Path(path).expanduser().resolve())
        requested_session = tmux_session.strip()
        if requested_session:
            requested_session = validate_session_name(requested_session)
        else:
            requested_session = ServerInstance._derive_session_name(name)

        for existing in self.instances:
            if existing.path == resolved:
                raise ValueError(
                    f"'{resolved}' is already registered as '{existing.name}'"
                )
            if existing.tmux_session == requested_session:
                raise ValueError(
                    f"tmux session '{requested_session}' is already used by "
                    f"'{existing.name}'"
                )

        inst = ServerInstance(
            path         = resolved,
            name         = name,
            version      = version,
            pack_source  = pack_source,
            minecraft_version = minecraft_version,
            loader       = loader,
            loader_version = loader_version,
            prism_source = prism_source,
            tmux_session = requested_session,
        )

        problems = inst.validate()
        if problems:
            for p in problems:
                print(f"[crucible] Warning: {p}")

        # Commit disk first, then memory. A failed save must not leave a
        # phantom row that exists only until Crucible restarts.
        next_instances = [*self.instances, inst]
        self._write_instances(next_instances)
        self.instances = next_instances
        return inst

    def remove_instance(self, instance_id: str) -> ServerInstance:
        """
        Remove an instance from the registry by ID.
        Does NOT delete any files from disk.
        Raises KeyError if not found.
        """
        for i, inst in enumerate(self.instances):
            if inst.id == instance_id:
                removed = inst
                next_instances = self.instances[:i] + self.instances[i + 1:]
                # Commit disk first. If this fails, both disk and memory retain
                # the instance and callers may safely retry.
                self._write_instances(next_instances)
                self.instances = next_instances
                return removed
        raise KeyError(f"No instance with id: {instance_id!r}")

    def update_instance(self, inst: ServerInstance) -> None:
        """Persist changes made to an already-registered instance object."""
        for i, existing in enumerate(self.instances):
            if existing.id == inst.id:
                next_instances = list(self.instances)
                next_instances[i] = inst
                self._write_instances(next_instances)
                self.instances = next_instances
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
        next_instances = ordered + leftover
        self._write_instances(next_instances)
        self.instances = next_instances

    # Lookups

    def get_by_id(self, instance_id: str) -> ServerInstance | None:
        exact = [i for i in self.instances if i.id == instance_id]
        if exact:
            return exact[0]
        matches = [i for i in self.instances if i.id.startswith(instance_id)]
        return matches[0] if len(matches) == 1 else None

    def get_by_name(self, name: str) -> ServerInstance | None:
        name_lower = name.lower()
        for i in self.instances:
            if i.name.lower() == name_lower:
                return i
        return None

    def get_by_name_or_id(self, key: str) -> ServerInstance | None:
        """Convenience: try name first, then ID prefix."""
        return self.get_by_name(key) or self.get_by_id(key)

    # Discovery

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
            "start.sh",
            "run.sh",
            "launch.sh",
            "run-server.sh",
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
