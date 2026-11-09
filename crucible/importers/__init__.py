"""Import helpers for external Minecraft launcher/modpack formats."""

__all__ = [
    "PrismImportPlan", "PrismPackInfo", "detect_prism_source",
    "import_prism_source", "scan_prism_instances",
]


def __getattr__(name):
    if name in __all__:
        from . import prism
        value = getattr(prism, name)
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value



def __dir__():
    return sorted(set(globals()) | set(__all__))
