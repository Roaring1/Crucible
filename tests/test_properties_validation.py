import unittest

from crucible.data.properties import PropertiesDoc


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
