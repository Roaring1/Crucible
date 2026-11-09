"""Package re-exports must not eagerly import unrelated subsystems."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("crucible.data.properties", "crucible.data.instance_manager"),
        ("crucible.process.resource_monitor", "crucible.process.tmux_manager"),
        ("crucible.mods.distribution_catalog", "crucible.mods.mod_manager"),
        ("crucible.importers.downloader", "crucible.importers.prism"),
    ],
)
def test_lightweight_submodule_does_not_import_unrequested_sibling(module, forbidden):
    code = (
        "import importlib, sys; "
        f"importlib.import_module({module!r}); "
        f"assert {forbidden!r} not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_public_package_exports_remain_available():
    code = "\n".join(
        [
            "from crucible.data import ServerInstance, InstanceManager, BackupManager",
            "from crucible.process import TmuxManager, LogWatcher, Watchdog",
            "from crucible.mods import ModManager, ModEntry",
            "from crucible.importers import PrismImportPlan, PrismPackInfo, detect_prism_source, import_prism_source, scan_prism_instances",
            "from crucible.exporters import export, ExportResult",
        ]
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_dir_lists_the_existing_public_exports():
    code = "\n".join(
        [
            "import crucible.data, crucible.process, crucible.mods, crucible.importers, crucible.exporters",
            "assert {'ServerInstance', 'InstanceManager'} <= set(dir(crucible.data))",
            "assert {'TmuxManager'} <= set(dir(crucible.process))",
            "assert {'ModManager', 'ModEntry'} <= set(dir(crucible.mods))",
            "assert {'PrismImportPlan', 'PrismPackInfo', 'detect_prism_source', 'import_prism_source', 'scan_prism_instances'} <= set(dir(crucible.importers))",
            "assert {'export', 'ExportResult'} <= set(dir(crucible.exporters))",
        ]
    )
    subprocess.run([sys.executable, "-c", code], check=True)
