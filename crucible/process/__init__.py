"""Process-layer public exports, loaded only when requested."""

__all__ = ["TmuxManager"]


def __getattr__(name):
    if name == "TmuxManager":
        from .tmux_manager import TmuxManager as value
    elif name == "LogWatcher":
        from .log_watcher import LogWatcher as value
    elif name == "Watchdog":
        from .watchdog import Watchdog as value
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value



def __dir__():
    return sorted(set(globals()) | set(__all__))
