from .instance_model import ServerInstance
from .instance_manager import InstanceManager

__all__ = ["ServerInstance", "InstanceManager"]


def __getattr__(name):
    if name == "BackupManager":
        from .backup_manager import BackupManager
        return BackupManager
    raise AttributeError(name)
