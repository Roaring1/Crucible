"""
crucible/process/tmux_manager.py

All server start/stop/status/console operations go through tmux.
The server process lives in tmux independently of Crucible —
closing the manager never stops a running server.

tmux command reference (matching the user's current manual workflow):

  Start:   tmux new-session -d -s {session} -c {path} "bash ServerStart.sh"
  Stop:    tmux send-keys -t {session} "stop" Enter  (then poll until gone)
  Attach:  tmux attach -t {session}          (opens in a new terminal window)
  Check:   tmux has-session -t {session}     (exit 0 = running)
  Kill:    tmux kill-session -t {session}    (force, no save)
"""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..data.instance_model import ServerInstance


class TmuxManager:
    """
    Manages GTNH server processes via tmux sessions.

    All methods are safe to call regardless of server state — they check
    first and return (bool, message) tuples rather than raising.
    Nothing here blocks the event loop in a way that can't be moved to a
    QThread later; the only blocking call is the graceful-stop poll loop,
    which is fine for Stage 1 CLI use.
    """

    SESSION_PREFIX = "gtnh-"

    # ── Internal subprocess wrapper ───────────────────────────────────────────

    def _run(
        self,
        cmd: list[str],
        capture: bool = True,
        timeout: int = 10,
    ) -> subprocess.CompletedProcess:
        """
        Run a tmux command.  Never raises — returncode is always checked
        by the caller.
        """
        try:
            return subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="timeout")
        except FileNotFoundError:
            # tmux not installed
            return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="tmux not found")

    # ── Session name ──────────────────────────────────────────────────────────

    def session_name(self, instance: ServerInstance) -> str:
        """Return the tmux session name for this instance."""
        return instance.tmux_session

    # ── Status checks ─────────────────────────────────────────────────────────

    def is_running(self, instance: ServerInstance) -> bool:
        """Return True if a tmux session exists for this instance."""
        result = self._run(
            ["tmux", "has-session", "-t", self.session_name(instance)]
        )
        return result.returncode == 0

    def get_status(
        self, instance: ServerInstance
    ) -> Literal["running", "stopped", "tmux_missing"]:
        """
        Return a status string for the instance.
        "tmux_missing" means tmux itself isn't installed.
        """
        if not shutil.which("tmux"):
            return "tmux_missing"
        return "running" if self.is_running(instance) else "stopped"

    def tmux_available(self) -> bool:
        return shutil.which("tmux") is not None

    def list_sessions(self) -> list[str]:
        """
        Return all active tmux session names that have our prefix.
        Returns empty list if tmux isn't running or no sessions exist.
        """
        result = self._run(["tmux", "list-sessions", "-F", "#{session_name}"])
        if result.returncode != 0:
            return []
        return [
            s.strip()
            for s in result.stdout.splitlines()
            if s.strip().startswith(self.SESSION_PREFIX)
        ]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, instance: ServerInstance) -> tuple[bool, str]:
        """
        Start the server in a new detached tmux session.

        Returns (True, success_msg) or (False, reason).
        On success, instance.last_started is updated (caller must save registry).
        """
        if not self.tmux_available():
            return False, "tmux is not installed — run: sudo dnf install tmux"

        if self.is_running(instance):
            return (
                False,
                f"Session '{self.session_name(instance)}' is already running.\n"
                f"  Attach with: tmux attach -t {self.session_name(instance)}",
            )

        script = instance.get_startscript()
        if script is None:
            return (
                False,
                f"No start script found in {instance.path}.\n"
                f"  Expected one of: ServerStart.sh, startserver.sh, …",
            )

        session = self.session_name(instance)
        cmd = [
            "tmux", "new-session",
            "-d",              # detached — don't steal the terminal
            "-s", session,     # session name
            "-c", instance.path,  # working directory
            f"bash {script.name}",  # command; script.name is the bare filename
        ]

        result = self._run(cmd)
        if result.returncode != 0:
            return False, f"tmux error (exit {result.returncode}): {result.stderr.strip()}"

        instance.last_started = datetime.now().isoformat()
        return True, f"Server started in tmux session '{session}'"

    def stop(
        self,
        instance: ServerInstance,
        graceful: bool = True,
        timeout_s: int = 90,
        poll_interval_s: int = 2,
    ) -> tuple[bool, str]:
        """
        Stop the server.

        graceful=True  → sends 'stop' to the console, waits up to timeout_s
                         seconds for the session to disappear on its own.
                         Falls through to force-kill if the server hangs.
        graceful=False → immediately kills the tmux session (no world save).
        """
        if not self.is_running(instance):
            return False, "Server is not running"

        if not graceful:
            return self._force_kill(instance)

        # Send 'stop' via the console
        if not self.send_command(instance, "stop"):
            return False, "Failed to send 'stop' command — session may have vanished"

        # Poll until the session disappears or we time out
        elapsed = 0
        while elapsed < timeout_s:
            time.sleep(poll_interval_s)
            elapsed += poll_interval_s
            if not self.is_running(instance):
                return True, f"Server stopped gracefully after {elapsed}s"

        # Timed out — fall through to force kill
        ok, msg = self._force_kill(instance)
        if ok:
            return True, f"Server did not stop within {timeout_s}s — force-killed"
        return False, f"Force kill failed after timeout: {msg}"

    def _force_kill(self, instance: ServerInstance) -> tuple[bool, str]:
        """Kill the tmux session immediately."""
        session = self.session_name(instance)
        result  = self._run(["tmux", "kill-session", "-t", session])
        if result.returncode == 0:
            return True, f"Session '{session}' force-killed"
        return False, f"kill-session failed: {result.stderr.strip()}"

    # ── Console interaction ───────────────────────────────────────────────────

    def send_command(self, instance: ServerInstance, command: str) -> bool:
        """
        Send a command string to the server console via tmux send-keys.

        Works for any Minecraft/Forge console command: stop, say, op, tps, etc.
        Returns True on success.
        """
        session = self.session_name(instance)
        result  = self._run([
            "tmux", "send-keys",
            "-t", session,
            command,
            "Enter",
        ])
        return result.returncode == 0

    def attach(
        self,
        instance: ServerInstance,
        terminal: str = "auto",
    ) -> tuple[bool, str]:
        """
        Open the server console in a new terminal window.

        Uses Popen (not run) so the call returns immediately — the terminal
        window lives independently of Crucible.

        terminal: "auto" | "konsole" | "gnome-terminal" | "kitty" | "alacritty" | "xterm"

        Auto-detection order: konsole (KDE/Nobara default) → kitty → alacritty
        → gnome-terminal → xterm.
        """
        if not self.is_running(instance):
            return False, "Server is not running — nothing to attach to"

        session    = self.session_name(instance)
        attach_cmd = f"tmux attach -t {session}"

        # Terminal → [command, ...] mapping
        # Each command opens a new window running attach_cmd
        terminal_cmds: dict[str, list[str]] = {
            "konsole":        ["konsole", "-e", attach_cmd],
            "kitty":          ["kitty", "--", "bash", "-c", attach_cmd],
            "alacritty":      ["alacritty", "-e", "bash", "-c", attach_cmd],
            "gnome-terminal": ["gnome-terminal", "--", "bash", "-c", attach_cmd],
            "xterm":          ["xterm", "-e", attach_cmd],
        }

        if terminal == "auto":
            order = ["konsole", "kitty", "alacritty", "gnome-terminal", "xterm"]
            for name in order:
                if shutil.which(name):
                    terminal = name
                    break
            else:
                return (
                    False,
                    f"No supported terminal found.\n"
                    f"  Run manually: {attach_cmd}",
                )

        cmd = terminal_cmds.get(terminal)
        if cmd is None:
            return False, f"Unknown terminal: {terminal!r}"

        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True, f"Opened '{session}' in {terminal}"
        except FileNotFoundError:
            return (
                False,
                f"{terminal!r} not found.\n"
                f"  Run manually: {attach_cmd}",
            )

    # ── Bulk queries (for the future sidebar health-check timer) ──────────────

    def status_map(
        self, instances: list[ServerInstance]
    ) -> dict[str, Literal["running", "stopped"]]:
        """
        Return {instance.id: status} for all instances in one pass.
        More efficient than calling is_running() N times separately because
        we call list-sessions once and do set membership for the rest.
        """
        if not self.tmux_available():
            return {i.id: "stopped" for i in instances}

        active = set(self.list_sessions())
        return {
            i.id: ("running" if i.tmux_session in active else "stopped")
            for i in instances
        }
