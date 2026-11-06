from __future__ import annotations

import subprocess
from types import SimpleNamespace

from crucible.process import machine_resources as resources


def test_instance_processes_prefers_exact_session_marker(monkeypatch) -> None:
    instances = [
        SimpleNamespace(id="a", tmux_session="server-a", path="/srv/a"),
        SimpleNamespace(id="b", tmux_session="server-b", path="/srv/b"),
    ]
    monkeypatch.setattr(resources, "_iter_pids", lambda: [10, 20, 30, 40])
    commands = {
        10: "java -Dcrucible.session=server-a -Xmx4G",
        20: "java -Dcrucible.session=other",
        30: "java -jar server.jar",
        40: "dolphin /srv/a",
    }
    monkeypatch.setattr(resources, "_read_cmd", lambda pid: commands[pid])
    monkeypatch.setattr(resources, "_read_comm", lambda pid: "java" if pid in {10, 20, 30} else "dolphin")
    monkeypatch.setattr(resources, "_proc_cwd", lambda pid: "/srv/b" if pid == 30 else "")

    assert resources._instance_processes(instances) == [10, 30]


def test_machine_snapshot_aggregates_managed_process_and_system_values(monkeypatch) -> None:
    samples = [
        {101: SimpleNamespace(cpu_pct=120.0, rss_mb=700.0), 202: SimpleNamespace(cpu_pct=40.0, rss_mb=120.0)},
        {101: SimpleNamespace(cpu_pct=120.0, rss_mb=700.0), 202: SimpleNamespace(cpu_pct=40.0, rss_mb=120.0)},
    ]
    monkeypatch.setattr(resources, "_instance_processes", lambda _instances: [101])
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 80)
    sampler = SimpleNamespace(sample_pids=lambda _pids: samples.pop(0))
    monkeypatch.setattr(resources, "ResourceSampler", lambda: sampler)
    monkeypatch.setattr(resources, "_system_cpu_ticks", iter([(1000, 200), (2000, 900)]).__next__)
    monkeypatch.setattr(resources.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(resources, "system_memory_mb", lambda: (6000.0, 16000.0))
    monkeypatch.setattr(resources, "_gpu_usage", lambda: (17.0, 2048.0, 8192.0))
    # The requested GUI PID is included once alongside registered server PIDs.
    result = resources.sample_machine_usage([object()], gui_pid=202)

    assert result.managed_cpu_pct == 2.0  # (160% of one core) / 80 logical CPUs
    assert result.managed_memory_mb == 820.0
    assert result.system_cpu_pct == 30.0
    assert result.system_memory_used_mb == 6000.0
    assert result.system_memory_total_mb == 16000.0
    assert result.gpu_pct == 17.0
    assert result.gpu_memory_used_mb == 2048.0
    assert result.gpu_memory_total_mb == 8192.0


def test_nvidia_query_averages_utilization_and_sums_vram(monkeypatch) -> None:
    monkeypatch.setattr(resources.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        resources.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "10, 1000, 8000\n30, 2000, 12000\n", ""
        ),
    )
    assert resources._gpu_usage() == (20.0, 3000.0, 20000.0)


def test_gpu_is_unavailable_when_provider_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(resources.shutil, "which", lambda _name: None)
    assert resources._gpu_usage() == (None, None, None)


def test_gpu_query_failure_is_unavailable_not_zero(monkeypatch) -> None:
    monkeypatch.setattr(resources.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        resources.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "failed"),
    )
    assert resources._gpu_usage() == (None, None, None)
