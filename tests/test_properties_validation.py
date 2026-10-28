import unittest

from crucible.data.properties import PropertiesDoc
from crucible.data.instance_model import ServerInstance


class UnknownPropertySuggestionSafetyTests(unittest.TestCase):
    def test_server_id_and_server_name_do_not_get_semantically_unsafe_guesses(self):
        doc = PropertiesDoc.loads("server-id=legacy-value\nserver-name=GTNH Server\n")
        issues = doc.validate(minecraft_version="1.7.10")
        self.assertEqual([issue.key for issue in issues], ["server-id", "server-name"])
        self.assertTrue(all(issue.kind == "unknown_key" for issue in issues))
        self.assertTrue(all(issue.suggestion is None for issue in issues))
        self.assertTrue(all(issue.options == [] for issue in issues))
        self.assertIn("not an ID", issues[0].message)
        self.assertIn("not a display name", issues[1].message)

    def test_full_repair_does_not_rename_ambiguous_custom_or_legacy_keys(self):
        doc = PropertiesDoc.loads("server-id=legacy-value\nserver-name=GTNH Server\n")
        changes = doc.autofix(fix_unknown_keys=True, minecraft_version="1.7.10")
        self.assertEqual(changes, [])
        self.assertEqual(doc.get("server-id"), "legacy-value")
        self.assertEqual(doc.get("server-name"), "GTNH Server")
        self.assertIsNone(doc.get("server-ip"))
        self.assertIsNone(doc.get("level-name"))

    def test_clear_typo_still_gets_a_suggestion(self):
        issues = PropertiesDoc.loads("max-player=12\n").validate(minecraft_version="1.21.1")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].suggestion, "max-players")


class VersionedPropertiesValidationTests(unittest.TestCase):
    def _issues(self, version):
        doc = PropertiesDoc.loads("spawn-animals=true\nspawn-npcs=false\n")
        return doc.validate(minecraft_version=version)

    def test_legacy_spawn_properties_are_valid_in_minecraft_1211(self):
        self.assertEqual(self._issues("1.21.1"), [])

    def test_legacy_spawn_properties_are_valid_in_older_minecraft(self):
        self.assertEqual(self._issues("1.20.6"), [])

    def test_legacy_spawn_properties_are_unknown_in_minecraft_1212(self):
        issues = self._issues("1.21.2")
        self.assertEqual([issue.key for issue in issues], ["spawn-animals", "spawn-npcs"])
        self.assertTrue(all(issue.kind == "unsupported_version" for issue in issues))
        self.assertTrue(all(issue.suggestion is None for issue in issues))
        self.assertTrue(all("removed in Minecraft 1.21.2" in issue.message for issue in issues))

    def test_unknown_server_version_does_not_assume_legacy_properties_are_valid(self):
        for version in (None, "", "unknown"):
            with self.subTest(version=version):
                issues = self._issues(version)
                self.assertEqual([issue.key for issue in issues], ["spawn-animals", "spawn-npcs"])
                self.assertTrue(all(issue.suggestion is None for issue in issues))

    def test_legacy_property_values_are_checked_in_versions_that_support_them(self):
        issues = PropertiesDoc.loads("spawn-animals=maybe\n").validate(minecraft_version="1.21.1")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, "invalid_bool")
        self.assertEqual(issues[0].suggestion, "true")

    def test_gtnh_1710_legacy_properties_are_recognized(self):
        doc = PropertiesDoc.loads(
            "announce-player-achievements=true\n"
            "max-build-height=256\n"
            "snooper-enabled=true\n"
            "spawn-animals=true\n"
            "spawn-npcs=true\n"
            "difficulty=3\n"
            "gamemode=0\n"
            "level-type=rwg\n"
        )
        self.assertEqual(doc.validate(minecraft_version="1.7.10"), [])

    def test_removed_legacy_properties_are_reported_without_rename_suggestions(self):
        doc = PropertiesDoc.loads(
            "announce-player-achievements=true\n"
            "max-build-height=256\n"
            "snooper-enabled=true\n"
        )
        issues = doc.validate(minecraft_version="1.21.1")
        self.assertEqual(
            {i.key for i in issues},
            {"announce-player-achievements", "max-build-height", "snooper-enabled"},
        )
        self.assertTrue(all(i.kind == "unsupported_version" for i in issues))
        self.assertTrue(all(i.suggestion is None for i in issues))


class EffectiveMinecraftVersionTests(unittest.TestCase):
    def test_registered_minecraft_version_wins_over_jar_evidence(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "minecraft_server.1.7.10.jar").touch()
            inst = ServerInstance(path=tmp, name="test", minecraft_version="1.21.1")
            self.assertEqual(inst.effective_minecraft_version(), "1.21.1")

    def test_detects_version_from_exact_minecraft_server_jar_name(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "minecraft_server.1.7.10.jar").touch()
            inst = ServerInstance(path=tmp, name="GTNH", version="2.8.4")
            self.assertEqual(inst.effective_minecraft_version(), "1.7.10")
            self.assertEqual(inst._mc_version_tuple(), (1, 7, 10))

    def test_detects_version_from_forge_universal_jar_name(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "forge-1.20.1-47.2.0-universal.jar").touch()
            inst = ServerInstance(path=tmp, name="Forge", version="custom pack 4.8")
            self.assertEqual(inst.effective_minecraft_version(), "1.20.1")

    def test_mc_label_is_used_but_bare_pack_version_is_not(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            labelled = ServerInstance(path=tmp, name="labelled", version="MC 1.21.1 · NeoForge 21.1.265")
            pack = ServerInstance(path=tmp, name="pack", version="2.8.4")
            self.assertEqual(labelled.effective_minecraft_version(), "1.21.1")
            self.assertIsNone(pack.effective_minecraft_version())
            self.assertEqual(pack._mc_version_tuple(), ())


class ModdedWorldTypeValidationTests(unittest.TestCase):
    def _level_type_issues(self, value: str):
        return PropertiesDoc.loads(f"level-type={value}\n").validate()

    def test_accepts_namespaced_modded_world_type(self):
        self.assertEqual(self._level_type_issues("wover:normal"), [])

    def test_accepts_escaped_colon_for_canonical_vanilla_world_type(self):
        self.assertEqual(self._level_type_issues(r"minecraft\:normal"), [])

    def test_accepts_legacy_modded_world_type_on_old_minecraft(self):
        issues = PropertiesDoc.loads("level-type=rwg\n").validate(minecraft_version="1.7.10")
        self.assertEqual(issues, [])

    def test_accepts_java_properties_escaped_colon_in_modded_world_type(self):
        self.assertEqual(self._level_type_issues(r"wover\:normal"), [])

    def test_still_warns_for_unknown_vanilla_namespaced_world_type(self):
        issues = self._level_type_issues("minecraft:unknown_preset")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, "invalid_enum")

    def test_still_warns_for_unrecognized_unnamespaced_world_type(self):
        issues = self._level_type_issues("wover-normal")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, "invalid_enum")
        self.assertEqual(issues[0].suggestion, "minecraft:normal")

    def test_vanilla_canonical_type_is_accepted_and_legacy_alias_still_suggests_it(self):
        self.assertEqual(self._level_type_issues("minecraft:normal"), [])
        issues = self._level_type_issues("normal")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, "invalid_enum")
        self.assertEqual(issues[0].suggestion, "minecraft:normal")


if __name__ == "__main__":
    unittest.main()
