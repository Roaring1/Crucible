import re
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
