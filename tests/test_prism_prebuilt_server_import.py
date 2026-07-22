import os
import shutil
import tempfile
import unittest
from pathlib import Path

from crucible.importers.prism import (
    _looks_like_prebuilt_server,
    import_prism_source,
)


class TestPrebuiltServerDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_bare_client_instance_not_detected(self):
        root = self.tmp / "client"
        (root / "mods").mkdir(parents=True)
        (root / "mods" / "somemod.jar").write_bytes(b"x")
        (root / "config").mkdir()
        self.assertFalse(_looks_like_prebuilt_server(root))

    def test_gtnh_style_server_pack_detected(self):
        root = self.tmp / "gtnh_server"
        root.mkdir()
        (root / "startserver-java9.sh").write_text("#!/bin/sh\necho hi\n")
        (root / "mods").mkdir()
        self.assertTrue(_looks_like_prebuilt_server(root))

    def test_forge_jar_at_top_level_detected(self):
        root = self.tmp / "forge_server"
        root.mkdir()
        (root / "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar").write_bytes(b"x")
        self.assertTrue(_looks_like_prebuilt_server(root))


class TestImportPreservesRealServerFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_prebuilt_server_start_script_and_jar_survive_import(self):
        src = self.tmp / "source_server_pack"
        src.mkdir()
        script = src / "startserver-java9.sh"
        script.write_text("#!/bin/sh\nexec java -jar forge-server.jar\n")
        os.chmod(script, 0o755)
        (src / "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar").write_bytes(b"fake-jar-bytes")
        mods = src / "mods"
        mods.mkdir()
        (mods / "somemod.jar").write_bytes(b"x")

        target = self.tmp / "target_instance"
        import_prism_source(src, target, accept_eula=True)

        # The real launcher files must survive -- this is the exact regression
        # a narrow client-import allowlist previously caused (real dedicated
        # Server Pack downloads silently lost their jar/start script).
        self.assertTrue((target / "startserver-java9.sh").exists())
        self.assertTrue(
            (target / "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar").exists()
        )
        self.assertTrue((target / "mods" / "somemod.jar").exists())
        # Crucible must NOT clobber the real script with its own placeholder.
        content = (target / "startserver-java9.sh").read_text()
        self.assertIn("forge-server.jar", content)
        self.assertNotIn("Crucible could not find a runnable server jar", content)
        # The copied script must remain executable.
        self.assertTrue(os.access(target / "startserver-java9.sh", os.X_OK))

    def test_bare_client_instance_still_gets_placeholder(self):
        src = self.tmp / "source_client"
        src.mkdir()
        mods = src / "mods"
        mods.mkdir()
        (mods / "somemod.jar").write_bytes(b"x")

        target = self.tmp / "target_instance2"
        import_prism_source(src, target, accept_eula=True)

        content = (target / "start.sh").read_text()
        self.assertIn("Crucible could not find a runnable server jar", content)


if __name__ == "__main__":
    unittest.main()
