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

import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..data.instance_model import ServerInstance
from . import netfix


class TmuxManager:
    """
    Manages GTNH server processes via tmux sessions.

    All methods are safe to call regardless of server state — they check
    first and return (bool, message) tuples rather than raising.
    Nothing here blocks the event loop in a way that can't be moved to a
    QThread later; the only blocking call is the graceful-stop poll loop,
    which is fine for Stage 1 CLI use.
    """

    SESSION_PREFIX = ""  # set to a prefix string to filter list_sessions() results

    # Internal subprocess wrapper

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

    # Session name

    def session_name(self, instance: ServerInstance) -> str:
        """Return the tmux session name for this instance."""
        return instance.tmux_session

    def _target(self, instance: ServerInstance) -> str:
        """Force exact tmux target matching (never an accidental prefix match)."""
        return "=" + self.session_name(instance)

    # Status checks

    def is_running(self, instance: ServerInstance) -> bool:
        """Return True if a tmux session exists for this instance."""
        result = self._run(
            ["tmux", "has-session", "-t", self._target(instance)]
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
        Return all active tmux session names.
        If SESSION_PREFIX is set, only sessions starting with that string are returned.
        Used by status_map() so a health pass requires only one tmux process.
        Useful for debugging or CLI summary commands.
        """
        result = self._run(["tmux", "list-sessions", "-F", "#{session_name}"], timeout=2)
        if result.returncode != 0:
            return []
        sessions = [s.strip() for s in result.stdout.splitlines() if s.strip()]
        if self.SESSION_PREFIX:
            sessions = [s for s in sessions if s.startswith(self.SESSION_PREFIX)]
        return sessions

    # Lifecycle

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

        # Force the IPv4 stack so the server can bind its port on machines where
        # IPv6 is disabled/unavailable (fixes Netty bind error -97,
        # "Address family not supported by protocol"). Covers Crucible's own
        # start.sh via CRUCIBLE_JAVA_ARGS...
        java_args = netfix.ensure_ipv4(instance.java_args)
        # ...and Forge/NeoForge run scripts that read user_jvm_args.txt instead.
        try:
            netfix.ensure_user_jvm_args_file(instance.path)
        except OSError:
            pass

        # Record the start script's real exit code. Keep a failed pane alive
        # for one extra second so verification can capture its scrollback. Do
        # not pipe through tee: that would duplicate output for the full server
        # lifetime and could consume unbounded disk space.
        start_env = f"{java_args} -Dcrucible.session={session}".strip()
        marker = self._start_marker_path(session)
        try:
            marker.unlink()
        except OSError:
            pass
        inner = (
            "env CRUCIBLE_JAVA_ARGS=" + shlex.quote(start_env)
            + " bash " + shlex.quote(script.name)
            + "; _crucible_code=$?; printf '%s' \"$_crucible_code\" > "
            + shlex.quote(str(marker))
            + "; sleep 1; exit \"$_crucible_code\""
        )
        cmd = [
            "tmux", "new-session",
            "-d",              # detached — don't steal the terminal
            "-s", session,     # session name
            "-c", instance.path,  # working directory
            # Tag the JVM with a per-instance marker so it's identifiable in
            # Activity Monitor / Mission Control / htop AND matchable by the
            # resource monitor.
            # Run through bash -c so the exit marker can be written.
            "bash -c " + shlex.quote(inner),
        ]

        result = self._run(cmd)
        if result.returncode != 0:
            return False, f"tmux error (exit {result.returncode}): {result.stderr.strip()}"

        ok, why = self._verify_started(instance, session)
        if not ok:
            return False, why

        instance.last_started = datetime.now().isoformat()
        return True, f"Server started in tmux session '{session}'"

    # --- Start verification helpers ---

    def _tmp_dir(self) -> Path:
        return Path(tempfile.gettempdir())

    def _start_marker_path(self, session: str) -> Path:
        return self._tmp_dir() / ("crucible-start-" + session + ".exit")

    def _read_marker(self, marker: Path) -> int:
        try:
            raw = marker.read_text(errors="replace").strip()
        except OSError:
            return -1
        try:
            return int(raw)
        except ValueError:
            return -1

    def _read_capture(self, session: str, max_lines: int = 40, max_chars: int = 4000) -> str:
        result = self._run(
            ["tmux", "capture-pane", "-p", "-t", "=" + session, "-S", f"-{max_lines}"],
            timeout=2,
        )
        if result.returncode != 0:
            return ""
        out = result.stdout.strip()
        return out[-max_chars:] if len(out) > max_chars else out

    def _verify_started(
        self,
        instance: ServerInstance,
        session: str,
        grace_s: float = 6.0,
        interval_s: float = 0.25,
    ) -> tuple[bool, str]:
        """Watch a freshly-launched session briefly.

        (True, "") if the server is still running after the grace window (the
        healthy case: no exit marker appears because the process keeps running).
        (False, reason) with captured output if the start script exited or the
        session vanished within the window.
        """
        marker = self._start_marker_path(session)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            time.sleep(interval_s)
            if marker.exists():
                code = self._read_marker(marker)
                tail = self._read_capture(session)
                hint = ""
                if code == 2:
                    hint = (
                        "\n\nThis looks like Crucible's placeholder start.sh: the "
                        "folder has no dedicated server jar/loader yet. Install the "
                        "server (Setup tab -> Install server) or import a fully "
                        "installed instance, then try again."
                    )
                msg = f"Server process exited immediately (exit code {code})." + hint
                if tail:
                    msg += "\n\nLast output:\n" + tail
                return False, msg
            if not self.is_running(instance):
                tail = self._read_capture(session)
                msg = "The tmux session ended before the server finished starting."
                if tail:
                    msg += "\n\nLast output:\n" + tail
                return False, msg
        return True, ""

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

        # Timed out -- fall through to force kill
        ok, msg = self._force_kill(instance)
        if ok:
            return True, f"Server did not stop within {timeout_s}s — force-killed"
        return False, f"Force kill failed after timeout: {msg}"

    def _force_kill(self, instance: ServerInstance) -> tuple[bool, str]:
        """Kill the tmux session immediately."""
        session = self.session_name(instance)
        result  = self._run(["tmux", "kill-session", "-t", "=" + session])
        if result.returncode == 0:
            return True, f"Session '{session}' force-killed"
        return False, f"kill-session failed: {result.stderr.strip()}"

    # Console interaction

    def send_command(self, instance: ServerInstance, command: str) -> bool:
        """
        Send a command string to the server console via tmux send-keys.

        Works for any Minecraft/Forge console command: stop, say, op, tps, etc.
        Returns True on success.
        """
        session = self.session_name(instance)
        literal = self._run([
            "tmux", "send-keys", "-t", "=" + session, "-l", "--", command,
        ])
        if literal.returncode != 0:
            return False
        enter = self._run([
            "tmux", "send-keys", "-t", "=" + session, "Enter",
        ])
        return enter.returncode == 0

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

        session = self.session_name(instance)
        target = "=" + session
        attach_cmd = f"tmux attach -t {shlex.quote(target)}"

        # Pass executable/arguments as separate argv entries. Konsole/xterm do
        # not execute a single string containing spaces as a shell command.
        terminal_cmds: dict[str, list[str]] = {
            "konsole":        ["konsole", "-e", "tmux", "attach", "-t", target],
            "kitty":          ["kitty", "--", "tmux", "attach", "-t", target],
            "alacritty":      ["alacritty", "-e", "tmux", "attach", "-t", target],
            "gnome-terminal": ["gnome-terminal", "--", "tmux", "attach", "-t", target],
            "xterm":          ["xterm", "-e", "tmux", "attach", "-t", target],
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

    # Bulk queries (for the future sidebar health-check timer)

    def status_map(
        self, instances: list[ServerInstance]
    ) -> dict[str, Literal["running", "stopped"]]:
        """Return all statuses from one tmux query using exact session names."""
        if not self.tmux_available():
            return {i.id: "stopped" for i in instances}
        sessions = set(self.list_sessions())
        return {
            i.id: ("running" if self.session_name(i) in sessions else "stopped")
            for i in instances
        }
