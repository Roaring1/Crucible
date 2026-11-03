"""systemd user timer rendering and lifecycle tests; never touch a real user unit."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crucible.data import backup_scheduler


def _completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        list(argv), returncode, stdout=stdout, stderr=stderr
    )


def test_timer_install_writes_private_ten_minute_user_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr(backup_scheduler, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(backup_scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        backup_scheduler.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    commands: list[tuple[str, ...]] = []

    def fake_run(*args, **_kwargs):
        commands.append(tuple(args))
        if args == ("is-active", backup_scheduler.TIMER_NAME):
            return _completed(args, "active\n")
        if args == ("is-enabled", backup_scheduler.TIMER_NAME):
            return _completed(args, "enabled\n")
        if args[0] == "show":
            return _completed(
                args,
                "NextElapseUSecRealtime=Mon 2026-11-02 23:40:00 PST\n"
                "LastTriggerUSec=Mon 2026-11-02 23:30:00 PST\n",
            )
        return _completed(args)

    monkeypatch.setattr(backup_scheduler, "_run_systemctl", fake_run)
    status = backup_scheduler.install_timer()

    service = unit_dir / backup_scheduler.SERVICE_NAME
    timer = unit_dir / backup_scheduler.TIMER_NAME
    assert status["active"] == "true"
    assert status["enabled"] == "true"
    assert "backup-fleet" in service.read_text(encoding="utf-8")
    assert "TimeoutStartSec=infinity" in service.read_text(encoding="utf-8")
    assert "OnCalendar=*:0/10" in timer.read_text(encoding="utf-8")
    assert "Persistent=true" in timer.read_text(encoding="utf-8")
    assert unit_dir.stat().st_mode & 0o777 == 0o700
    assert service.stat().st_mode & 0o777 == 0o600
    assert timer.stat().st_mode & 0o777 == 0o600
    assert ("enable", "--now", backup_scheduler.TIMER_NAME) in commands


def test_remove_timer_removes_only_units_and_keeps_backup_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr(backup_scheduler, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(backup_scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        backup_scheduler.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    unit_dir.mkdir(parents=True)
    for name in (backup_scheduler.SERVICE_NAME, backup_scheduler.TIMER_NAME):
        (unit_dir / name).write_text("fixture", encoding="utf-8")

    generation = tmp_path / "fleet" / "generations" / "generation-1"
    generation.mkdir(parents=True)
    marker = generation / "manifest.json"
    marker.write_text("keep", encoding="utf-8")

    def fake_run(*args, **_kwargs):
        return _completed(args, "inactive\n" if args[0] == "is-active" else "")

    monkeypatch.setattr(backup_scheduler, "_run_systemctl", fake_run)
    backup_scheduler.remove_timer()

    assert not (unit_dir / backup_scheduler.SERVICE_NAME).exists()
    assert not (unit_dir / backup_scheduler.TIMER_NAME).exists()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_install_refuses_when_the_timer_interpreter_cannot_import_crucible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(backup_scheduler, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(backup_scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        backup_scheduler.importlib.util, "find_spec", lambda _name: None
    )

    with pytest.raises(
        backup_scheduler.BackupSchedulerError, match="cannot import Crucible"
    ):
        backup_scheduler.install_timer()

    assert not unit_dir.exists()


def test_install_refuses_symlinked_systemd_unit_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_directory = tmp_path / "external-units"
    real_directory.mkdir()
    marker = real_directory / "keep"
    marker.write_text("untouched", encoding="utf-8")
    linked_directory = tmp_path / "units"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setattr(backup_scheduler, "UNIT_DIR", linked_directory)
    monkeypatch.setattr(backup_scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        backup_scheduler.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    with pytest.raises(backup_scheduler.BackupSchedulerError, match="symbolic link"):
        backup_scheduler.install_timer()

    assert marker.read_text(encoding="utf-8") == "untouched"
    assert sorted(path.name for path in real_directory.iterdir()) == ["keep"]


def test_remove_refuses_symlinked_unit_directory_before_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_directory = tmp_path / "external-units"
    real_directory.mkdir()
    marker = real_directory / backup_scheduler.TIMER_NAME
    marker.write_text("do not delete", encoding="utf-8")
    linked_directory = tmp_path / "units"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setattr(backup_scheduler, "UNIT_DIR", linked_directory)
    monkeypatch.setattr(backup_scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        backup_scheduler.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        backup_scheduler,
        "_run_systemctl",
        lambda *args, **_kwargs: calls.append(tuple(args)) or _completed(args),
    )

    with pytest.raises(backup_scheduler.BackupSchedulerError, match="symbolic link"):
        backup_scheduler.remove_timer()

    assert not calls
    assert marker.read_text(encoding="utf-8") == "do not delete"


def test_remove_refuses_symlinked_unit_before_calling_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(backup_scheduler, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(backup_scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        backup_scheduler.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    unit_dir.mkdir(parents=True)
    outside = tmp_path / "outside-unit"
    outside.write_text("user data", encoding="utf-8")
    (unit_dir / backup_scheduler.SERVICE_NAME).write_text(
        "owned service", encoding="utf-8"
    )
    (unit_dir / backup_scheduler.TIMER_NAME).symlink_to(outside)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        backup_scheduler,
        "_run_systemctl",
        lambda *args, **_kwargs: calls.append(tuple(args)) or _completed(args),
    )

    with pytest.raises(backup_scheduler.BackupSchedulerError, match="symbolic link"):
        backup_scheduler.remove_timer()

    assert not calls
    assert outside.read_text(encoding="utf-8") == "user data"
    assert (unit_dir / backup_scheduler.SERVICE_NAME).exists()


def test_user_unit_runs_from_the_resolved_package_root_without_gui_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "Crucible checkout % 1"
    package_directory = package_root / "crucible"
    package_directory.mkdir(parents=True)
    package_spec = type(
        "PackageSpec",
        (),
        {"submodule_search_locations": [str(package_directory)], "origin": None},
    )()
    monkeypatch.setattr(
        backup_scheduler.importlib.util,
        "find_spec",
        lambda name: package_spec if name == "crucible" else None,
    )
    service, timer = backup_scheduler._unit_contents()

    python = Path(backup_scheduler.sys.executable).resolve()
    assert (
        f"ExecStart={backup_scheduler._systemd_quote(python)} -m crucible backup-fleet"
        in service
    )
    assert f"WorkingDirectory={backup_scheduler._systemd_path(package_root)}" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=full" in service
    assert "PYTHONUNBUFFERED=1" in service
    assert "OnCalendar=*:0/10" in timer
    assert "WantedBy=timers.target" in timer


def test_timer_status_is_unavailable_when_systemd_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_scheduler.sys, "platform", "linux")
    monkeypatch.setattr(backup_scheduler.shutil, "which", lambda _name: None)
    status = backup_scheduler.scheduler_status()
    assert status["active"] == "false"
    assert "unavailable" in status["detail"]


def test_scheduler_refuses_non_linux_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_scheduler.sys, "platform", "win32")
    with pytest.raises(backup_scheduler.BackupSchedulerError, match="Linux only"):
        backup_scheduler.install_timer()
