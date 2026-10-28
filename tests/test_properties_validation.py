import unittest

from crucible.data.properties import PropertiesDoc


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


class ModdedWorldTypeValidationTests(unittest.TestCase):
    def _level_type_issues(self, value: str):
        return PropertiesDoc.loads(f"level-type={value}\n").validate()

    def test_accepts_namespaced_modded_world_type(self):
        self.assertEqual(self._level_type_issues("wover:normal"), [])

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
