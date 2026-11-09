"""Client-instance exporters (Prism / Modrinth / CurseForge)."""

__all__ = ["export", "ExportResult"]


def __getattr__(name):
    if name in __all__:
        from .client_export import ExportResult, export
        value = {"export": export, "ExportResult": ExportResult}[name]
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
