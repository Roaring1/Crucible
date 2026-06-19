from .tmux_manager import TmuxManager

__all__ = ["TmuxManager"]


def __getattr__(name):
    if name == "LogWatcher":
        from .log_watcher import LogWatcher
        return LogWatcher
    if name == "Watchdog":
        from .watchdog import Watchdog
        return Watchdog
    raise AttributeError(name)
