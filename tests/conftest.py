"""Apply process-wide safety defaults to pytest and all child processes."""

from tmux_isolation import isolate_tmux


isolate_tmux()
