import unittest
from unittest.mock import patch

from crucible.data.instance_model import ServerInstance
from crucible.process.resource_monitor import ResourceSampler


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


class FindInstancesPidsTests(unittest.TestCase):
    def setUp(self):
        self.sampler = ResourceSampler()
        self.inst = ServerInstance(
            "/srv/a", "A", tmux_session="gt-new-horizons-a"
        )

    def test_one_proc_scan_associates_multiple_instances(self):
        instances = [
            ServerInstance("/srv/a", "A", tmux_session="server-a"),
            ServerInstance("/srv/b", "B", tmux_session="server-b"),
        ]
        commands = {
            10: "java -Dcrucible.session=server-a -Xmx4G",
            20: "java -Dcrucible.session=server-b -Xmx6G",
            30: "java -jar server.jar",
            40: "java -jar /srv/a/server.jar",
            50: "dolphin /srv/a",
        }
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=list(commands)) as scan,
            patch("crucible.process.resource_monitor._read_cmd", side_effect=lambda pid: commands[pid]),
            patch(
                "crucible.process.resource_monitor._read_comm",
                side_effect=lambda pid: "java" if pid in {10, 20, 30, 40} else "dolphin",
            ),
            patch(
                "crucible.process.resource_monitor._proc_cwd",
                side_effect=lambda pid: "/srv/b" if pid == 30 else "",
            ),
        ):
            inventory = self.sampler.find_instances_pids(instances)

        self.assertEqual(inventory, {instances[0].id: [10, 40], instances[1].id: [20, 30]})
        scan.assert_called_once_with()

    def test_sibling_server_paths_do_not_cross_match_command_arguments(self):
        instances = [
            ServerInstance("/srv/game", "Short path", tmux_session="old-short"),
            ServerInstance("/srv/game-old", "Long path", tmux_session="old-long"),
        ]
        command = "java -jar /srv/game-old/server.jar"
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[4321]),
            patch("crucible.process.resource_monitor._read_comm", return_value="java"),
            patch("crucible.process.resource_monitor._read_cmd", return_value=command),
            patch("crucible.process.resource_monitor._proc_cwd", return_value="/opt/other"),
        ):
            inventory = self.sampler.find_instances_pids(instances)

        self.assertEqual(inventory, {instances[0].id: [], instances[1].id: [4321]})

    def test_embedded_jvm_path_property_obeys_path_component_boundaries(self):
        instances = [
            ServerInstance("/srv/game", "Short path", tmux_session="old-short"),
            ServerInstance("/srv/game-old", "Long path", tmux_session="old-long"),
        ]
        command = "java -Duser.dir=/srv/game-old -jar server.jar"
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[4321]),
            patch("crucible.process.resource_monitor._read_comm", return_value="java"),
            patch("crucible.process.resource_monitor._read_cmd", return_value=command),
            patch("crucible.process.resource_monitor._proc_cwd", return_value="/opt/other"),
        ):
            inventory = self.sampler.find_instances_pids(instances)

        self.assertEqual(inventory, {instances[0].id: [], instances[1].id: [4321]})

    def test_standalone_and_descendant_path_arguments_still_match(self):
        from crucible.process.resource_monitor import _command_contains_path

        self.assertTrue(_command_contains_path("java -jar /srv/game/server.jar", "/srv/game"))
        self.assertTrue(_command_contains_path("java -Duser.dir=/srv/game", "/srv/game"))
        self.assertTrue(_command_contains_path("java -Duser.dir='/srv/game dir'", "/srv/game dir"))
        self.assertFalse(_command_contains_path("java -jar /srv/game-old/server.jar", "/srv/game"))
        self.assertFalse(_command_contains_path("java -jar /other/srv/game/server.jar", "/srv/game"))

    def test_failed_pid_enumeration_is_unknown_not_empty(self):
        instances = [self.inst]
        with patch(
            "crucible.process.resource_monitor._iter_pids", return_value=None
        ):
            self.assertIsNone(self.sampler.find_instances_pids(instances))

    def test_unreadable_process_invalidates_partial_inventory(self):
        instances = [self.inst]
        with (
            patch(
                "crucible.process.resource_monitor._iter_pids",
                return_value=[555, 666],
            ),
            patch(
                "crucible.process.resource_monitor._read_comm", return_value="java"
            ),
            patch(
                "crucible.process.resource_monitor._read_cmd",
                side_effect=[
                    f"java -Dcrucible.session={self.inst.tmux_session}",
                    None,
                ],
            ),
        ):
            self.assertIsNone(self.sampler.find_instances_pids(instances))

    def test_unreadable_java_identity_invalidates_path_fallback_scan(self):
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555]),
            patch(
                "crucible.process.resource_monitor._read_cmd",
                return_value="java -jar server.jar",
            ),
            patch("crucible.process.resource_monitor._read_comm", return_value=None),
        ):
            self.assertIsNone(self.sampler.find_instances_pids([self.inst]))

    def test_unreadable_working_directory_invalidates_path_fallback_scan(self):
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555]),
            patch(
                "crucible.process.resource_monitor._read_cmd",
                return_value="java -jar server.jar",
            ),
            patch("crucible.process.resource_monitor._read_comm", return_value="java"),
            patch("crucible.process.resource_monitor._proc_cwd", return_value=None),
        ):
            self.assertIsNone(self.sampler.find_instances_pids([self.inst]))

    def test_process_exiting_during_scan_is_normal_churn(self):
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555]),
            patch("crucible.process.resource_monitor._read_comm", return_value="java"),
            patch("crucible.process.resource_monitor._read_cmd", return_value=""),
        ):
            self.assertEqual(
                self.sampler.find_instances_pids([self.inst]),
                {self.inst.id: []},
            )

    def test_non_java_process_does_not_read_its_command_line(self):
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555]),
            patch("crucible.process.resource_monitor._read_comm", return_value="dolphin"),
            patch("crucible.process.resource_monitor._read_cmd") as read_cmd,
        ):
            self.assertEqual(
                self.sampler.find_instances_pids([self.inst]),
                {self.inst.id: []},
            )
        read_cmd.assert_not_called()


    def test_comm_with_invalid_utf8_does_not_abort_process_scan(self):
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555]),
            patch("builtins.open", return_value=__import__("io").BytesIO(b"java\xff")),
        ):
            self.assertEqual(
                self.sampler.find_instances_pids([self.inst]),
                {self.inst.id: []},
            )


    def test_uid_scoped_scan_skips_other_users_before_reading_comm(self):
        import os
        from types import SimpleNamespace

        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555, 666]),
            patch(
                "crucible.process.resource_monitor.os.stat",
                side_effect=[
                    SimpleNamespace(st_uid=os.getuid()),
                    SimpleNamespace(st_uid=os.getuid() + 1),
                ],
            ),
            patch(
                "crucible.process.resource_monitor._read_comm",
                return_value="java",
            ) as read_comm,
            patch(
                "crucible.process.resource_monitor._read_cmd",
                return_value=f"java -Dcrucible.session={self.inst.tmux_session}",
            ) as read_cmd,
        ):
            inventory = self.sampler.find_instances_pids(
                [self.inst], owner_uid=os.getuid()
            )

        self.assertEqual(inventory, {self.inst.id: [555]})
        read_comm.assert_called_once_with(555)
        read_cmd.assert_called_once_with(555)

    def test_uid_scoped_scan_returns_unknown_when_process_owner_cannot_be_read(self):
        with (
            patch("crucible.process.resource_monitor._iter_pids", return_value=[555]),
            patch(
                "crucible.process.resource_monitor.os.stat",
                side_effect=PermissionError("procfs owner hidden"),
            ),
        ):
            self.assertIsNone(
                self.sampler.find_instances_pids(
                    [self.inst], owner_uid=__import__("os").getuid()
                )
            )

    def test_os_error_enumerating_proc_is_unknown(self):
        with patch(
            "crucible.process.resource_monitor.os.listdir",
            side_effect=PermissionError("procfs hidden"),
        ):
            self.assertIsNone(
                self.sampler.find_instances_pids([self.inst])
            )


if __name__ == "__main__":
    unittest.main()
