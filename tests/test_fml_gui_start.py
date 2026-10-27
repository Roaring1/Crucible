import unittest
from pathlib import Path
from crucible.data.instance_model import ServerInstance
from crucible.process.netfix import ensure_fml_query_result

class FmlGuiStartTests(unittest.TestCase):
    def test_confirm_is_added(self):
        self.assertEqual(ensure_fml_query_result('-Xmx4G', 'confirm'), '-Xmx4G -Dfml.queryResult=confirm')

    def test_existing_choice_is_replaced_not_duplicated(self):
        result = ensure_fml_query_result('-Dfml.queryResult=cancel -Xmx4G', 'confirm')
        self.assertEqual(result.count('-Dfml.queryResult='), 1)
        self.assertIn('-Dfml.queryResult=confirm', result)

    def test_ask_leaves_user_arguments_alone(self):
        self.assertEqual(ensure_fml_query_result('-Xmx4G', 'ask'), '-Xmx4G')

    def test_instance_choice_round_trips_and_old_registry_is_safe(self):
        inst = ServerInstance('/tmp/server', 'Server', fml_query_result='confirm')
        self.assertEqual(ServerInstance.from_dict(inst.to_dict()).fml_query_result, 'confirm')
        old = inst.to_dict(); old.pop('fml_query_result')
        self.assertEqual(ServerInstance.from_dict(old).fml_query_result, 'ask')

    def test_gui_and_universal_jvm_compatibility_paths_exist(self):
        root = Path(__file__).parents[1]
        tmux = (root / 'crucible/process/tmux_manager.py').read_text()
        panel = (root / 'crucible/ui/instance_panel.py').read_text()
        setup = (root / 'crucible/ui/tabs/setup_tab.py').read_text()
        self.assertIn('JAVA_TOOL_OPTIONS=', tmux)
        self.assertIn('Continue startup', panel)
        self.assertIn('Continue automatically (confirm)', setup)
