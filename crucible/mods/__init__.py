"""Mod tooling public exports, loaded only when requested."""

__all__ = ["ModManager", "ModEntry"]


def __getattr__(name):
    if name in __all__:
        from .mod_manager import ModEntry, ModManager
        value = {"ModManager": ModManager, "ModEntry": ModEntry}[name]
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
