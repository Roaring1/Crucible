"""On-demand aggregate resource snapshot for the system tray.

This module deliberately has no timer, service, third-party dependency, or
import-time work. The UI calls it only when the user opens System usage. One
short worker samples /proc twice and queries an installed GPU utility, then
exits. CPU is reported as a share of the whole machine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .resource_monitor import ResourceSampler, machine_cpu_percent, system_memory_mb


@dataclass(frozen=True)
class MachineUsage:
    managed_cpu_pct: float | None
    managed_memory_mb: float | None
    system_cpu_pct: float | None
    system_memory_used_mb: float
    system_memory_total_mb: float
    gpu_pct: float | None
    gpu_memory_used_mb: float | None
    gpu_memory_total_mb: float | None


def _system_cpu_ticks() -> tuple[int, int] | None:
    """Return (total, idle+io-wait) jiffies from /proc/stat."""
    try:
        first = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
        fields = first.split()
        if not fields or fields[0] != "cpu":
            return None
        values = [int(value) for value in fields[1:]]
        if len(values) < 4:
            return None
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    except (OSError, ValueError, IndexError):
        return None


def _instance_processes(instances) -> list[int] | None:
    """Return all managed JVM PIDs from one complete fleet inventory.

    None means procfs could not be inspected completely; never report that as
    an empty managed-process set.
    """
    inventory = ResourceSampler().find_instances_pids(
        instances, owner_uid=os.getuid()
    )
    if inventory is None:
        return None
    return sorted({pid for pids in inventory.values() for pid in pids})


def _gpu_usage(timeout_s: float = 1.5) -> tuple[float | None, float | None, float | None]:
    """Best-effort NVIDIA query; unsupported or unavailable means None."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None, None, None
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None, None
    if result.returncode != 0:
        return None, None, None
    samples: list[tuple[float, float, float]] = []
    for line in result.stdout.splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 3:
            continue
        try:
            samples.append((float(fields[0]), float(fields[1]), float(fields[2])))
        except ValueError:
            continue
    if not samples:
        return None, None, None
    return (
        sum(item[0] for item in samples) / len(samples),
        sum(item[1] for item in samples),
        sum(item[2] for item in samples),
    )


def sample_machine_usage(instances, gui_pid: int | None = None) -> MachineUsage:
    """Take one bounded, on-demand machine snapshot.

    Sampling waits for only 350 ms between process/cpu counters. No timer is
    installed and no work continues after this function returns.
    """
    gui_pid = os.getpid() if gui_pid is None else int(gui_pid)
    server_pids = _instance_processes(instances)
    inventory_complete = server_pids is not None
    managed_pids = sorted(set((server_pids or []) + [gui_pid]))
    sampler = ResourceSampler()

    system_before = _system_cpu_ticks()
    sampler.sample_pids(managed_pids)
    time.sleep(0.35)
    process_stats = sampler.sample_pids(managed_pids)
    system_after = _system_cpu_ticks()

    managed_cpu_core = sum(item.cpu_pct for item in process_stats.values())
    managed_cpu = (
        machine_cpu_percent(managed_cpu_core) if inventory_complete else None
    )

    system_cpu: float | None = None
    if system_before is not None and system_after is not None:
        total_delta = system_after[0] - system_before[0]
        idle_delta = system_after[1] - system_before[1]
        if total_delta > 0 and 0 <= idle_delta <= total_delta:
            system_cpu = max(
                0.0,
                min(100.0, (total_delta - idle_delta) / total_delta * 100.0),
            )

    managed_memory = (
        sum(item.rss_mb for item in process_stats.values())
        if inventory_complete else None
    )
    system_used, system_total = system_memory_mb()
    gpu_pct, gpu_used, gpu_total = _gpu_usage()

    return MachineUsage(
        managed_cpu_pct=managed_cpu,
        managed_memory_mb=managed_memory,
        system_cpu_pct=system_cpu,
        system_memory_used_mb=system_used,
        system_memory_total_mb=system_total,
        gpu_pct=gpu_pct,
        gpu_memory_used_mb=gpu_used,
        gpu_memory_total_mb=gpu_total,
    )
