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
        """Exact target-session syntax for session-level tmux commands."""
        return "=" + self.session_name(instance)

    def _pane_target(self, instance: ServerInstance) -> str:
        """Exact target-pane syntax for commands sent to the server console.

        tmux accepts ``=name`` for target-session commands such as has-session,
        attach-session, and kill-session. Pane commands such as send-keys and
        capture-pane require a colon: ``=name:``. Without it tmux reports
        ``can't find pane: =name`` even though the session exists.
        """
        return "=" + self.session_name(instance) + ":"

    # Status checks

    def probe_running(self, instance: ServerInstance) -> bool | None:
        """Return True/False for a confirmed probe, or None on uncertainty."""
        if not self.tmux_available():
            return False
        result = self._run(
            ["tmux", "has-session", "-t", self._target(instance)], timeout=2
        )
        if result.returncode == 0:
            return True
        if (result.stderr or "").strip().lower() == "timeout":
            return None
        if result.returncode == 1:
            return False
        return None

    def is_running(self, instance: ServerInstance) -> bool:
        """Compatibility boolean; uncertain probes are never treated as running."""
        return self.probe_running(instance) is True

    @staticmethod
    def _unmanaged_pids(instance: ServerInstance) -> list[int]:
        """Best-effort detection of a server JVM not reachable via its tmux session."""
        try:
            from .resource_monitor import ResourceSampler
            return ResourceSampler().find_instance_pids(instance)
        except Exception:
            return []

    def safe_to_remove(self, instance: ServerInstance) -> tuple[bool, str]:
        """Fail-closed live probe before unregistering or deleting an instance.

        Sidebar health is intentionally not trusted here because it can be up to
        one polling interval stale. A timeout or unexpected tmux error blocks
        removal rather than guessing that the server is stopped.
        """
        if not self.tmux_available():
            return True, "tmux is not installed; no managed session can be running"
        result = self._run(
            ["tmux", "has-session", "-t", self._target(instance)], timeout=2
        )
        if result.returncode == 0:
            return False, "the tmux session is still running"
        detail = (result.stderr or "").strip().lower()
        if detail == "timeout":
            return False, "the live tmux safety check timed out"
        # tmux has-session uses exit 1 when the exact session does not exist.
        if result.returncode == 1:
            pids = self._unmanaged_pids(instance)
            if pids:
                return False, (
                    "a server-like Java process is still running outside the "
                    f"configured tmux session (PID(s): {', '.join(map(str, pids))})"
                )
            return True, "no live tmux session or matching server process exists"
        return False, detail or f"tmux safety check failed with exit {result.returncode}"

    def get_status(
        self, instance: ServerInstance
    ) -> Literal["running", "unmanaged", "stopped", "missing", "unknown", "tmux_missing"]:
        """
        Return a status string for the instance.
        "tmux_missing" means tmux itself isn't installed.
        """
        if not shutil.which("tmux"):
            return "tmux_missing"
        # A live tmux session remains controllable even if its original server
        # directory was externally deleted. Prefer live process truth first.
        running = self.probe_running(instance)
        if running is True:
            return "running"
        if running is None:
            return "unknown"
        if self._unmanaged_pids(instance):
            return "unmanaged"
        if not Path(instance.path).is_dir():
            return "missing"
        return "stopped"

    def tmux_available(self) -> bool:
        return shutil.which("tmux") is not None

    def list_sessions(self) -> list[str] | None:
        """
        Return all active tmux session names.
        If SESSION_PREFIX is set, only sessions starting with that string are returned.
        Used by status_map() so a health pass requires only one tmux process.
        Useful for debugging or CLI summary commands.
        """
        result = self._run(["tmux", "list-sessions", "-F", "#{session_name}"], timeout=2)
        if result.returncode != 0:
            detail = (result.stderr or "").strip().lower()
            if "no server running" in detail or "failed to connect to server" in detail:
                return []
            return None
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
            ["tmux", "capture-pane", "-p", "-t", "=" + session + ":", "-S", f"-{max_lines}"],
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
            running = self.probe_running(instance)
            if running is False:
                tail = self._read_capture(session)
                msg = "The tmux session ended before the server finished starting."
                if tail:
                    msg += "\n\nLast output:\n" + tail
                return False, msg
            # None means the probe was uncertain; do not turn a transient tmux
            # query failure into a false immediate-start failure.
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
        running = self.probe_running(instance)
        if running is None:
            return False, "Could not verify the tmux session state; no command was sent"
        if not running:
            pids = self._unmanaged_pids(instance)
            if pids:
                return False, (
                    "Server process exists outside the configured tmux session; "
                    "Crucible cannot safely send console commands to it"
                )
            return False, "Server is not running in its configured tmux session"

        if not graceful:
            return self._force_kill(instance)

        # Send 'stop' via the console
        sent, send_detail = self.send_command_result(instance, "stop")
        if not sent:
            return False, f"Failed to send 'stop': {send_detail}"

        # Poll until the session is *confirmed* gone. Unknown probes are not
        # offline and never authorize a force-kill.
        elapsed = 0
        saw_unknown = False
        while elapsed < timeout_s:
            time.sleep(poll_interval_s)
            elapsed += poll_interval_s
            running = self.probe_running(instance)
            if running is False:
                return True, f"Server stopped gracefully after {elapsed}s"
            if running is None:
                saw_unknown = True

        if saw_unknown and self.probe_running(instance) is None:
            return False, (
                f"Could not verify shutdown within {timeout_s}s because tmux "
                "status checks were unavailable. No force-kill was attempted."
            )
        # Graceful mode must never silently become destructive. The GUI may now
        # ask the user explicitly whether to force-kill without a world save.
        return False, (
            f"Server did not stop within {timeout_s}s. It was not force-killed."
        )

    def _force_kill(self, instance: ServerInstance) -> tuple[bool, str]:
        """Kill the tmux session immediately."""
        session = self.session_name(instance)
        result  = self._run(["tmux", "kill-session", "-t", "=" + session])
        if result.returncode == 0:
            return True, f"Session '{session}' force-killed"
        return False, f"kill-session failed: {result.stderr.strip()}"

    # Console interaction

    def send_command_result(
        self, instance: ServerInstance, command: str
    ) -> tuple[bool, str]:
        """Send literal console input and return actionable failure detail."""
        target = self._pane_target(instance)
        literal = self._run([
            "tmux", "send-keys", "-t", target, "-l", "--", command,
        ], timeout=2)
        if literal.returncode != 0:
            detail = (literal.stderr or "").strip() or f"exit {literal.returncode}"
            return False, f"tmux could not target the server console ({detail})"
        enter = self._run([
            "tmux", "send-keys", "-t", target, "Enter",
        ], timeout=2)
        if enter.returncode != 0:
            detail = (enter.stderr or "").strip() or f"exit {enter.returncode}"
            return False, f"tmux sent the text but could not press Enter ({detail})"
        return True, "command accepted by the configured tmux pane"

    def send_command(self, instance: ServerInstance, command: str) -> bool:
        """Compatibility boolean wrapper around :meth:`send_command_result`."""
        return self.send_command_result(instance, command)[0]

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
        running = self.probe_running(instance)
        if running is None:
            return False, "Could not verify the tmux session; retry shortly"
        if not running:
            if self._unmanaged_pids(instance):
                return False, (
                    "A server process is running outside the configured tmux "
                    "session; Crucible cannot attach to its console"
                )
            return False, "Server is not running in its configured tmux session"

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
    ) -> dict[str, Literal[
        "running", "unmanaged", "stopped", "missing", "unknown", "tmux_missing"
    ]]:
        """Return process/filesystem truth without converting query errors to offline."""
        if not self.tmux_available():
            return {i.id: "tmux_missing" for i in instances}
        queried = self.list_sessions()
        if queried is None:
            return {i.id: "unknown" for i in instances}
        sessions = set(queried)
        result = {}
        for instance in instances:
            if self.session_name(instance) in sessions:
                result[instance.id] = "running"
            elif self._unmanaged_pids(instance):
                result[instance.id] = "unmanaged"
            elif not Path(instance.path).is_dir():
                result[instance.id] = "missing"
            else:
                result[instance.id] = "stopped"
        return result
