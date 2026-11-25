'''Launch-config resolution and overlays, against this workspace's real
lance config (skipped when the orchestrator isn't inside a workspace).'''

import os
import unittest

from backend.core import ORCHESTRATOR_ROOT
from backend.lance_config import ConfigError, LanceConfig


WORKSPACE = os.path.dirname(os.path.dirname(ORCHESTRATOR_ROOT))
HAS_CONFIG = os.path.isfile(os.path.join(
    WORKSPACE, 'src', 'lance', 'config', 'launch.json'))


@unittest.skipUnless(HAS_CONFIG, 'no lance workspace around the orchestrator')
class LanceConfigTest(unittest.TestCase):

    def setUp(self):
        self.config = LanceConfig(WORKSPACE)

    def test_dev_preset_selects_others(self):
        selected = self.config.resolve({'dev': 'robot_motor_sim'})['selected']
        self.assertEqual(selected['dev'], 'robot_motor_sim')
        self.assertIn('redux', selected)

    def test_overlay_derives_from_selected_preset(self):
        arguments = {'dev': 'robot_motor_sim'}
        parent = self.config.resolve(arguments)['selected']['redux']
        overlay, extra, final, constants = self.config.overlayFor(
            arguments, {'redux': {'client_hostname': '10.1.2.3'}}, 'robot')
        self.assertEqual(constants, [])
        self.assertEqual(extra, {'redux': 'orchestrator_robot'})
        self.assertEqual(overlay['pragma:import'], ['launch.json'])
        derived = overlay['redux']['orchestrator_robot']
        self.assertEqual(derived['pragma:derived'], parent)
        self.assertEqual(final['selected']['redux'], 'orchestrator_robot')
        self.assertEqual(final['config']['redux']['client_hostname'],
                         '10.1.2.3')

    def test_nothing_to_change_means_no_overlay(self):
        overlay, extra, final, constants = self.config.overlayFor(
            {'dev': 'robot_motor_sim'}, {}, 'robot', {})
        self.assertIsNone(overlay)
        self.assertEqual(extra, {})

    def test_constants_replace_addresses(self):
        name = 'ip.net1.mac_vm_eth'
        was = self.config.constant(name)
        self.assertIsNotNone(was)
        overlay, extra, final, constants = self.config.overlayFor(
            {'dev': 'robot_motor_sim'}, {}, 'robot', {
                name: '10.9.9.9',
                'ip.net1.mochapanda': self.config.constant(
                    'ip.net1.mochapanda'),           # unchanged: left out
                'ip.net1.no_such_machine': '1.1.1.1',  # unknown: left out
            })
        self.assertEqual(constants, [{'name': name, 'value': '10.9.9.9',
                                      'was': was}])
        self.assertEqual(overlay['pragma:constants'],
                         {'ip': {'net1': {'mac_vm_eth': '10.9.9.9'}}})
        self.assertEqual(extra, {})
        # Presets that use the constant see the new address.
        resolved = self.config.resolve(
            {'dev': 'robot_motor_sim'})['selected']
        self.assertEqual(final['selected'], resolved)

    def test_overlay_on_disabled_action(self):
        with self.assertRaises(ConfigError):
            self.config.overlayFor({'dev': 'robot_motor_sim'},
                                   {'no_such_action': {'x': 1}}, 'robot')


if __name__ == '__main__':
    unittest.main()
