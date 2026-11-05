"""Install and inspect the lightweight systemd user fleet-backup timer."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE_NAME = "crucible-fleet-backup.service"
TIMER_NAME = "crucible-fleet-backup.timer"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


class BackupSchedulerError(RuntimeError):
    """The automatic fleet backup timer could not be installed or inspected."""


def _systemctl() -> str:
    if sys.platform != "linux":
        raise BackupSchedulerError(
            "Automatic systemd backups are available on Linux only."
        )
    executable = shutil.which("systemctl")
    if not executable:
        raise BackupSchedulerError(
            "systemctl is not installed; automatic timer scheduling is unavailable."
        )
    return executable


def _run_systemctl(
    *arguments: str, timeout_s: int = 20
) -> subprocess.CompletedProcess[str]:
    executable = _systemctl()
    try:
        return subprocess.run(
            [executable, "--user", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupSchedulerError(
            f"Could not run systemctl --user {' '.join(arguments)}: {exc}"
        ) from exc


def _systemd_quote(value: str | Path) -> str:
    """Quote an ExecStart argument containing spaces or unit specifiers."""
    escaped = str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_path(value: str | Path) -> str:
    """Escape a path-valued unit setting; unlike ExecStart, it is not argv."""
    return str(value).replace("%", "%%").replace("\\", "\\\\")


def _installed_package_root(package_spec) -> Path:
    """Find the directory containing the importable Crucible package.

    A GUI may have been started with the repository as its current directory.
    A timer runs later with a different working directory, so it must point
    systemd at the exact package root rather than relying on that transient
    interactive sys.path entry.
    """
    locations = getattr(package_spec, "submodule_search_locations", None)
    if locations:
        package_directory = Path(next(iter(locations))).expanduser()
    else:
        origin = getattr(package_spec, "origin", None)
        if not origin or origin in {"built-in", "frozen"}:
            raise BackupSchedulerError(
                "The Crucible package has no filesystem location usable by the backup service."
            )
        origin_path = Path(origin).expanduser()
        package_directory = origin_path.parent

    if package_directory.is_symlink() or not package_directory.is_dir():
        raise BackupSchedulerError(
            f"The resolved Crucible package directory is missing or unsafe: {package_directory}"
        )
    package_root = package_directory.resolve().parent
    if not package_root.is_dir():
        raise BackupSchedulerError(
            f"The Crucible package root does not exist: {package_root}"
        )
    return package_root


def _unit_contents() -> tuple[str, str]:
    python = Path(sys.executable).resolve()
    if not python.is_file():
        raise BackupSchedulerError(
            f"Current Python interpreter does not exist: {python}"
        )
    try:
        package_spec = importlib.util.find_spec("crucible")
    except (ImportError, ValueError) as exc:
        raise BackupSchedulerError(
            f"The current Python interpreter cannot resolve the Crucible package: {exc}"
        ) from exc
    if package_spec is None:
        raise BackupSchedulerError(
            "The current Python interpreter cannot import Crucible. "
            "Install/deploy the application before enabling automatic backups."
        )
    package_root = _installed_package_root(package_spec)
    service = f"""[Unit]
Description=Crucible complete fleet backup
Documentation=https://github.com/Roaring1/Crucible/issues/49
After=default.target

[Service]
Type=oneshot
ExecStart={_systemd_quote(python)} -m crucible backup-fleet
WorkingDirectory={_systemd_path(package_root)}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
TimeoutStartSec=infinity
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
NoNewPrivileges=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
LockPersonality=true
RestrictSUIDSGID=true
"""
    timer = f"""[Unit]
Description=Crucible fleet backups every ten minutes

[Timer]
OnCalendar=*:0/10
Persistent=true
AccuracySec=30s
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""
    return service, timer


def _validate_unit_directory() -> None:
    """Refuse symlinked user-systemd path components before writing/removing units."""
    for directory in (
        UNIT_DIR,
        UNIT_DIR.parent,
        UNIT_DIR.parent.parent,
    ):
        if directory.is_symlink():
            raise BackupSchedulerError(
                f"Refusing a symbolic link in the user systemd unit directory: {directory}"
            )


def _atomic_write(path: Path, text: str) -> None:
    _validate_unit_directory()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _validate_unit_directory()
    path.parent.chmod(0o700)
    if path.is_symlink():
        raise BackupSchedulerError(f"Refusing to replace a symbolic link at {path}.")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def install_timer() -> dict[str, str]:
    """Install/refresh the per-user units and enable the ten-minute timer."""
    _validate_unit_directory()
    service, timer = _unit_contents()
    _atomic_write(UNIT_DIR / SERVICE_NAME, service)
    _atomic_write(UNIT_DIR / TIMER_NAME, timer)

    reload_result = _run_systemctl("daemon-reload")
    if reload_result.returncode != 0:
        raise BackupSchedulerError(
            "systemd could not reload the user units: "
            + (reload_result.stderr.strip() or reload_result.stdout.strip())
        )
    enable_result = _run_systemctl("enable", "--now", TIMER_NAME)
    if enable_result.returncode != 0:
        raise BackupSchedulerError(
            "Could not enable the automatic fleet backup timer: "
            + (enable_result.stderr.strip() or enable_result.stdout.strip())
        )
    state = scheduler_status()
    if not state["active"]:
        raise BackupSchedulerError(
            "systemd accepted the timer units but the timer is not active. "
            + state["detail"]
        )
    return state


def remove_timer() -> dict[str, str]:
    """Disable and remove the user timer units; retained generations are untouched."""
    _validate_unit_directory()
    # Validate both unit paths before asking systemd to touch them.
    for name in (SERVICE_NAME, TIMER_NAME):
        path = UNIT_DIR / name
        if path.is_symlink():
            raise BackupSchedulerError(f"Refusing a symbolic link at {path}.")
    result = _run_systemctl("disable", "--now", TIMER_NAME)
    if result.returncode not in (0, 1):
        raise BackupSchedulerError(
            "Could not disable the automatic fleet backup timer: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    for name in (SERVICE_NAME, TIMER_NAME):
        path = UNIT_DIR / name
        if path.is_symlink():
            raise BackupSchedulerError(f"Refusing a symbolic link at {path}.")
        path.unlink(missing_ok=True)
    reload_result = _run_systemctl("daemon-reload")
    if reload_result.returncode != 0:
        raise BackupSchedulerError(
            "The timer was removed but systemd could not reload its user units: "
            + (reload_result.stderr.strip() or reload_result.stdout.strip())
        )
    return scheduler_status()


def scheduler_status() -> dict[str, str]:
    """Return timer activation/enabled state and next scheduled run, if known."""
    if sys.platform != "linux" or not shutil.which("systemctl"):
        return {
            "active": "false",
            "enabled": "false",
            "next": "",
            "detail": "systemd user services are unavailable on this platform.",
        }

    active_result = _run_systemctl("is-active", TIMER_NAME)
    enabled_result = _run_systemctl("is-enabled", TIMER_NAME)
    show_result = _run_systemctl(
        "show",
        TIMER_NAME,
        "--property=NextElapseUSecRealtime",
        "--property=LastTriggerUSec",
    )
    properties = {}
    for line in show_result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value

    active = active_result.returncode == 0 and active_result.stdout.strip() == "active"
    enabled = (
        enabled_result.returncode == 0 and enabled_result.stdout.strip() == "enabled"
    )
    details = []
    if not active:
        details.append(
            active_result.stderr.strip()
            or active_result.stdout.strip()
            or "timer is not active"
        )
    return {
        "active": "true" if active else "false",
        "enabled": "true" if enabled else "false",
        "next": properties.get("NextElapseUSecRealtime", ""),
        "last": properties.get("LastTriggerUSec", ""),
        "detail": "; ".join(details) if details else "systemd timer is active",
    }
