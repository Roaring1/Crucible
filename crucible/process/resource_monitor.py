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
from pathlib import Path

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = 4096

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


def _read_cmd(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _have_proc() -> bool:
    return Path("/proc/self/stat").exists()


def _iter_pids() -> list[int]:
    if not _have_proc():
        return []
    return [int(e) for e in os.listdir("/proc") if e.isdigit()]


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
        now = time.time()
        result: dict[int, ProcStat] = {}
        new_jiffies: dict[int, int] = {}
        for pid in pids:
            j = _read_jiffies(pid)
            new_jiffies[pid] = j
            result[pid] = ProcStat(
                pid=pid, rss_mb=_read_rss_mb(pid),
                cpu_pct=self._cpu_pct(pid, now, j), cmd=_read_cmd(pid),
            )
        self._prev_jiffies = new_jiffies
        self._prev_time = now
        return result

    def find_instance_pids(self, instance) -> list[int]:
        """Java/server PIDs belonging to one instance.

        Matches the per-instance marker injected into JAVA_ARGS, then falls back
        to matching the server directory in the process cwd or command line.
        """
        if instance is None:
            return []
        path = str(getattr(instance, "path", "") or "")
        slug = str(getattr(instance, "tmux_session", "") or "")
        marker = SESSION_MARKER + slug if slug else None
        pids: list[int] = []
        for pid in _iter_pids():
            cmd = _read_cmd(pid)
            if marker and marker in cmd:
                pids.append(pid)
                continue
            if "java" in cmd.lower() and path and (path in cmd or _proc_cwd(pid) == path):
                pids.append(pid)
        return pids

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
