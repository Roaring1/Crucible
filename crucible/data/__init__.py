"""Data-layer public exports, loaded only when requested."""

__all__ = ["ServerInstance", "InstanceManager"]


def __getattr__(name):
    if name == "ServerInstance":
        from .instance_model import ServerInstance as value
    elif name == "InstanceManager":
        from .instance_manager import InstanceManager as value
    elif name == "BackupManager":
        from .backup_manager import BackupManager as value
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value



def __dir__():
    return sorted(set(globals()) | set(__all__))
