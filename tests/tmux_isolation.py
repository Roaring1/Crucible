"""Give test processes a private tmux socket and clean up only that server."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile


_TEST_TMUX_TMPDIR: str | None = None


def isolate_tmux() -> None:
    """Redirect tmux in this process tree away from every user socket."""
    global _TEST_TMUX_TMPDIR
    if _TEST_TMUX_TMPDIR is not None:
        return

    _TEST_TMUX_TMPDIR = tempfile.mkdtemp(prefix="crucible-test-tmux-")
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    os.environ["TMUX_TMPDIR"] = _TEST_TMUX_TMPDIR

    def cleanup() -> None:
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        env["TMUX_TMPDIR"] = _TEST_TMUX_TMPDIR
        try:
            if shutil.which("tmux"):
                subprocess.run(
                    ["tmux", "kill-server"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                )
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            shutil.rmtree(_TEST_TMUX_TMPDIR, ignore_errors=True)

    atexit.register(cleanup)
