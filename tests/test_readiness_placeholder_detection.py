"""
tests/test_readiness_placeholder_detection.py

Regression test for a real-world false-positive reported by a user importing
a GT New Horizons Prism instance: the Setup tab's checklist showed a green
checkmark for "Server program is installed", but pressing Start immediately
failed with exit code 2, because the folder only contained Crucible's own
generated start.sh placeholder (written by importers/prism.py when no real
server jar/loader was found during import) -- not an actual dedicated server.

Root cause: ServerInstance.readiness() treated "a file named start.sh (or any
other known start-script name) exists" as proof the server program was
installed, without checking whether that particular start.sh was Crucible's
own stub (which always exits 2 until a real jar/loader shows up) or a real,
pack-provided launcher.

Fix: readiness() (and a new _is_placeholder_start_script() helper) now
detect Crucible's stub via its unique fallback-error marker text and only
report the server program as installed when either a real server jar/loader
glob matches, or the discovered start script is not Crucible's own stub.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from crucible.data.instance_model import (  # noqa: E402
    ServerInstance,
    _is_placeholder_start_script,
    _PLACEHOLDER_START_SH_MARKER,
)
from crucible.importers.prism import _START_SH  # noqa: E402


def _make_instance(path: Path) -> ServerInstance:
    return ServerInstance(id="t", name="t", path=str(path))


class PlaceholderStartScriptDetectionTests(unittest.TestCase):
    def test_marker_matches_real_prism_template(self) -> None:
        """The importer's actual generated script must trip the detector --
        this pins the detector to the real template, not just a copy of it."""
        self.assertIn(_PLACEHOLDER_START_SH_MARKER, _START_SH)

    def test_detects_crucibles_own_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "start.sh"
            p.write_text(_START_SH, encoding="utf-8")
            self.assertTrue(_is_placeholder_start_script(p))

    def test_does_not_flag_a_real_pack_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ServerStart.sh"
            p.write_text("#!/bin/bash\nexec java -jar forge-1.7.10-server.jar nogui\n", encoding="utf-8")
            self.assertFalse(_is_placeholder_start_script(p))

    def test_readiness_reports_not_installed_for_placeholder_only(self) -> None:
        """The exact bug: a folder with only Crucible's stub start.sh (no
        real jar) must NOT show a green 'Server program is installed'."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "start.sh").write_text(_START_SH, encoding="utf-8")
            inst = _make_instance(root)
            items = {item["key"]: item for item in inst.readiness()}
            launcher_item = items["launcher"]
            self.assertFalse(launcher_item["ok"])
            self.assertEqual(launcher_item["fix"], "install_server")

    def test_readiness_reports_installed_for_real_pack_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ServerStart.sh").write_text(
                "#!/bin/bash\nexec java -jar forge-1.7.10-server.jar nogui\n", encoding="utf-8"
            )
            inst = _make_instance(root)
            items = {item["key"]: item for item in inst.readiness()}
            launcher_item = items["launcher"]
            self.assertTrue(launcher_item["ok"])
            self.assertIsNone(launcher_item["fix"])

    def test_readiness_reports_installed_once_a_real_jar_appears_alongside_stub(self) -> None:
        """Once a real jar is dropped in next to Crucible's stub start.sh,
        readiness should flip to ok via the has_server_launcher() glob --
        the placeholder detection must not block that path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "start.sh").write_text(_START_SH, encoding="utf-8")
            (root / "forge-1.7.10-server.jar").write_text("fake jar", encoding="utf-8")
            inst = _make_instance(root)
            items = {item["key"]: item for item in inst.readiness()}
            launcher_item = items["launcher"]
            self.assertTrue(launcher_item["ok"])
            self.assertIsNone(launcher_item["fix"])


if __name__ == "__main__":
    unittest.main()
