"""Workspace tabs, imported on demand.

Importing this package must not load every page before the first window is
shown. The public class exports remain available through module-level lazy
attribute resolution.
"""

from importlib import import_module

_EXPORTS = {
    "ConsoleTab": ("console_tab", "ConsoleTab"),
    "ConfigTab": ("config_tab", "ConfigTab"),
    "ModsTab": ("mods_tab", "ModsTab"),
    "NotesTab": ("notes_tab", "NotesTab"),
    "InfoTab": ("info_tab", "InfoTab"),
    "BackupTab": ("backup_tab", "BackupTab"),
    "WorldTab": ("world_tab", "WorldTab"),
    "PlayersTab": ("players_tab", "PlayersTab"),
    "SetupTab": ("setup_tab", "SetupTab"),
    "SystemTab": ("system_tab", "SystemTab"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    """Resolve a tab class only when a caller requests that export."""
    try:
        module_name, class_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, class_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
