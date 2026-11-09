"""Lightweight filesystem locations shared by backup UI and implementation."""

from pathlib import Path

DEFAULT_FLEET_BACKUP_DIR = (
    Path.home() / ".local" / "share" / "crucible-backups" / "fleet"
)
