import unittest
from unittest.mock import patch

from crucible.process.resource_monitor import ResourceSampler
from crucible.data.instance_model import ServerInstance


class FindInstancePidsTests(unittest.TestCase):
    """GTNH server folders are conventionally named "..._Server_Java_<ver>".

    Any process merely given that path as an argument -- a file manager
    window, an editor, `ls` -- has the substring "java" in its full command
    line purely because of the folder name, even though no JVM is involved.
    find_instance_pids() must not mistake that for a live server process.
    """

    def setUp(self):
        self.path = "/home/roaring/CrucibleServers/Main_GT_New_Horizons_2.8.4_Server_Java_17-25"
        self.inst = ServerInstance(self.path, "GTNH", tmux_session="gt-new-horizons-284-server-java-17-25")
        self.sampler = ResourceSampler()

    def test_file_manager_pointed_at_the_folder_is_not_matched(self):
        """Reproduces the exact false positive: `dolphin <server path>` has
        "java" in its cmdline (from the folder name) but is not a JVM."""
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[1262173]),
            patch(
                "crucible.process.resource_monitor._read_cmd",
                return_value=f"/usr/bin/dolphin {self.path}",
            ),
            patch("crucible.process.resource_monitor._read_comm", return_value="dolphin"),
            patch("crucible.process.resource_monitor._proc_cwd", return_value=""),
        ):
            self.assertEqual(self.sampler.find_instance_pids(self.inst), [])

    def test_real_java_process_in_the_server_dir_is_matched(self):
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555]),
            patch(
                "crucible.process.resource_monitor._read_cmd",
                return_value="java -Xms6G -Xmx6G -jar lwjgl3ify-forgePatches.jar nogui",
            ),
            patch("crucible.process.resource_monitor._read_comm", return_value="java"),
            patch("crucible.process.resource_monitor._proc_cwd", return_value=self.path),
        ):
            self.assertEqual(self.sampler.find_instance_pids(self.inst), [555])

    def test_unrelated_process_with_path_substring_but_no_java_is_not_matched(self):
        """A plain `ls`/`cat`/editor pointed at the path, no "java" anywhere
        in comm, must never match regardless of cmdline contents."""
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[9001]),
            patch(
                "crucible.process.resource_monitor._read_cmd",
                return_value=f"/usr/bin/cat {self.path}/logs/fml-server-latest.log",
            ),
            patch("crucible.process.resource_monitor._read_comm", return_value="cat"),
            patch("crucible.process.resource_monitor._proc_cwd", return_value=""),
        ):
            self.assertEqual(self.sampler.find_instance_pids(self.inst), [])


if __name__ == "__main__":
    unittest.main()
