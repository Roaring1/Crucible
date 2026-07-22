import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseScriptTests(unittest.TestCase):
    def test_installer_is_single_current_safe_script(self):
        text = (ROOT / "install.sh").read_text()
        self.assertEqual(text.count("set -Eeuo pipefail"), 1)
        self.assertNotIn("v0.3.3", text)
        self.assertNotIn('rm -rf "$HERE"', text)
        self.assertIn(".crucible-stage.", text)
        self.assertIn(".crucible-previous.", text)
        self.assertIn("This installer never deletes the folder", text)
        self.assertIn("$APP_HOME/bin/crucible", text)


    def test_installed_launcher_resolves_symlink_before_locating_venv(self):
        text = (ROOT / "install.sh").read_text()
        marker = 'cat > "$STAGE/app/bin/crucible" <<\'EOF\'\n'
        body = text.split(marker, 1)[1].split("\nEOF\n", 1)[0] + "\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "share" / "crucible"
            (app / "bin").mkdir(parents=True)
            (app / "venv" / "bin").mkdir(parents=True)
            (app / "source").mkdir()
            launcher = app / "bin" / "crucible"
            launcher.write_text(body)
            launcher.chmod(0o755)
            fake_python = app / "venv" / "bin" / "python"
            fake_python.write_text("#!/usr/bin/env bash\nprintf '%s\n' \"$PYTHONPATH|$*\"\n")
            fake_python.chmod(0o755)
            link_dir = root / "bin"
            link_dir.mkdir()
            link = link_dir / "crucible"
            link.symlink_to(launcher)
            result = subprocess.run(
                [str(link), "--help"], text=True, capture_output=True,
                check=True, cwd=root,
                env={**os.environ, "PYTHONPATH": ""},
            )
            self.assertIn(str(app / "source"), result.stdout)
            self.assertIn("-m crucible --help", result.stdout)
            self.assertNotIn(str(root / "venv"), result.stdout)

    def test_expensive_server_tabs_are_lazy_loaded(self):
        panel = (ROOT / "crucible/ui/instance_panel.py").read_text()
        self.assertIn("self._loaded_tabs.clear()", panel)
        self.assertIn("self._load_current_tab()", panel)
        load_body = panel.split("def load(self, instance:", 1)[1].split(
            "def current_instance_id", 1
        )[0]
        self.assertNotIn("self._mods.load(instance)", load_body)
        self.assertNotIn("self._backup.load(instance)", load_body)
        self.assertNotIn("self._players.load(instance)", load_body)

    def test_console_stop_uses_lifecycle_state_machine(self):
        console = (ROOT / "crucible/ui/tabs/console_tab.py").read_text()
        panel = (ROOT / "crucible/ui/instance_panel.py").read_text()
        self.assertIn("lifecycle_intent(cmd)", console)
        self.assertIn("_on_console_lifecycle_command", panel)
        self.assertIn('self._update_status_display("stopping")', panel)

    def test_downloader_requires_exact_verified_assets(self):
        text = (ROOT / "get-crucible.sh").read_text()
        self.assertNotIn("{" + "ht" + "tps://", text)
        self.assertIn("https://api.github.com/repos/${REPO}/releases/latest", text)
        self.assertIn('source_name = f"Crucible-{tag}-source.zip"', text)
        self.assertIn('checksum_name = f"Crucible-{tag}-SHA256.txt"', text)
        self.assertIn("sha256sum -c", text)
        self.assertIn("--max-filesize", text)
        self.assertIn("PurePosixPath", text)
        self.assertIn("stat.S_ISLNK", text)
        self.assertNotRegex(text, re.compile(r"first .*\\.zip", re.I))

    def test_wheel_declares_icon_package_data(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('[tool.setuptools.package-data]', text)
        self.assertIn('crucible = ["assets/*.png"]', text)


    def test_stale_async_results_are_generation_guarded(self):
        panel = (ROOT / "crucible/ui/instance_panel.py").read_text()
        setup = (ROOT / "crucible/ui/tabs/setup_tab.py").read_text()
        mods = (ROOT / "crucible/ui/tabs/mods_tab.py").read_text()
        players = (ROOT / "crucible/ui/tabs/players_tab.py").read_text()
        self.assertIn("generation != self._ip_request_generation", panel)
        self.assertIn("generation != self._ip_request_generation", setup)
        self.assertIn("generation != self._inspect_generation", mods)
        self.assertIn("_inspect_pending", mods)
        self.assertIn("_reload_if_current", players)

    def test_icon_only_controls_have_accessible_names(self):
        files = [
            ROOT / "crucible/ui/tabs/config_tab.py",
            ROOT / "crucible/ui/tabs/mods_tab.py",
            ROOT / "crucible/ui/tabs/backup_tab.py",
            ROOT / "crucible/ui/tabs/players_tab.py",
            ROOT / "crucible/ui/tabs/console_tab.py",
            ROOT / "crucible/ui/new_server_dialog.py",
        ]
        text = "\n".join(path.read_text() for path in files)
        for name in (
            "Reload server properties", "Refresh mod list", "Delete backup",
            "Remove {name}", "Find previous match", "Close find bar",
            "Refresh Minecraft version list",
        ):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
