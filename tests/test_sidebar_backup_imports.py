from __future__ import annotations

import subprocess
import sys


def test_main_window_import_does_not_load_fleet_backup_implementation():
    code = (
        "import crucible.ui.main_window, sys; "
        "assert 'crucible.data.fleet_backup' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_backup_path_constant_is_shared_without_loading_implementation():
    code = (
        "from crucible.data.backup_paths import DEFAULT_FLEET_BACKUP_DIR; "
        "import sys; "
        "assert DEFAULT_FLEET_BACKUP_DIR.parts[-4:] == ('.local', 'share', 'crucible-backups', 'fleet'); "
        "assert 'crucible.data.fleet_backup' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_backup_implementation_reexports_the_shared_default_path():
    code = (
        "from crucible.data.backup_paths import DEFAULT_FLEET_BACKUP_DIR as shared; "
        "from crucible.data.fleet_backup import DEFAULT_FLEET_BACKUP_DIR as public; "
        "assert shared == public"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_main_window_import_defers_crash_recovery_until_startup_check():
    code = (
        "import crucible.ui.main_window, sys; "
        "assert 'crucible.process.crash_recovery' not in sys.modules; "
        "assert 'crucible.process.watchdog' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
