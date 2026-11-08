"""
crucible/process/resource_monitor.py

Dependency-free process resource sampling so owners can see - at a glance, and
matching what Mission Control / Activity Monitor / htop show - how much CPU and
memory each server uses versus the Crucible GUI itself.

No psutil required. On Linux it reads /proc directly. Every call is best-effort
and never raises. CPU%% is computed across two samples, so call sample_pids()
on a timer (e.g. every 2s) and read the cached percentages.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

SESSION_MARKER = "-Dcrucible.session="


@dataclass
class ProcStat:
    pid: int
    rss_mb: float = 0.0
    cpu_pct: float = 0.0
    cmd: str = ""


def _read_rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm") as f:
            fields = f.read().split()
        return int(fields[1]) * _PAGE / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def _read_jiffies(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        rest = data[data.rfind(")") + 2:].split()
        return int(rest[11]) + int(rest[12])  # utime + stime
    except (OSError, ValueError, IndexError):
        return 0


def _read_cmd(pid: int) -> str | None:
    """Read one command line; None means the process was not inspectable."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, ProcessLookupError):
        # A PID can exit between readdir and the proc-file read.
        return ""
    except OSError:
        return None


def _read_comm(pid: int) -> str | None:
    """Read the kernel executable name; None means inspection failed.

    The executable name avoids treating "java" in a path argument as a JVM.
    """
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode("utf-8", "replace").strip()
    except (FileNotFoundError, ProcessLookupError):
        return ""
    except OSError:
        return None


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (FileNotFoundError, ProcessLookupError):
        return ""
    except OSError:
        return None


def _iter_pids() -> list[int] | None:
    """Enumerate visible processes; None means procfs could not be scanned."""
    try:
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return None


_COMMAND_PATH_BEFORE = frozenset("=:'\"([{,;")
_COMMAND_PATH_AFTER = frozenset("/:'\"),]};,")


def _command_contains_path(command: str, path: str) -> bool:
    """Match a registered path only at argument/path-component boundaries.

    A plain substring check associates ``/srv/game-old`` with the registered
    ``/srv/game`` instance. Paths may be standalone arguments or embedded in
    JVM properties such as ``-Duser.dir=/srv/game``; both forms are accepted,
    but adjacent filename/path characters are not.
    """
    if not path:
        return False
    start = 0
    while (index := command.find(path, start)) >= 0:
        end = index + len(path)
        before_ok = (
            index == 0
            or command[index - 1].isspace()
            or command[index - 1] in _COMMAND_PATH_BEFORE
        )
        after_ok = (
            end == len(command)
            or command[end].isspace()
            or command[end] in _COMMAND_PATH_AFTER
        )
        if before_ok and after_ok:
            return True
        start = index + 1
    return False


class ResourceSampler:
    """Stateful CPU%% sampler. Reuse one instance; call sample_pids() periodically."""

    def __init__(self):
        self._prev_jiffies: dict[int, int] = {}
        self._prev_time: float = 0.0

    def _cpu_pct(self, pid: int, now: float, jiffies: int) -> float:
        prev_j = self._prev_jiffies.get(pid)
        prev_t = self._prev_time
        if prev_j is None or prev_t == 0.0 or now <= prev_t:
            return 0.0
        dj = jiffies - prev_j
        dt = now - prev_t
        if dj < 0 or dt <= 0:
            return 0.0
        return (dj / _CLK_TCK) / dt * 100.0

    def sample_pids(self, pids: list[int]) -> dict[int, ProcStat]:
        now = time.monotonic()
        result: dict[int, ProcStat] = {}
        new_jiffies: dict[int, int] = {}
        for pid in pids:
            j = _read_jiffies(pid)
            new_jiffies[pid] = j
            result[pid] = ProcStat(
                pid=pid, rss_mb=_read_rss_mb(pid),
                cpu_pct=self._cpu_pct(pid, now, j), cmd=_read_cmd(pid) or "",
            )
        self._prev_jiffies = new_jiffies
        self._prev_time = now
        return result

    def find_instances_pids(
        self, instances, *, owner_uid: int | None = None
    ) -> dict[str, list[int]] | None:
        """Find Java/server PIDs for all registered instances in one procfs scan.

        Exact per-session markers are preferred. Older instances fall back to
        the actual Java executable plus a registered path in argv or cwd.

        Returns a mapping (including empty lists) only for a complete scan.
        None means procfs or one of its visible process records could not be
        inspected, so callers must not interpret a partial result as offline.
        PIDs that disappear during a scan are normal process churn and are
        skipped; permission errors and other read failures invalidate the
        complete fleet snapshot. When owner_uid is set, only processes owned
        by that UID are in scope; pass None for all visible users.
        """
        configs: dict[str, tuple[str | None, str]] = {}
        marker_to_ids: dict[str, list[str]] = {}
        has_path_fallback = False
        for instance in instances:
            instance_id = str(getattr(instance, "id", "") or "")
            if not instance_id:
                continue
            path = str(getattr(instance, "path", "") or "")
            slug = str(getattr(instance, "tmux_session", "") or "")
            marker = SESSION_MARKER + slug if slug else None
            configs[instance_id] = (marker, path)
            if marker:
                marker_to_ids.setdefault(marker, []).append(instance_id)
            has_path_fallback = has_path_fallback or bool(path)

        if not configs:
            return {}
        result: dict[str, list[int]] = {instance_id: [] for instance_id in configs}
        if not marker_to_ids and not has_path_fallback:
            return result

        pids = _iter_pids()
        if pids is None:
            return None

        found: dict[str, set[int]] = {instance_id: set() for instance_id in configs}
        for pid in pids:
            if owner_uid is not None:
                try:
                    process_uid = os.stat(f"/proc/{pid}").st_uid
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError:
                    return None
                if process_uid != owner_uid:
                    continue
            # comm is a tiny kernel field. Check it before reading full argv so
            # unrelated processes never incur a command-line read/tokenization.
            comm = _read_comm(pid)
            if comm is None:
                return None
            if comm.casefold() != "java":
                continue

            command = _read_cmd(pid)
            if command is None:
                return None
            if not command:
                # The process exited or has no user-space command line.
                continue

            # Exact marker dictionary lookup avoids comparing every process
            # against every registered instance.
            marker_matches = [
                instance_id
                for token in command.split()
                for instance_id in marker_to_ids.get(token, ())
            ]
            if marker_matches:
                for instance_id in marker_matches:
                    found[instance_id].add(pid)
                continue

            if not has_path_fallback:
                continue
            command_matches = [
                instance_id
                for instance_id, (_marker, path) in configs.items()
                if _command_contains_path(command, path)
            ]
            if command_matches:
                for instance_id in command_matches:
                    found[instance_id].add(pid)
                continue

            cwd = _proc_cwd(pid)
            if cwd is None:
                return None
            if cwd:
                for instance_id, (_marker, path) in configs.items():
                    if path and cwd == path:
                        found[instance_id].add(pid)

        return {instance_id: sorted(pids) for instance_id, pids in found.items()}

    def find_instance_pids(self, instance) -> list[int]:
        """Compatibility wrapper; use find_instances_pids() for safe decisions.

        The legacy list API cannot express an incomplete scan, so it returns an
        empty list on unknown. Safety-sensitive code must use the tri-state
        fleet API and distinguish None from a confirmed empty result.
        """
        if instance is None:
            return []
        instance_id = str(getattr(instance, "id", "") or "")
        inventory = self.find_instances_pids([instance])
        if inventory is None:
            return []
        return inventory.get(instance_id, [])

    def gui_pid(self) -> int:
        return os.getpid()


def system_memory_mb() -> tuple[float, float]:
    """Return (used_mb, total_mb) for the machine, best-effort."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        total = int(info["MemTotal"].split()[0]) / 1024.0
        avail = int(info.get("MemAvailable", "0").split()[0]) / 1024.0
        return (total - avail, total)
    except (OSError, ValueError, KeyError, IndexError):
        return (0.0, 0.0)


def machine_cpu_percent(core_percent: float, logical_cpus: int | None = None) -> float:
    """Convert Linux process CPU (100% = one core) to whole-machine share."""
    count = max(1, logical_cpus if logical_cpus is not None else (os.cpu_count() or 1))
    return max(0.0, min(100.0, core_percent / count))
